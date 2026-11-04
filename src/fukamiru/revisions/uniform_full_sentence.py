"""One exact teacher-forcing route for every complete Japanese sentence.

The model returns raw pre-sampling logprobs for a selected target token.  We
force exactly one canonical token at each position, always from BOS plus the
preceding canonical IDs.  There is no bulk-grammar route or route fallback.
"""

from __future__ import annotations

import math
import time
from typing import Any


class UniformScoreError(RuntimeError):
    """A complete sentence could not be scored under the frozen profile."""


class UniformTokenScorer:
    def __init__(
        self, adapter: Any, *, context_size: int, cache_prompt: bool, profile_hash: str
    ) -> None:
        self.adapter = adapter
        self.context_size = context_size
        self.cache_prompt = cache_prompt
        self.profile_hash = profile_hash
        self.request_count = 0
        self.prompt_token_ids_sent = 0
        self.server_tokens_observed = 0
        self.cache_hits = 0

    @staticmethod
    def _ids(response: dict[str, Any]) -> tuple[int, ...]:
        raw = response.get("tokens")
        if not isinstance(raw, list):
            raise UniformScoreError("missing_returned_token_ids")
        return tuple(
            int(item.get("id", item.get("token"))) if isinstance(item, dict) else int(item)
            for item in raw
        )

    @staticmethod
    def _logprob(response: dict[str, Any], target: int) -> float:
        raw = response.get("completion_probabilities")
        if not isinstance(raw, list) or len(raw) != 1:
            raise UniformScoreError("missing_single_raw_probability")
        item = raw[0]
        if not isinstance(item, dict) or int(item.get("id", -1)) != target:
            raise UniformScoreError("raw_probability_token_mismatch")
        value = item.get("logprob")
        if not isinstance(value, (float, int)) or not math.isfinite(value) or value > 0:
            raise UniformScoreError("invalid_raw_probability")
        return float(value)

    def tokenize_complete(self, sentence: str) -> dict[str, Any]:
        if not sentence:
            raise UniformScoreError("empty_sentence")
        special = self.adapter.tokenize("", add_special=True)
        whole = self.adapter.tokenize(sentence, add_special=True)
        if len(special) != 1 or len(whole) < 2 or whole[0][0] != special[0][0]:
            raise UniformScoreError("bos_convention_unavailable")
        if len(whole) > self.context_size:
            raise UniformScoreError("context_overflow")
        bos = int(special[0][0])
        ids = tuple(int(item[0]) for item in whole[1:])
        bos_text = self.adapter.detokenize((bos,))
        rendered = self.adapter.detokenize((bos, *ids))
        if not rendered.startswith(bos_text):
            raise UniformScoreError("bos_detokenization_mismatch")
        body = rendered[len(bos_text) :]
        if body == sentence:
            convention = "exact_after_bos"
        elif body == " " + sentence:
            convention = "tokenizer_dummy_prefix_space_after_bos"
        else:
            raise UniformScoreError("whole_sentence_tokenization_not_roundtrip")
        return {
            "bos_id": bos,
            "token_ids": ids,
            "token_pieces": [item[1] for item in whole[1:]],
            "tokenization_convention": convention,
        }

    def score(self, sentence: str) -> dict[str, Any]:
        started = time.perf_counter()
        tokenized = self.tokenize_complete(sentence)
        bos = tokenized["bos_id"]
        ids = tokenized["token_ids"]
        logprobs: list[float] = []
        before_requests = self.request_count
        before_prompt = self.prompt_token_ids_sent
        before_observed = self.server_tokens_observed
        for index, target in enumerate(ids):
            prefix = [bos, *ids[:index]]
            body = {
                "prompt": prefix,
                "n_predict": 1,
                "temperature": -1.0,
                "top_k": 0,
                "top_p": 1.0,
                "min_p": 0.0,
                "typical_p": 1.0,
                "repeat_penalty": 1.0,
                "presence_penalty": 0.0,
                "frequency_penalty": 0.0,
                "dry_multiplier": 0.0,
                "n_probs": 1,
                "return_tokens": True,
                "cache_prompt": self.cache_prompt,
                "post_sampling_probs": False,
                "seed": 0,
                "stream": False,
                "logit_bias": [[target, 100.0]],
            }
            response = self.adapter._request_json(
                "/completion", body, timeout=self.adapter.settings.request_timeout_seconds
            )
            self.request_count += 1
            self.prompt_token_ids_sent += len(prefix)
            observed = self._ids(response)
            self.server_tokens_observed += len(observed)
            if observed != (target,):
                raise UniformScoreError(f"forced_token_mismatch_at_{index}")
            logprobs.append(self._logprob(response, target))
        return {
            "status": "complete",
            "sentence": sentence,
            "J_nats": math.fsum(logprobs),
            "text_token_count": len(ids),
            "character_count": len(sentence),
            "bos_id": bos,
            "bos_scored": False,
            "eos_appended": False,
            "tokenization_convention": tokenized["tokenization_convention"],
            "route": "uniform_per_token_teacher_forcing",
            "cache_prompt": self.cache_prompt,
            "profile_hash": self.profile_hash,
            "token_ids": list(ids),
            "token_pieces": tokenized["token_pieces"],
            "token_logprobs": logprobs,
            "request_count": self.request_count - before_requests,
            "prompt_token_ids_sent": self.prompt_token_ids_sent - before_prompt,
            "server_tokens_observed": self.server_tokens_observed - before_observed,
            "duration_seconds": time.perf_counter() - started,
        }
