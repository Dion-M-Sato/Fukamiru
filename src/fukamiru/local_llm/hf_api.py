"""Small HF Jobs REST client using system trust and private exposed ports."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import truststore

from fukamiru.errors import NonRetryableFukamiruError


def token_available() -> str | None:
    """Read the same credential store as hf; never serialize its contents."""
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    try:
        from huggingface_hub import get_token

        return get_token()
    except ImportError:
        return None


class HFJobsAPI:
    def request(self, url: str, body: Any = None) -> Any:
        token = token_available()
        if not token:
            raise NonRetryableFukamiruError("hf_jobs.authentication_missing")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not (
            parsed.hostname == "huggingface.co" or (parsed.hostname or "").endswith(".hf.jobs")
        ):
            raise NonRetryableFukamiruError("hf_jobs.invalid_private_endpoint")
        request = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)),
                SameOriginRedirect(),
            )
            with opener.open(request, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            # Do not put bearer headers, command bodies or manuscript text into errors.
            raise NonRetryableFukamiruError(f"hf_jobs.http_{exc.code}") from exc

    def hardware(self) -> list[dict[str, Any]]:
        return self.request("https://huggingface.co/api/jobs/hardware")

    def launch(self, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self.request(f"https://huggingface.co/api/jobs/{namespace}", spec)

    def inspect(self, namespace: str, job_id: str) -> dict[str, Any]:
        return self.request(f"https://huggingface.co/api/jobs/{namespace}/{job_id}")

    def cancel(self, namespace: str, job_id: str) -> None:
        self.request(f"https://huggingface.co/api/jobs/{namespace}/{job_id}/cancel", {})

    def find(self, namespace: str, labels: dict[str, str]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode([("label", f"{k}={v}") for k, v in labels.items()])
        return self.request(f"https://huggingface.co/api/jobs/{namespace}?{query}")


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlparse(req.full_url)
        new = urllib.parse.urlparse(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise NonRetryableFukamiruError("hf_jobs.refuse_cross_origin_credential_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
