"""Versioned MeCab/GiNZA analysis, with one-shot ambiguity adjudication.

Offsets always belong to the application. A missing parser is an unresolved
analysis, never a successful grammatical or semantic review.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fukamiru.errors import ExternalServiceUnavailable, FukamiruError, RunCancelled

POLICY = "mecab-ginza-roles-v2"
V22_ANALYSIS_CONTRACT = "ambiguity-contract-v3-engine-bound-monotonic"
V25_LOCAL_ANALYSIS_CONTRACT = "mecab-ginza-local-analysis-v1"
_LOCK = threading.RLock()
_ALIASES = {"位置づけ": "位置付け", "結び付": "結びつ", "裏付": "裏づけ"}


def normalized_word(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for source, target in _ALIASES.items():
        text = text.replace(source, target)
    return text


@lru_cache(maxsize=1)
def analyzer_versions() -> dict[str, str]:
    result = {"policy": POLICY}
    for name in ("mecab-python3", "unidic-lite", "ginza", "ja-ginza", "spacy", "sudachidict-core"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "unavailable"
    return result


@lru_cache(maxsize=1)
def _engines() -> tuple[Any, Any]:
    import MeCab
    import spacy
    import unidic_lite

    return MeCab.Tagger(f'-d "{unidic_lite.DICDIR}"'), spacy.load("ja_ginza")


@lru_cache(maxsize=1024)
def parse_japanese(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "versions": analyzer_versions(),
        "tokens": [],
        "bunsetsu": [],
        "arguments": [],
        "negation_scopes": [],
        "modality": [],
        "ambiguities": [],
        "ambiguity_spans": [],
    }
    try:
        import ginza

        with _LOCK:
            tagger, nlp = _engines()
            node = tagger.parseToNode(text)
            cursor = 0
            while node:
                if node.surface:
                    start = text.find(node.surface, cursor)
                    features = node.feature.split(",")
                    result["tokens"].append(
                        {
                            "text": node.surface,
                            "start": start,
                            "end": start + len(node.surface),
                            "pos": features[:4],
                            "inflection": features[4:6],
                            "lemma": features[7] if len(features) > 7 else node.surface,
                        }
                    )
                    cursor = start + len(node.surface)
                node = node.next
            doc = nlp(text)
        result["dependencies"] = [
            {
                "text": t.text,
                "start": t.idx,
                "end": t.idx + len(t),
                "pos": t.pos_,
                "lemma": t.lemma_,
                "role": t.dep_,
                "head_start": t.head.idx,
            }
            for t in doc
        ]
        result["bunsetsu"] = [
            {"text": s.text, "start": s.start_char, "end": s.end_char}
            for s in ginza.bunsetu_spans(doc)
        ]
        for token in doc:
            if token.dep_ in {"nsubj", "obj", "iobj", "obl", "csubj"}:
                result["arguments"].append(
                    {
                        "role": token.dep_,
                        "head": normalized_word(token.lemma_),
                        "participants": sorted(
                            {
                                normalized_word(child.lemma_)
                                for child in token.subtree
                                if child.pos_ in {"NOUN", "PROPN"}
                            }
                        ),
                        "start": token.idx,
                        "predicate_start": token.head.idx,
                    }
                )
            if token.pos_ == "AUX" and token.lemma_ in {"ない", "ぬ", "ず"}:
                result["negation_scopes"].append(
                    {
                        "predicate_start": token.head.idx,
                        "sentence_start": token.sent.start_char,
                    }
                )
            if token.pos_ == "AUX" and token.lemma_ in {"べし", "らしい", "だろう", "まい"}:
                result["modality"].append(token.lemma_)
        if not doc.has_annotation("DEP"):
            result["ambiguities"].append("dependency_unavailable")
        # Tokenizers legitimately use different word boundaries. Record these
        # without equating boundary differences with grammatical errors.
        mecab_ends = {t["end"] for t in result["tokens"]}
        result["boundary_disagreements"] = sorted(
            mecab_ends.symmetric_difference(t.idx + len(t) for t in doc)
        )
        for token in doc:
            matches = [
                t
                for t in result["tokens"]
                if t["start"] == token.idx and t["end"] == token.idx + len(token)
            ]
            if matches and (
                (matches[0]["pos"][0] == "助動詞" and token.pos_ == "NOUN")
                or (matches[0]["pos"][0] == "名詞" and token.pos_ == "AUX")
            ):
                result["ambiguities"].append(f"pos_disagreement:{token.idx}")
                result["ambiguity_spans"].append(
                    {"start": token.sent.start_char, "end": token.sent.end_char}
                )
        if any(t.pos_ == "VERB" for t in doc) and not any(
            t.dep_ in {"nsubj", "csubj"} for t in doc
        ):
            result["ambiguities"].append("implicit_subject")
            result["ambiguity_spans"].extend(
                {"start": sent.start_char, "end": sent.end_char}
                for sent in doc.sents
                if any(t.pos_ == "VERB" for t in sent)
            )
    except (ImportError, OSError, RuntimeError) as exc:
        result["ambiguities"].append(f"analyzer_unavailable:{type(exc).__name__}")
    result["status"] = "unresolved" if result["ambiguities"] else "analyzed"
    return result


def target_analysis_status(analysis: dict, start: int, end: int) -> str:
    tokens = analysis.get("tokens") or []
    if start not in {t["start"] for t in tokens} or end not in {t["end"] for t in tokens}:
        return "unresolved"
    if analysis.get("status") == "analyzed":
        return "analyzed"
    spans = analysis.get("ambiguity_spans") or []
    if spans and all(end <= s["start"] or s["end"] <= start for s in spans):
        return "analyzed"
    return "unresolved"


def cached_analysis(connection: Any, text: str, context: str, resolver: Any = None) -> dict:
    signature = hashlib.sha256(
        json.dumps(
            [text, context, analyzer_versions(), "ambiguity-contract-v1"],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    row = connection.execute(
        "SELECT payload_json FROM manuscript_language_analyses WHERE signature=?", (signature,)
    ).fetchone()
    if row:
        return json.loads(row[0])
    result = json.loads(json.dumps(parse_japanese(text), ensure_ascii=False))
    result["signature"] = signature
    needs_adjudication = result["status"] == "unresolved" and not any(
        a.startswith("analyzer_unavailable") for a in result["ambiguities"]
    )
    if needs_adjudication and not callable(resolver):
        raise FukamiruError("Japanese ambiguity resolver unavailable")
    if needs_adjudication:
        try:
            verdict = resolver(text=text, context=context, analysis=result)
            result["adjudication"] = verdict
            if (
                verdict.get("status") == "resolved"
                and verdict.get("explanation")
                and isinstance(verdict.get("roles"), list)
                and verdict["roles"]
            ):
                result["status"] = "analyzed"
        except Exception as exc:
            result["adjudication_error"] = type(exc).__name__
    result["decision"] = (
        "analysis_ready" if result["status"] == "analyzed" else "semantic_analysis_unresolved"
    )
    # Negative results are durable too. No retry loop on an unchanged manuscript.
    connection.execute(
        "INSERT OR IGNORE INTO manuscript_language_analyses VALUES (?,?)",
        (signature, json.dumps(result, ensure_ascii=False)),
    )
    connection.commit()
    return result


def _persist_analysis_v22(connection: Any, result: dict) -> dict:
    """Atomically retain the strongest completed result for this exact contract.

    Initial requests already in flight may race. A late incomplete response
    must neither overwrite a valid decision nor reopen its supplement budget.
    Conversely, a valid late response may improve a conditional incomplete one.
    The conflict predicate is evaluated by SQLite under its write lock, not by
    a read/modify/write check performed before the model call.
    """

    rank = (
        0
        if result.get("response_contract_state") == "supplement_pending"
        else 1
        if result.get("decision") == "response_contract_incomplete"
        else 2
    )
    connection.execute(
        "INSERT INTO manuscript_language_analyses VALUES (?,?) "
        "ON CONFLICT(signature) DO UPDATE SET payload_json=excluded.payload_json "
        "WHERE (CASE "
        "WHEN json_extract(manuscript_language_analyses.payload_json, "
        "'$.response_contract_state')='supplement_pending' THEN 0 "
        "WHEN json_extract(manuscript_language_analyses.payload_json, "
        "'$.decision')='response_contract_incomplete' THEN 1 ELSE 2 END) < ? "
        "OR (?=0 AND json_extract(manuscript_language_analyses.payload_json, "
        "'$.response_contract_state')='supplement_pending' "
        "AND COALESCE(json_extract(manuscript_language_analyses.payload_json, "
        "'$.adjudication_attempt_count'), 0) < ?)",
        (
            result["signature"],
            json.dumps(result, ensure_ascii=False),
            rank,
            rank,
            int(result.get("adjudication_attempt_count") or 0),
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT payload_json FROM manuscript_language_analyses WHERE signature=?",
        (result["signature"],),
    ).fetchone()
    return json.loads(row[0])


def cached_analysis_v22(
    connection: Any,
    text: str,
    context: str,
    resolver: Any = None,
    *,
    engine_fingerprint: str,
) -> dict:
    """V22 analysis cache that never turns execution failure into semantics.

    A valid ``unresolved`` adjudication is a durable content decision. An
    incomplete response receives one immediate repair request and then becomes
    an explicit conditional-result status. Transport and execution exceptions
    escape without writing a negative cache entry. The engine identity is
    mandatory even when local parsing succeeds: a later ambiguity decision
    must never be silently inherited from a different inference engine.
    """

    if not isinstance(engine_fingerprint, str) or not engine_fingerprint.strip():
        raise ValueError("V22 Japanese analysis requires an inference-engine fingerprint")
    signature = hashlib.sha256(
        json.dumps(
            [text, context, analyzer_versions(), V22_ANALYSIS_CONTRACT, engine_fingerprint],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    row = connection.execute(
        "SELECT payload_json FROM manuscript_language_analyses WHERE signature=?", (signature,)
    ).fetchone()
    cached = json.loads(row[0]) if row else None
    if cached and cached.get("response_contract_state") != "supplement_pending":
        return cached
    result = (
        cached
        if cached is not None
        else json.loads(json.dumps(parse_japanese(text), ensure_ascii=False))
    )
    result["signature"] = signature
    result["analysis_contract"] = V22_ANALYSIS_CONTRACT
    result["inference_engine_fingerprint"] = engine_fingerprint
    result["analysis_provenance"] = {
        "contract": V22_ANALYSIS_CONTRACT,
        "engine_fingerprint": engine_fingerprint,
        "decision_source": "external_adjudicator"
        if result["status"] == "unresolved"
        else "local_analyzers",
    }

    def valid_verdict(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("status") not in {"resolved", "unresolved"}:
            return False
        if not str(value.get("explanation") or "").strip():
            return False
        roles = value.get("roles")
        return isinstance(roles, list) and (
            value.get("status") == "unresolved" or bool(roles)
        )

    if result["status"] == "unresolved":
        if any(a.startswith("analyzer_unavailable") for a in result["ambiguities"]):
            raise FukamiruError(
                "Japanese morphological or dependency analysis is unavailable; "
                "semantic adjudication cannot safely begin"
            )
        if not callable(resolver):
            raise ExternalServiceUnavailable(
                "External Codex Japanese ambiguity adjudication is required but unavailable; "
                "automatic inference-engine fallback is disabled"
            )
        verdict: Any = None
        start_attempt = int(result.get("adjudication_attempt_count") or 0)
        for attempt in range(start_attempt, 2):
            # Another caller may have completed this signature while our
            # previous response was in flight. Reuse its canonical decision
            # before requesting a supplement, not just when saving our result.
            latest_row = connection.execute(
                "SELECT payload_json FROM manuscript_language_analyses WHERE signature=?",
                (signature,),
            ).fetchone()
            if latest_row:
                latest = json.loads(latest_row[0])
                if latest.get("response_contract_state") != "supplement_pending":
                    return latest
            try:
                verdict = resolver(text=text, context=context, analysis=result)
            except (ExternalServiceUnavailable, RunCancelled):
                raise
            except Exception as exc:
                # An execution exception is not evidence of semantic ambiguity.
                raise FukamiruError(
                    "Japanese ambiguity adjudication failed before a content decision"
                ) from exc
            if valid_verdict(verdict):
                result["adjudication"] = verdict
                result["adjudication_attempt_count"] = attempt + 1
                result["response_contract_state"] = "complete"
                if verdict["status"] == "resolved":
                    result["status"] = "analyzed"
                break
            result["adjudication"] = verdict if isinstance(verdict, dict) else None
            result["adjudication_attempt_count"] = attempt + 1
            if attempt == 0:
                # Persist the consumed response-repair budget before issuing the
                # supplement.  A transport failure during the supplement can
                # then be resumed without silently creating a third attempt.
                result["response_contract_state"] = "supplement_pending"
                result = _persist_analysis_v22(connection, result)
                if result.get("response_contract_state") != "supplement_pending":
                    return result
        else:
            result["adjudication"] = verdict if isinstance(verdict, dict) else None
            result["adjudication_attempt_count"] = 2
            result["adjudication_error"] = "response_contract_incomplete"
            result["status"] = "unresolved"
            result["decision"] = "response_contract_incomplete"
            result["response_contract_state"] = "complete"
    if "decision" not in result:
        result["decision"] = (
            "analysis_ready" if result["status"] == "analyzed" else "semantic_analysis_unresolved"
        )
    result["response_contract_state"] = "complete"
    return _persist_analysis_v22(connection, result)


def cached_local_analysis_v25(
    connection: Any, text: str, *, parsed: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Cache a parser-only result without implying a Codex engine decision."""
    result = parsed if parsed is not None else parse_japanese(text)
    if result.get("status") != "analyzed":
        raise ValueError("Local analysis cache only accepts unambiguous parser results")
    signature = hashlib.sha256(
        json.dumps(
            [text, analyzer_versions(), V25_LOCAL_ANALYSIS_CONTRACT],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    row = connection.execute(
        "SELECT payload_json FROM manuscript_language_analyses WHERE signature=?", (signature,)
    ).fetchone()
    if row:
        return json.loads(row[0])
    stored = json.loads(json.dumps(result, ensure_ascii=False))
    stored.update(
        signature=signature,
        analysis_contract=V25_LOCAL_ANALYSIS_CONTRACT,
        analysis_provenance={
            "contract": V25_LOCAL_ANALYSIS_CONTRACT,
            "decision_source": "local_analyzers",
        },
        decision="analysis_ready",
        response_contract_state="complete",
    )
    connection.execute(
        "INSERT OR IGNORE INTO manuscript_language_analyses VALUES (?,?)",
        (signature, json.dumps(stored, ensure_ascii=False)),
    )
    connection.commit()
    row = connection.execute(
        "SELECT payload_json FROM manuscript_language_analyses WHERE signature=?", (signature,)
    ).fetchone()
    return json.loads(row[0])


def compare_grammar(source: str, candidate: str, start: int, end: int) -> tuple[str, ...]:
    before = parse_japanese(source)
    after = parse_japanese(candidate)
    if any(
        a.startswith("analyzer_unavailable") for a in before["ambiguities"] + after["ambiguities"]
    ):
        return ("analysis.unavailable",)
    delta = len(candidate) - len(source)
    # Valency alternation is not participant replacement: 回復に寄与する -> 回復を促す.
    contribution_verbs = {"寄与", "貢献", "促す", "支える"}
    before_verbs = {t["lemma"] for t in before.get("dependencies", []) if t["pos"] == "VERB"}
    after_verbs = {t["lemma"] for t in after.get("dependencies", []) if t["pos"] == "VERB"}
    contribution_alternation = bool(
        before_verbs & contribution_verbs and after_verbs & contribution_verbs
    )

    def mapped(offset: int) -> int | str:
        if offset < start:
            return offset
        if offset >= end:
            return offset + delta
        return "edited_predicate"

    def roles(parsed: dict, old: bool) -> set[tuple]:
        return {
            (
                (
                    "affected"
                    if contribution_alternation and a["role"] in {"obj", "obl"}
                    else a["role"]
                ),
                tuple(a.get("participants") or [a["head"]]),
                mapped(a["predicate_start"])
                if old
                else (
                    "edited_predicate"
                    if start <= a["predicate_start"] < end + delta
                    else a["predicate_start"]
                ),
            )
            for a in parsed["arguments"]
        }

    def negations(parsed: dict, old: bool) -> set:
        return {
            mapped(a["predicate_start"])
            if old
            else (
                "edited_predicate"
                if start <= a["predicate_start"] < end + delta
                else a["predicate_start"]
            )
            for a in parsed["negation_scopes"]
        }

    issues = []
    if roles(before, True) != roles(after, False):
        issues.append("veto.predicate_arguments_changed")
    if negations(before, True) != negations(after, False):
        issues.append("veto.negation_changed")
    if set(before["modality"]) != set(after["modality"]):
        issues.append("veto.modality_changed")
    return tuple(issues)
