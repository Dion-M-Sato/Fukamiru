"""Provider-wide bounded HTTP budgets, independent of the Codex execution pool."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

from fukamiru.errors import FukamiruError


class ProviderRequestError(FukamiruError):
    def __init__(
        self, provider: str, outcome: str, status: int | None = None,
        *, retry_after_seconds: float = 2,
    ):
        self.outcome = outcome
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{provider}: {outcome} (HTTP {status or 'unavailable'})")


@dataclass
class ProviderBudget:
    lock: Any = field(default_factory=threading.Lock)
    next_request: float = 0.0
    unavailable_until: float = 0.0
    unavailable_reason: str = "rate_limited"
    cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)


_budgets: dict[str, ProviderBudget] = {}
_budgets_lock = threading.Lock()


def retry_delay(headers: Any, fallback: float) -> float:
    value = headers.get("Retry-After", "") if headers else ""
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return fallback


def budget_headers(headers: Any) -> dict[str, str]:
    if not hasattr(headers, "items"):
        return {}
    return {
        str(k).lower(): str(v)
        for k, v in headers.items()
        if str(k).lower().startswith("x-ratelimit")
        or str(k).lower().startswith("x-rate-limit")
        or str(k).lower() in {"retry-after", "x-concurrency-limit", "x-api-pool"}
    }


def get_json(provider: Any, parameters: dict[str, Any]) -> dict[str, Any]:
    with _budgets_lock:
        budget = _budgets.setdefault(provider.base_url, ProviderBudget())
    key = urllib.parse.urlencode(parameters)
    query = (
        parameters.get("search") or parameters.get("query.bibliographic")
        or parameters.get("query") or parameters.get("any") or parameters.get("title")
    )
    # One in-flight request per provider: safe for the unauthenticated Crossref
    # pool and shared by evidence/reference nodes and all Runs in this process.
    with budget.lock:
        now = time.monotonic()
        cached = budget.cache.get(key)
        if cached and cached[0] > now:
            provider.request_outcomes.append({"outcome": "cache_hit"})
            return json.loads(json.dumps(cached[1]))
        if budget.unavailable_until > now:
            provider.request_outcomes.append(
                {"outcome": budget.unavailable_reason, "retry_in": budget.unavailable_until - now}
            )
            raise ProviderRequestError(
                provider.name, budget.unavailable_reason,
                429 if budget.unavailable_reason in {"rate_limited", "budget_exhausted"} else None,
                retry_after_seconds=budget.unavailable_until - now,
            )
        for attempt in range(3):
            time.sleep(max(0, budget.next_request - time.monotonic()))
            request = urllib.request.Request(
                provider.base_url + "?" + key, headers=provider._headers()
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=provider.timeout_seconds, context=provider.ssl_context
                ) as response:
                    data = response.read()
                    decoder = getattr(provider, "decode_response", None)
                    payload = decoder(data) if decoder else json.loads(data.decode("utf-8"))
                    headers = getattr(response, "headers", {})
                if not isinstance(payload, dict):
                    raise ValueError("non-object JSON")
                provider.request_outcomes.append(
                    {
                        "outcome": "success",
                        "attempt": attempt + 1,
                        "query": query,
                        "headers": budget_headers(headers),
                    }
                )
                budget.cache[key] = (time.monotonic() + 900, payload)
                budget.unavailable_until = 0.0
                if len(budget.cache) > 512:
                    budget.cache.pop(next(iter(budget.cache)))
                interval = max(0.25, getattr(provider, "minimum_interval_seconds", 0))
                try:
                    limit = float(headers.get("x-rate-limit-limit", 0))
                    window = float(str(headers.get("x-rate-limit-interval", "1s")).rstrip("s"))
                    if limit > 0:
                        interval = max(interval, window / limit)
                except (ValueError, TypeError, AttributeError):
                    pass
                budget.next_request = time.monotonic() + interval
                return payload
            except urllib.error.HTTPError as exc:
                delay = retry_delay(exc.headers, 2 ** (attempt + 1))
                outcome = (
                    "rate_limited"
                    if exc.code == 429
                    else ("temporary_failure" if exc.code >= 500 else "invalid_request")
                )
                body = exc.read(4096).decode("utf-8", errors="replace").casefold()
                if exc.code == 429 and any(word in body for word in ("daily", "budget", "credits")):
                    outcome = "budget_exhausted"
                    delay = max(delay, 3600)
                provider.request_outcomes.append(
                    {
                        "outcome": outcome,
                        "status": exc.code,
                        "attempt": attempt + 1,
                        "retry_after_seconds": delay,
                        "query": query,
                        "headers": budget_headers(exc.headers),
                    }
                )
                if exc.code == 429:
                    budget.unavailable_until = time.monotonic() + max(delay, 2)
                    budget.unavailable_reason = outcome
                if (exc.code == 429 or exc.code >= 500) and attempt < 2 and delay <= 8:
                    budget.next_request = time.monotonic() + delay
                    continue
                raise ProviderRequestError(
                    provider.name, outcome, exc.code, retry_after_seconds=delay
                ) from exc
            except (OSError, ValueError) as exc:
                outcome = "invalid_response" if isinstance(exc, ValueError) else "temporary_failure"
                provider.request_outcomes.append({"outcome": outcome, "attempt": attempt + 1})
                if isinstance(exc, OSError):
                    # Retrying a full timeout inline monopolizes the shared
                    # provider lock. Let the durable node retry release it.
                    budget.unavailable_until = time.monotonic() + 30
                    budget.unavailable_reason = outcome
                raise ProviderRequestError(
                    provider.name, outcome, retry_after_seconds=30
                ) from exc
        raise ProviderRequestError(provider.name, "temporary_failure")
