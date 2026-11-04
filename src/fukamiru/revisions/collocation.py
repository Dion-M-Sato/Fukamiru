"""V29 predicate-collocation frames, probes, evidence, and durable provenance."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.local_llm import ContinuationScore, LlamaServerAdapter
from fukamiru.revisions.japanese_analysis import analyzer_versions, parse_japanese
from fukamiru.storage.runs import utc_now

COLLOCATION_FRAME_POLICY = "collocation-frame-v29.1-full-document-mecab-ginza"
COLLOCATION_PROBE_POLICY = "natural-japanese-probes-v29"
COLLOCATION_DECISION_POLICY = "validation-only-no-auto-apply-v29"

_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
_CASE = {
    "は": "ga",
    "が": "ga",
    "を": "wo",
    "に": "ni",
    "へ": "ni",
    "で": "de",
    "と": "to",
    "から": "kara",
    "より": "yori",
}
_CANONICAL_PARTICLE = {
    "ga": "が",
    "wo": "を",
    "ni": "に",
    "de": "で",
    "to": "と",
    "kara": "から",
    "yori": "より",
}


@dataclass(frozen=True)
class CollocationArgument:
    surface: str
    surface_particle: str
    canonical_case: str
    semantic_class: str


@dataclass(frozen=True)
class CollocationFrame:
    id: str
    run_id: str
    document_id: str
    block_id: str
    sentence_ordinal: int
    sentence: str
    sentence_start: int
    sentence_end: int
    predicate_start: int
    predicate_end: int
    expression: str
    lemma: str
    voice: str
    predicate_type: str
    arguments: tuple[CollocationArgument, ...]
    register: str
    signature: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["arguments"] = [asdict(item) for item in self.arguments]
        return value


def frame_from_dict(value: dict[str, Any]) -> CollocationFrame:
    return CollocationFrame(
        **{
            **value,
            "arguments": tuple(
                CollocationArgument(**dict(item)) for item in value.get("arguments") or []
            ),
        }
    )


@dataclass(frozen=True)
class CollocationProbe:
    id: str
    frame_id: str
    kind: str
    prefix: str
    suffix: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class CollocationScorer(Protocol):
    @property
    def identity(self) -> str: ...

    def score(self, *, prefix: str, candidate: str) -> ContinuationScore: ...


class LLMJPBaseScorer:
    scorer_type = "llm_jp_base"
    quantization = "Q4_K_M"

    def __init__(self, adapter: LlamaServerAdapter) -> None:
        self.adapter = adapter

    @property
    def identity(self) -> str:
        return self.adapter.model_identity

    def score(self, *, prefix: str, candidate: str) -> ContinuationScore:
        return self.adapter.score_continuation(prefix=prefix, continuation=candidate)


class CorpusCollocationScorer:
    """Reserved interface for future observed-corpus evidence."""

    scorer_type = "corpus"

    @property
    def identity(self) -> str:
        return "not-configured"

    def score(self, *, prefix: str, candidate: str) -> ContinuationScore:
        del prefix, candidate
        raise NotImplementedError("No corpus collocation source is configured in V29")


def _semantic_class(text: str) -> str:
    if re.search(r"(?:年|月|日|時|期|世紀)$", text):
        return "TIME"
    if re.search(r"(?:人|氏|者|著者|読者)$", text):
        return "PERSON"
    if re.search(r"(?:大学|機関|組織|政府|委員会|社)$", text):
        return "ORGANIZATION"
    if re.search(r"(?:方法|分析|調査|比較|検討|手法)$", text):
        return "METHOD"
    if re.search(r"(?:差異|関係|関連|相違|類似|影響)$", text):
        return "ABSTRACT_RELATION"
    if re.search(r"(?:結果|結論|帰結)$", text):
        return "RESULT"
    if re.search(r"(?:条件|状況|状態)$", text):
        return "CONDITION"
    if re.search(r"(?:論文|文献|資料|記述|テクスト|文章)$", text):
        return "TEXT"
    return "UNKNOWN"


def _voice(expression: str) -> str:
    if re.search(r"させられ", expression):
        return "causative_passive"
    if re.search(r"(?:され|られ|れる)", expression):
        return "passive"
    if re.search(r"させ", expression):
        return "causative"
    return "active"


def _surface_particle(text: str, token_end: int) -> str:
    following = text[token_end : token_end + 2]
    for particle in ("から", "より", "は", "が", "を", "に", "へ", "で", "と"):
        if following.startswith(particle):
            return particle
    return ""


def extract_collocation_frames(
    *,
    canonical_html: str,
    run_id: str,
    document_id: str,
    source_preserve_block_ids: set[str],
    editable_blocks: Any,
) -> tuple[CollocationFrame, ...]:
    """Extract every parser-visible predicate from every eligible manuscript block."""

    frames: list[CollocationFrame] = []
    for block in editable_blocks(canonical_html):
        block_id, text = str(block["id"]), str(block["text"])
        if block_id in source_preserve_block_ids or block.get("inline_protected"):
            continue
        analysis = parse_japanese(text)
        dependencies = sorted(
            [dict(item) for item in analysis.get("dependencies") or []],
            key=lambda item: int(item["start"]),
        )
        if not dependencies:
            raise NonRetryableFukamiruError(
                "collocation.frame_parser_unavailable: MeCab/GiNZA did not return dependencies"
            )
        sentences = [match for match in _SENTENCE_RE.finditer(text) if match.group().strip()]
        for sentence_ordinal, match in enumerate(sentences):
            for index, token in enumerate(dependencies):
                start = int(token["start"])
                if not (match.start() <= start < match.end()) or token.get("pos") not in {
                    "VERB",
                    "ADJ",
                }:
                    continue
                if token.get("pos") == "ADJ":
                    following = dependencies[index + 1 : index + 3]
                    contiguous = all(
                        int(item["start"])
                        == (int(token["end"]) if offset == 0 else int(following[offset - 1]["end"]))
                        for offset, item in enumerate(following)
                    )
                    if (
                        following
                        and contiguous
                        and any(item.get("pos") == "VERB" for item in following)
                    ):
                        continue
                expression_start = start
                if (
                    str(token.get("lemma") or "") == "する"
                    and index > 0
                    and int(dependencies[index - 1]["end"]) == start
                    and dependencies[index - 1].get("pos") in {"NOUN", "PROPN"}
                ):
                    expression_start = int(dependencies[index - 1]["start"])
                elif str(token.get("lemma") or "") in {"する", "為る"}:
                    cursor = index - 1
                    while (
                        cursor >= 0
                        and int(dependencies[cursor]["end"]) == expression_start
                        and dependencies[cursor].get("pos") in {"ADJ", "AUX"}
                    ):
                        expression_start = int(dependencies[cursor]["start"])
                        cursor -= 1
                expression_end = int(token["end"])
                for following in dependencies[index + 1 :]:
                    if int(following["start"]) != expression_end or following.get("pos") != "AUX":
                        break
                    expression_end = int(following["end"])
                expression = text[expression_start:expression_end]
                arguments: list[CollocationArgument] = []
                for argument in analysis.get("arguments") or []:
                    if int(argument.get("predicate_start", -1)) != start:
                        continue
                    argument_start = int(argument.get("start", -1))
                    argument_token = next(
                        (item for item in dependencies if int(item["start"]) == argument_start),
                        None,
                    )
                    if argument_token is None:
                        continue
                    descendant_starts = {argument_start}
                    changed = True
                    while changed:
                        changed = False
                        for item in dependencies:
                            item_start = int(item["start"])
                            if (
                                item_start < argument_start
                                and int(item.get("head_start", -1)) in descendant_starts
                                and item_start not in descendant_starts
                            ):
                                descendant_starts.add(item_start)
                                changed = True
                    surface = text[min(descendant_starts) : int(argument_token["end"])]
                    particle = _surface_particle(text, int(argument_token["end"]))
                    arguments.append(
                        CollocationArgument(
                            surface=surface,
                            surface_particle=particle,
                            canonical_case=_CASE.get(
                                particle, str(argument.get("role") or "unknown")
                            ),
                            semantic_class=_semantic_class(surface),
                        )
                    )
                signature = hashlib.sha256(
                    json.dumps(
                        [
                            COLLOCATION_FRAME_POLICY,
                            analyzer_versions(),
                            block_id,
                            match.start(),
                            expression_start,
                            expression,
                            [asdict(item) for item in arguments],
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                frames.append(
                    CollocationFrame(
                        id="coll-frame:" + signature[:24],
                        run_id=run_id,
                        document_id=document_id,
                        block_id=block_id,
                        sentence_ordinal=sentence_ordinal,
                        sentence=match.group(),
                        sentence_start=match.start(),
                        sentence_end=match.end(),
                        predicate_start=expression_start,
                        predicate_end=expression_end,
                        expression=expression,
                        lemma=str(token.get("lemma") or expression),
                        voice=_voice(expression),
                        predicate_type=(
                            "complex_predicate" if expression_start < start else "simple_predicate"
                        ),
                        arguments=tuple(arguments),
                        register="academic",
                        signature=signature,
                    )
                )
    return tuple(frames)


def generate_probes(frame: CollocationFrame) -> tuple[CollocationProbe, ...]:
    relative_start = frame.predicate_start - frame.sentence_start
    relative_end = frame.predicate_end - frame.sentence_start
    original = CollocationProbe(
        id=f"{frame.id}:original_sentence",
        frame_id=frame.id,
        kind="original_sentence",
        prefix=frame.sentence[:relative_start],
        suffix=frame.sentence[relative_end:],
    )
    local_arguments = "".join(item.surface + item.surface_particle for item in frame.arguments[-2:])
    local = CollocationProbe(
        id=f"{frame.id}:local_predicate_frame",
        frame_id=frame.id,
        kind="local_predicate_frame",
        prefix=local_arguments or frame.sentence[max(0, relative_start - 40) : relative_start],
        suffix="",
    )
    canonical_arguments = "".join(
        item.surface + _CANONICAL_PARTICLE.get(item.canonical_case, item.surface_particle)
        for item in frame.arguments[-2:]
    )
    canonical = CollocationProbe(
        id=f"{frame.id}:canonicalized_frame",
        frame_id=frame.id,
        kind="canonicalized_frame",
        prefix=canonical_arguments or local.prefix,
        suffix="",
    )
    return original, local, canonical


def valid_candidate(frame: CollocationFrame, expression: str) -> bool:
    expression = expression.strip()
    if not expression or len(expression) > 60 or expression == frame.expression:
        return False
    if any(character in expression for character in "。！？!?\r\n\t"):
        return False
    if _voice(expression) != frame.voice:
        return False
    analysis = parse_japanese(expression)
    return any(item.get("pos") in {"VERB", "ADJ"} for item in analysis.get("dependencies") or [])


def score_payload(score: ContinuationScore) -> dict[str, Any]:
    return {
        "raw_logprob_sum": score.raw_logprob_sum,
        "mean_token_logprob": score.mean_token_logprob,
        "mean_character_logprob": score.mean_character_logprob,
        "token_ids": list(score.token_ids),
        "token_pieces": list(score.token_pieces),
        "token_logprobs": list(score.token_logprobs),
    }


class CollocationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def replace_frames(
        self, frames: tuple[CollocationFrame, ...], *, run_id: str, node_run_id: str
    ) -> None:
        self.connection.execute(
            "DELETE FROM manuscript_collocation_frames WHERE run_id=?", (run_id,)
        )
        for frame in frames:
            self.connection.execute(
                """INSERT INTO manuscript_collocation_frames(
                    id,run_id,node_run_id,document_id,block_id,sentence_ordinal,
                    sentence_start,sentence_end,predicate_start,predicate_end,
                    source_expression,frame_json,signature,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    frame.id,
                    run_id,
                    node_run_id,
                    frame.document_id,
                    frame.block_id,
                    frame.sentence_ordinal,
                    frame.sentence_start,
                    frame.sentence_end,
                    frame.predicate_start,
                    frame.predicate_end,
                    frame.expression,
                    json.dumps(frame.as_dict(), ensure_ascii=False),
                    frame.signature,
                    utc_now(),
                ),
            )
        self.connection.commit()

    def frames(self, run_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["frame_json"])
            for row in self.connection.execute(
                "SELECT frame_json FROM manuscript_collocation_frames WHERE run_id=? "
                "ORDER BY block_id,sentence_ordinal,predicate_start",
                (run_id,),
            )
        ]

    def candidates(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM manuscript_collocation_candidates WHERE run_id=? "
                "ORDER BY frame_id,candidate_ordinal",
                (run_id,),
            )
        ]

    def probes(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM manuscript_collocation_probes WHERE run_id=? "
                "ORDER BY frame_id,probe_kind",
                (run_id,),
            )
        ]

    def save_candidates(
        self, *, run_id: str, frame_id: str, candidates: list[dict[str, Any]]
    ) -> None:
        self.connection.execute(
            "DELETE FROM manuscript_collocation_candidates WHERE frame_id=?", (frame_id,)
        )
        for ordinal, candidate in enumerate(candidates):
            self.connection.execute(
                """INSERT INTO manuscript_collocation_candidates(
                    id,run_id,frame_id,candidate_ordinal,expression,source_kind,
                    rationale,created_at
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    str(candidate["id"]),
                    run_id,
                    frame_id,
                    ordinal,
                    str(candidate["expression"]),
                    str(candidate["source_kind"]),
                    str(candidate.get("rationale") or ""),
                    utc_now(),
                ),
            )
        self.connection.commit()

    def save_probes(self, *, run_id: str, probes: tuple[CollocationProbe, ...]) -> None:
        for probe in probes:
            self.connection.execute(
                """INSERT OR REPLACE INTO manuscript_collocation_probes(
                    id,run_id,frame_id,probe_kind,prefix_text,suffix_text,created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    probe.id,
                    run_id,
                    probe.frame_id,
                    probe.kind,
                    probe.prefix,
                    probe.suffix,
                    utc_now(),
                ),
            )
        self.connection.commit()

    def scores(
        self,
        run_id: str,
        *,
        scorer_type: str | None = None,
        scorer_identity: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["run_id=?"]
        parameters: list[Any] = [run_id]
        if scorer_type is not None:
            clauses.append("scorer_type=?")
            parameters.append(scorer_type)
        if scorer_identity is not None:
            clauses.append("scorer_identity=?")
            parameters.append(scorer_identity)
        rows = self.connection.execute(
            "SELECT * FROM manuscript_collocation_scores WHERE "
            + " AND ".join(clauses)
            + " ORDER BY candidate_id,probe_id",
            tuple(parameters),
        ).fetchall()
        return [
            {**dict(row), "features": json.loads(str(row["features_json"]))}
            for row in rows
        ]

    def save_score(
        self,
        *,
        run_id: str,
        candidate_id: str,
        probe_id: str,
        scorer_type: str,
        scorer_identity: str,
        features: dict[str, Any],
        commit: bool = True,
    ) -> None:
        self.connection.execute(
            """INSERT INTO manuscript_collocation_scores(
                id,run_id,candidate_id,probe_id,scorer_type,scorer_identity,
                features_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id,probe_id,scorer_type,scorer_identity)
            DO UPDATE SET features_json=excluded.features_json,
                          created_at=excluded.created_at""",
            (
                str(uuid.uuid4()),
                run_id,
                candidate_id,
                probe_id,
                scorer_type,
                scorer_identity,
                json.dumps(features, ensure_ascii=False),
                utc_now(),
            ),
        )
        if commit:
            self.connection.commit()

    def save_verdict(
        self,
        *,
        run_id: str,
        candidate_id: str,
        verdict: str,
        rationale: str,
        verifier_identity: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO manuscript_collocation_semantic_verdicts(
                id,run_id,candidate_id,verdict,rationale,verifier_identity,created_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id,verifier_identity)
            DO UPDATE SET verdict=excluded.verdict,rationale=excluded.rationale,
                          created_at=excluded.created_at""",
            (
                str(uuid.uuid4()),
                run_id,
                candidate_id,
                verdict,
                rationale,
                verifier_identity,
                utc_now(),
            ),
        )
        self.connection.commit()

    def save_decision(
        self,
        *,
        run_id: str,
        frame_id: str,
        original_candidate_id: str,
        suggested_candidate_id: str | None,
        decision: str,
        evidence: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """INSERT INTO manuscript_collocation_decisions(
                frame_id,run_id,original_candidate_id,suggested_candidate_id,
                decision,evidence_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(frame_id)
            DO UPDATE SET suggested_candidate_id=excluded.suggested_candidate_id,
                          decision=excluded.decision,
                          evidence_json=excluded.evidence_json,
                          updated_at=excluded.updated_at""",
            (
                frame_id,
                run_id,
                original_candidate_id,
                suggested_candidate_id,
                decision,
                json.dumps(evidence, ensure_ascii=False),
                utc_now(),
                utc_now(),
            ),
        )
        self.connection.commit()

    def save_human_judgment(
        self,
        *,
        run_id: str,
        frame_id: str,
        candidate_id: str,
        judgment: str,
        comment: str,
    ) -> str:
        if judgment not in {"accept", "reject"}:
            raise ValueError("judgment must be accept or reject")
        exists = self.connection.execute(
            """SELECT 1 FROM manuscript_collocation_candidates
            WHERE id=? AND frame_id=? AND run_id=?""",
            (candidate_id, frame_id, run_id),
        ).fetchone()
        if exists is None:
            raise KeyError(candidate_id)
        judgment_id = str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO manuscript_collocation_human_judgments(
                id,run_id,frame_id,candidate_id,judgment,comment,created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (judgment_id, run_id, frame_id, candidate_id, judgment, comment, utc_now()),
        )
        self.connection.commit()
        return judgment_id

    def list_run(self, run_id: str) -> list[dict[str, Any]]:
        frames = []
        for row in self.connection.execute(
            """SELECT * FROM manuscript_collocation_frames
            WHERE run_id=? ORDER BY block_id,sentence_ordinal,predicate_start""",
            (run_id,),
        ):
            frame = json.loads(row["frame_json"])
            candidates = []
            for candidate in self.connection.execute(
                """SELECT * FROM manuscript_collocation_candidates
                WHERE frame_id=? ORDER BY candidate_ordinal""",
                (row["id"],),
            ):
                item = dict(candidate)
                item["scores"] = [
                    {**dict(score), "features": json.loads(score["features_json"])}
                    for score in self.connection.execute(
                        """SELECT * FROM manuscript_collocation_scores
                        WHERE candidate_id=? ORDER BY probe_id""",
                        (candidate["id"],),
                    )
                ]
                verdict = self.connection.execute(
                    """SELECT verdict,rationale,verifier_identity
                    FROM manuscript_collocation_semantic_verdicts
                    WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1""",
                    (candidate["id"],),
                ).fetchone()
                item["semantic_verdict"] = dict(verdict) if verdict else None
                item["human_judgments"] = [
                    dict(judgment)
                    for judgment in self.connection.execute(
                        """SELECT id,judgment,comment,created_at
                        FROM manuscript_collocation_human_judgments
                        WHERE candidate_id=? ORDER BY created_at""",
                        (candidate["id"],),
                    )
                ]
                candidates.append(item)
            decision = self.connection.execute(
                "SELECT * FROM manuscript_collocation_decisions WHERE frame_id=?", (row["id"],)
            ).fetchone()
            decision_value = dict(decision) if decision else None
            if decision_value is not None:
                decision_value["evidence"] = json.loads(decision_value["evidence_json"])
            frames.append(
                {"frame": frame, "candidates": candidates, "decision": decision_value}
            )
        return frames
