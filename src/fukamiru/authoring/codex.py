"""Structured, read-only Codex CLI adapter for note-to-prose authoring."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fukamiru.codex_cli import require_codex_cli, resolve_codex_cli
from fukamiru.config import Settings
from fukamiru.errors import (
    ExternalServiceUnavailable,
    FukamiruError,
    NonRetryableFukamiruError,
)
from fukamiru.llm_observability import extract_codex_usage

_AUTHORING_INVOCATION_OBSERVER: ContextVar[
    tuple[Callable[[dict[str, Any]], None], dict[str, Any]] | None
] = ContextVar("authoring_invocation_observer", default=None)

PROVENANCE = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["memo", "rag", "llm_inference", "user", "mixed"]},
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "verification_status": {
            "type": "string",
            "enum": ["verified", "unverified", "not_applicable"],
        },
    },
    "required": ["source", "source_ids", "verification_status"],
    "additionalProperties": False,
}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


def _array(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


def _string(enum: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "string"}
    if enum:
        result["enum"] = enum
    return result


def _record(properties: dict[str, Any]) -> dict[str, Any]:
    return _object(properties)


def _schema(result_properties: dict[str, Any]) -> dict[str, Any]:
    return _object(result_properties)


def _validate_codex_output_schema(schema: dict[str, Any], path: str = "$") -> None:
    """Check the strict object contract before sending a schema to Codex."""

    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or len(required) != len(set(required))
            or set(required) != set(properties)
            or schema.get("additionalProperties") is not False
        ):
            raise NonRetryableFukamiruError(
                f"Invalid Codex output schema at {path}: object properties and required "
                "must match, with additionalProperties=false"
            )
        for name, child in properties.items():
            _validate_codex_output_schema(child, f"{path}.{name}")
    elif kind == "array":
        _validate_codex_output_schema(schema["items"], f"{path}[]")


STAGE_SCHEMAS: dict[str, dict[str, Any]] = {
    "proofreading_selection": _schema(
        {
            "decisions": _array(
                _record(
                    {
                        "sentence_id": _string(),
                        "selected_candidate_id": _string(),
                        "confidence": _string(["high", "medium", "low"]),
                        "reason_codes": _array(_string()),
                    }
                )
            )
        }
    ),
    "decompose_memo": _schema(
        {
            "propositions": _array(
                _record(
                    {
                        "id": _string(),
                        "text": _string(),
                        "source_span": _string(),
                        "provenance": PROVENANCE,
                    }
                )
            )
        }
    ),
    "classify_propositions": _schema(
        {
            "classifications": _array(
                _record(
                    {
                        "proposition_id": _string(),
                        "type": _string(
                            [
                                "fact",
                                "observation",
                                "interpretation",
                                "hypothesis",
                                "definition",
                                "method",
                                "reservation",
                                "example",
                            ]
                        ),
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "argument_role": _string(
                            ["author_claim", "research_lead", "context", "reservation"]
                        ),
                        "provenance": PROVENANCE,
                    }
                )
            )
        }
    ),
    "reasoning_review": _schema(
        {
            "findings": _array(
                _record(
                    {
                        "id": _string(),
                        "finding_type": _string(
                            [
                                "reasoning_asset",
                                "causal_gap",
                                "comparison_gap",
                                "definition_gap",
                                "evidence_gap",
                                "qualification_need",
                            ]
                        ),
                        "severity": _string(["info", "minor", "major", "critical"]),
                        "proposition_ids": _array(_string()),
                        "description": _string(),
                        "recommended_use": _string(),
                        "requires_research": {"type": "boolean"},
                    }
                )
            ),
            "reasoning_summary": _string(),
        }
    ),
    "research_question": _schema(
        {
            "research_question": _record(
                {
                    "id": _string(),
                    "text": _string(),
                    "scope": _string(),
                    "out_of_scope": _array(_string()),
                    "assumptions": _array(_string()),
                    "provisional_answer": _string(),
                    "research_gaps": _array(_string()),
                    "rival_interpretations": _array(_string()),
                    "provenance": PROVENANCE,
                }
            ),
            "topic_coverage": {
                "type": "array",
                "minItems": 1,
                "items": _record(
                    {
                        "topic_id": _string(),
                        "label": _string(),
                        "proposition_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": _string(),
                        },
                        "disposition": _string(
                            ["central", "supporting", "context", "research_lead", "excluded"]
                        ),
                        "relation_to_question": _string(),
                    }
                ),
            },
            "required_user_information": _array(
                _record(
                    {
                        "question": _string(),
                        "reason": _string(),
                        "default_assumption": _string(),
                        "affected_scope": _string(),
                    }
                )
            ),
        }
    ),
    "claim_tree": _schema(
        {
            "claims": _array(
                _record(
                    {
                        "id": _string(),
                        "level": _string(["main_claim", "subclaim", "supporting_claim"]),
                        "text": _string(),
                        "parent_id": {"type": ["string", "null"]},
                        "proposition_id": {"type": ["string", "null"]},
                        "answers_question": {"type": "boolean"},
                        "research_gap": _string(),
                        "rival_interpretation": _string(),
                        "provenance": PROVENANCE,
                    }
                )
            )
        }
    ),
    "research_query_plan": _schema(
        {
            "plans": _array(
                _record(
                    {
                        "unit_id": _string(),
                        "variants": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 6,
                            "items": _string(),
                        },
                        "subclaims": _array(_string()),
                        "source_kind": _string(["article", "book", "primary", "mixed"]),
                    }
                )
            )
        }
    ),
    "argument_graph": _schema(
        {
            "nodes": _array(
                _record(
                    {
                        "id": _string(),
                        "node_type": _string(
                            [
                                "claim",
                                "evidence",
                                "warrant",
                                "qualifier",
                                "counterexample",
                                "rebuttal",
                            ]
                        ),
                        "text": _string(),
                        "claim_id": {"type": ["string", "null"]},
                        "proposition_id": {"type": ["string", "null"]},
                        "source_span_id": {"type": ["string", "null"]},
                        "provenance": PROVENANCE,
                    }
                )
            ),
            "edges": _array(
                _record(
                    {
                        "id": _string(),
                        "source_id": _string(),
                        "target_id": _string(),
                        "relation": _string(
                            [
                                "supports",
                                "warrants",
                                "qualifies",
                                "challenges",
                                "rebuts",
                                "depends_on",
                                "alternative_to",
                            ]
                        ),
                    }
                )
            ),
            "claim_decisions": _array(
                _record(
                    {
                        "claim_id": _string(),
                        "disposition": _string(["adopt", "narrow", "research_again", "reject"]),
                        "reason": _string(),
                        "counterinterpretation": _string(),
                        "adopted_text": _string(),
                    }
                )
            ),
        }
    ),
    "hypotheses": _schema(
        {
            "candidates": _array(
                _record(
                    {
                        "id": _string(),
                        "text": _string(),
                        "gap_type": _string(),
                        "status": _string(["candidate", "adopted", "rejected"]),
                        "rationale": _string(),
                        "provenance": PROVENANCE,
                    }
                )
            )
        }
    ),
    "discourse_tree": _schema(
        {
            "nodes": _array(
                _record(
                    {
                        "id": _string(),
                        "section_id": {"type": ["string", "null"]},
                        "article_role": _string(["root", "introduction", "body", "conclusion"]),
                        "relation": _string(
                            [
                                "root",
                                "evidence",
                                "explanation",
                                "elaboration",
                                "contrast",
                                "concession",
                                "cause",
                                "condition",
                                "background",
                                "result",
                                "sequence",
                                "summary",
                            ]
                        ),
                        "parent_id": {"type": ["string", "null"]},
                        "child_ids": _array(_string()),
                        "attentional_focus": _string(),
                        "discourse_purpose": _string(),
                        "section_answer": _string(),
                        "claim_ids": _array(_string()),
                    }
                )
            )
        }
    ),
    "manuscript_structure": _schema(
        {
            "sections": _array(
                _record(
                    {
                        "section_id": _string(),
                        "article_role": _string(["introduction", "body", "conclusion"]),
                        "relation": _string(),
                        "reader_purpose": _string(),
                        "claim_ids": _array(_string()),
                        "source_proposition_ids": _array(_string()),
                    }
                )
            )
        }
    ),
    "section_contracts": _schema(
        {
            "sections": _array(
                _record(
                    {
                        "section_id": _string(),
                        "title": _string(),
                        "local_question": _string(),
                        "article_role": _string(["introduction", "body", "conclusion"]),
                        "expected_conclusion": _string(),
                        "section_answer": _string(),
                        "claim_ids": _array(_string()),
                        "evidence_ids": _array(_string()),
                        "warrant_ids": _array(_string()),
                        "qualifiers": _array(_string()),
                        "counterarguments": _array(_string()),
                        "discourse_relation_to_parent": _string(),
                        "previous_section_bridge": _string(),
                        "next_section_bridge": _string(),
                        "required_terms": _array(_string()),
                        "forbidden_topics": _array(_string()),
                        "development_expectations": _record(
                            {
                                "minimum_evidence_items": {"type": "integer", "minimum": 1},
                                "minimum_concrete_examples": {"type": "integer", "minimum": 1},
                                "required_argument_moves": {
                                    "type": "array",
                                    "minItems": 5,
                                    "items": _string(),
                                },
                            }
                        ),
                        "target_length": {"type": ["integer", "null"], "minimum": 1},
                    }
                )
            ),
            "introduction_contract": _record(
                {
                    "section_id": _string(),
                    "genre_profile": _string(),
                    "moves": {
                        "type": "array",
                        "minItems": 5,
                        "items": _record(
                            {
                                "move": _string(
                                    [
                                        "research_object_background",
                                        "problem_or_question",
                                        "prior_research_position",
                                        "positive_purpose_or_thesis",
                                        "scope_and_materials",
                                        "method_or_analytic_axis",
                                        "key_term_definition",
                                        "contribution",
                                        "roadmap",
                                        "scope_qualification",
                                    ]
                                ),
                                "purpose": _string(),
                                "claim_ids": _array(_string()),
                            }
                        ),
                    },
                }
            ),
        }
    ),
    "semantic_paragraphs": _schema(
        {
            "sections": _array(
                _record(
                    {
                        "section_id": _string(),
                        "paragraphs": _array(
                            _record(
                                {
                                    "rhetorical_moves": _array(_string()),
                                    "claim_ids": _array(_string()),
                                    "evidence_ids": _array(_string()),
                                    "central_claim": _string(),
                                    "consequence": _string(),
                                    "development_instruction": _string(),
                                }
                            )
                        ),
                    }
                )
            )
        }
    ),
    "draft": _schema(
        {
            "sections": _array(
                _record(
                    {
                        "section_id": _string(),
                        "paragraphs": {
                            "type": "array",
                            "items": _record(
                                {
                                    "move_indices": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {"type": "integer", "minimum": 1},
                                    },
                                    "evidence_ids": _array(_string()),
                                    "text": _string(),
                                }
                            ),
                        },
                        "claim_ids": _array(_string()),
                        "evidence_ids": _array(_string()),
                        "article_role": _string(["introduction", "body", "conclusion"]),
                        "realized_evidence_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": _string(),
                        },
                        "realized_warrant_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": _string(),
                        },
                        "realized_qualifier_ids": {
                            "type": "array",
                            "items": _string(),
                        },
                        "evidence_treatment": {
                            "type": "array",
                            "minItems": 1,
                            "items": _record(
                                {
                                    "evidence_id": _string(),
                                    "claim_id": _string(),
                                    "basis": _string(
                                        [
                                            "external_source",
                                            "memo_close_reading",
                                            "llm_inference",
                                            "user_assertion",
                                        ]
                                    ),
                                    "verification_status": _string(
                                        ["verified", "unverified", "not_applicable"]
                                    ),
                                    "prose_treatment": _string(
                                        [
                                            "externally_supported_fact",
                                            "attributed_close_reading",
                                            "qualified_interpretation",
                                            "attributed_user_statement",
                                            "attributed_scholarship",
                                        ]
                                    ),
                                    "qualifier_id": {"type": ["string", "null"]},
                                    "citation_purpose": _string(),
                                    "attribution_sentence": _string(),
                                    "purpose_clause": _string(),
                                }
                            ),
                        },
                        "concrete_examples": {"type": "array", "minItems": 1, "items": _string()},
                        "new_propositions": _array(
                            _record({"id": _string(), "text": _string(), "provenance": PROVENANCE})
                        ),
                    }
                )
            )
        }
    ),
    "audit": _schema(
        {
            "findings": _array(
                _record(
                    {
                        "id": _string(),
                        "severity": _string(["info", "minor", "major", "critical"]),
                        "finding_type": _string(),
                        "message": _string(),
                        "affected_claim_ids": _array(_string()),
                        "affected_section_ids": _array(_string()),
                        "repair_scope": _string(
                            ["none", "local_section", "plan_revision", "user_input"]
                        ),
                    }
                )
            )
        }
    ),
    "polish": _schema(
        {
            "sections": _array(
                _record(
                    {
                        "section_id": _string(),
                        "text": _string(),
                        "changed_claim_ids": {"type": "array", "maxItems": 0, "items": _string()},
                    }
                )
            )
        }
    ),
}


STAGE_GUIDANCE = {
    "proofreading_selection": (
        "Select independently between ORIGINAL and the supplied Japanese proofreading "
        "candidates. Return exactly one decision for every sentence_id. Prefer ORIGINAL "
        "whenever there is doubt, a tie, or no actual improvement. Routine Japanese "
        "copyediting is an intended automatic action: select a clearly better candidate for "
        "word choice, particles, redundancy, syntax, sentence connection, reference clarity, "
        "readability, or academic register even when the correction is stylistic. Do not keep "
        "ORIGINAL merely because the improvement is stylistic. Select a candidate with "
        "confidence=high only while preserving the original "
        "meaning, factual scope, modality and uncertainty, attribution, quoted status, "
        "technical terms, discourse role, and academic register. A change from possibility "
        "to fact, question to theme, description to obligation, person or work name to a "
        "common noun, or local object to a broader/narrower object must retain ORIGINAL. "
        "Use selected_candidate_id=ORIGINAL for every retained original. A candidate selected "
        "with medium or low confidence will not be applied automatically. Use concise stable "
        "reason codes and do not rewrite or invent text."
    ),
    "decompose_memo": (
        "Split the memo into the smallest meaningful atomic propositions. Preserve exact source "
        "spans. Ignore its headings, section boundaries, and ordering as an outline. Exclude "
        "document-level editorial scaffolding such as descriptions of how the memo was prepared, "
        "tag legends, revision instructions, and status notes; do not turn that scaffolding into "
        "propositions. Retain annotations that identify source uncertainty, verification gaps, "
        "factual risks, or unresolved author choices, because those are substantive reservations."
    ),
    "classify_propositions": (
        "Assign exactly one proposition type to every supplied proposition; keep its proposition "
        "ID unchanged. Set argument_role separately: author_claim for the author's own "
        "assertion, research_lead for a search clue or open question, context for framing, "
        "and reservation for a limitation. A research lead is not already a paper claim. "
        "memo_annotations and memo_annotation_kinds are immutable author-supplied "
        "source constraints, not prose to discard or facts to verify."
    ),
    "reasoning_review": (
        "Diagnose the memo's usable reasoning assets and its causal, comparative, definitional, "
        "evidential, and qualification gaps before choosing the research question. Refer only "
        "to supplied proposition IDs. Preserve useful material; do not turn this diagnostic "
        "into reader-facing prose or invent facts."
    ),
    "research_question": (
        "Formulate exactly one focused research question, its scope, and what falls outside "
        "that scope. Give a provisional answer, the gaps research must resolve, and rival "
        "interpretations. The question must be broad enough to organize every substantive topic in "
        "the supplied propositions unless the user explicitly requested a narrower paper; do "
        "not improve focus by silently discarding later parts of the memo. Partition every "
        "supplied proposition exactly once into topic_coverage. Mark each topic central, "
        "supporting, context, research_lead, or excluded and explain its disposition. "
        "Research leads guide retrieval without becoming paper claims. This is a "
        "coverage map, not an instruction to reuse the memo's headings or order. Every memo "
        "provenance source_id and every topic proposition_id must be an ID from the supplied "
        "propositions; never cite chunk labels, field names, or invented IDs. Add "
        "required_user_information only for unresolved authorial choices that materially affect "
        "scope or argument direction. Do not ask the user for facts that retrieval can verify."
        " Coverage does not require enumerating every topic in the question or scope sentence. "
        "Distinguish works and genres being compared from distribution venues, platforms, "
        "and production conditions; state their different roles instead of listing them "
        "under one comparison predicate."
    ),
    "claim_tree": (
        "Build one rooted hierarchy with one main_claim answering the question, then subclaims "
        "and supporting_claims. Treat topic_coverage as a preservation contract: every central "
        "or supporting topic needs a distinct subclaim branch. Context and research leads "
        "need not become claims; use them to orient or direct retrieval. State a provisional "
        "answer, unresolved research gap, and rival interpretation in the claim metadata. "
        "Aggregate propositions when they perform the same "
        "argumentative work, but never omit a topic merely to shorten the paper. Use stable unique "
        "IDs and parent links. Preserve the force of "
        "linked memo annotations: other_speaker is not the user's assertion, needs_review is an "
        "unresolved reservation, and added_theory/self_citation cannot be treated as verified "
        "merely because it appears in the memo."
    ),
    "research_query_plan": (
        "Convert each supplied atomic-proposition verification target into one to six short "
        "literature retrieval queries. Include a supporting and a challenging or rival "
        "query. Return keyword expressions (two to five concepts), at most 96 characters "
        "and 12 whitespace-separated words per variant. Never return explanatory sentences "
        "or Japanese connective phrases such as に関する, について, における, としての. "
        "For Japanese targets include Japanese and English variants, normally three to "
        "six in total. Separate narrower and broader queries: do not require every facet "
        "of the claim in one query. Keep proper names and work titles intact; use English "
        "research concepts for international databases and Japanese terms for NDL. "
        "Search for rival evidence using neutral alternative concepts, not a sentence "
        "asserting the desired conclusion. Include a supporting and a challenging or rival "
        "query where the available variants permit it. Preserve unit_id exactly. Use concrete "
        "names, periods, "
        "media, and "
        "research concepts; do not repeat the full claim sentence as a search query. Read "
        "source_context and source_hints: retain the memo's explicit titles, definitions and "
        "self-citations. Separate locating that work from searching its internal concepts. "
        "Do not replace named concepts with generic themes. This is a "
        "mechanical retrieval-planning task. When retry_reason is supplied, propose only "
        "new variants that address the reported support gap. Shorten overconstrained queries "
        "and vary research concepts using synonyms rather than repeating all claim facets. "
        "Include scholarly books and primary sources when appropriate; preserve the requested "
        "source_kind instead of treating every source as a journal article. "
        "Include a rival or contrary "
        "source query and never repeat previous_variants. Do not adjudicate whether the "
        "claim is true and do "
        "not invent bibliographic facts."
    ),
    "argument_graph": (
        "Represent claims, evidence, warrants, qualifiers, counterexamples, and rebuttals as "
        "nodes and typed edges. Create exactly one claim node for every supplied claim. For every "
        "claim, attach at least one evidence node and one warrant; add a qualifier only "
        "when a real scope limit or uncertainty warrants it, and add at least one "
        "counterexample or rebuttal; every attached node must carry that claim's claim_id and an "
        "edge connecting it to the claim node. Distinguish three evidence bases explicitly in "
        "the node text and provenance: (1) external-source evidence comes from an identified "
        "retrieved/source record and may be marked verified only when the cited source span or "
        "record actually supports the statement; retrieval alone is not verification; (2) memo "
        "close-reading uses memo provenance and the exact proposition/source span, and supports "
        "claims about what the note says, frames, or implies, not independent facts about the "
        "world; (3) LLM inference uses llm_inference provenance and remains unverified. Never "
        "upgrade memo interpretation or model inference into externally verified fact. Any "
        "preferred, interpretive, or unverified claim must have explicit provenance plus an "
        "attached qualifier/limitation and a qualifies edge when its scope actually needs "
        "limiting. Keep that qualification precise. Preserve memo_annotation constraints "
        "supplied with propositions "
        "and claim contracts: attribute other_speaker material to that speaker, retain "
        "needs_review "
        "as a visible limitation, and use added_theory/self_citation only after bibliographic "
        "verification. A citation_reference or author_supplied_reference whose bibliographic "
        "identity is render-ready may be connected to its matching annotated proposition as "
        "attributed scholarship, using its evidence ID in provenance.source_ids. That verifies "
        "only the bibliographic identity, not the work's substantive support for the claim, so "
        "keep the argument evidence unverified unless a separately accepted external evidence "
        "record supplies direct support. Do not use references marked final_revision_blocked. "
        "Use accepted evidence IDs in provenance.source_ids when applicable, and use only IDs "
        "present in context. For every claim return a claim_decisions entry: adopt, narrow, "
        "research_again, or reject, with the competing interpretation and an explicit reason. "
        "The decision is the AI's provisional judgment for human review, never a final human "
        "support verdict. Follow evidence.atomic_contracts for each proposition independently: "
        "assert_with_citation requires an evidence node carrying that exact proposition_id and an "
        "accepted evidence ID; attribute_to_memo must be visibly attributed to the memo; "
        "attribute_theory_with_citation must name the cited scholar or work and state why it is "
        "being cited; qualified_inference must remain explicitly qualified; omit creates no "
        "factual evidence node. A claim wrapper may combine these different treatments but never "
        "inherits "
        "verified status from only one of its atomic propositions."
    ),
    "hypotheses": (
        "Generate candidates for missing causal links, comparisons, rival explanations, and "
        "boundary conditions. Keep them candidates unless the provided material justifies "
        "adoption. When research_problem is supplied (V31), a coherent relation inferred "
        "from neighboring claims may be adopted as the article's own interpretation even "
        "if no source states it directly. Keep the reasoning distinct from source facts and "
        "do not invent a source, event, quotation, or externally checkable detail."
    ),
    "discourse_tree": (
        "Plan one discourse root with a null section_id and child section nodes with non-null "
        "section_ids. Assign article_role, discourse_purpose, and the exact claim_ids advanced "
        "by each node. Use explicit rhetorical relations and attentional focus. This is an "
        "independent discourse reconstruction, not a conversion of the claim tree into headings: "
        "reorder, combine, revisit, or distribute claims when that creates a clearer reader-facing "
        "sequence of background, evidence, explanation, contrast, concession, result, and "
        "synthesis. Give every section a declarative section_answer that advances the "
        "provisional conclusion. Organize around the argument, disregarding original memo "
        "sectioning. Build a "
        "complete article arc with one introduction, multiple developed body sections, and one "
        "conclusion. Do not force every branch into a synthesis or contrast phase. "
        "When several claim branches are relevant to one reader-facing section "
        "question, they may share a body section; their co-location does not mean their relation "
        "has been resolved. Preserve each branch's distinct claim and contribution. In V31 "
        "(when research_problem is supplied), when their relationship is the section's "
        "analytical work, choose a reasoned authorial comparison, mechanism, or synthesis "
        "from the claims and neighboring argument even if no source states it verbatim. "
        "The section_answer should state that interpretation as a result, while source facts "
        "remain separately attributed. If no coherent interpretation can be made, revise the "
        "section boundary instead of filling every paragraph with unresolved-relation notices. "
        "Outside V31, state a relation only when supplied claims and evidence warrant it; "
        "otherwise keep branches parallel, qualified, or explicitly unresolved. Do not make "
        "one section per claim and do not compress distinct argumentative work into a generic "
        "summary section. When the payload contains research_problem (V31), top-level branches "
        "may share a body phase where a coherent local question makes them readable together; "
        "supporting claims remain traceable to their parent branch, and no branch needs a "
        "separate section solely because it was promoted in the claim hierarchy. Keep sections "
        "readable through their question, answer, and paragraph development rather than a hard "
        "claim-count quota. Section titles should state a reader-facing question, "
        "distinction, or rhetorical function; do not copy or lightly paraphrase claim-tree "
        "labels as titles."
    ),
    "manuscript_structure": (
        "Convert the discourse tree into a reader-facing variable section sequence. Preserve "
        "every non-root discourse section ID exactly once and preserve its article role and "
        "claim coverage. Use the source preservation map to name the propositions realized in "
        "each section. Do not restore the memo's original headings or impose uniform sections. "
        "When research_problem is supplied (V31), preserve each discourse section's reasoned "
        "interpretive relation in reader_purpose and relation rather than reducing it to a "
        "list of parallel topics merely because no source states the relation directly."
    ),
    "section_contracts": (
        "Create a contract for every planned section: question, declarative section_answer, "
        "conclusion, evidence, claims, "
        "and bridges. Treat the discourse tree's article_role and claim_ids as locked: copy them "
        "exactly into the matching section contract. Return every non-root discourse section_id "
        "exactly once and invent no "
        "section IDs. Titles must express the reader-facing rhetorical task, not repeat or "
        "lightly paraphrase a claim-tree label. Claim IDs may be combined, reordered, revisited, "
        "or distributed across sections to serve reader comprehension; do not impose a one-claim "
        "per-section mapping. Every section must cite at least one supplied claim, evidence "
        "node, and "
        "warrant node; cover every supplied claim across the contracts. IDs must refer to "
        "supplied structures. Build an article progression with one introduction, at least two "
        "developed body sections, and one conclusion; do not collapse the paper into an intro, "
        "one body paragraph, and a conclusion. Mark each section's article_role. For every "
        "section, include development_expectations with minimum_evidence_items >= 1, "
        "minimum_concrete_examples >= 1, and at least five required_argument_moves selected "
        "for that section (for example: orient the reader, state a local claim, present concrete "
        "evidence, explain the warrant, qualify or address a counterargument, and state an "
        "implication or transition). These are structural development requirements, not word, "
        "character, page, or paragraph-count targets. Set target_length only when the input "
        "contains a positive user-specified requested_total_length, and allocate exactly that "
        "requested total across sections. Otherwise target_length must be null. Never infer, "
        "invent, or impose an arbitrary fixed total length. Also create one explicit, "
        "genre-sensitive introduction_contract for the introduction section. Its ordered moves "
        "must first establish the time-bound research object and background, then state the "
        "reader's present problem or question, then give the positive purpose or thesis, scope "
        "and materials, and method or analytic axis. When the source material contrasts a past "
        "media arrangement with a present condition in which its effects are obscured, that "
        "historical-to-present loss must be the problem_or_question move rather than being "
        "postponed to a later section. Theory, a generic example, a limitation, or a definition "
        "must not replace the opening orientation. Each move must state its concrete purpose and "
        "the supplied claim IDs it previews or advances."
    ),
    "semantic_paragraphs": (
        "Plan each section as non-uniform semantic paragraphs. Return every section contract "
        "exactly once. Decide paragraph boundaries from semantic and discourse completeness, "
        "not from the number of required argument moves. Map paragraphs and moves many-to-many: "
        "one paragraph may realize several moves, and one move may be developed across several "
        "paragraphs. In rhetorical_moves, use only the exact required_argument_moves strings from "
        "that section contract; repeat an exact move string across adjacent paragraph records when "
        "one move spans them. Do not generate numeric move indices: the system derives them "
        "deterministically from those exact move strings. Every required move must be covered at "
        "least once. Connect the relevant claims and evidence "
        "and give each paragraph a central_claim and consequence, so the prose develops "
        "an inference rather than a checklist. Explain how each paragraph develops its "
        "mapped moves. Allow short transition "
        "paragraphs and substantially longer evidence or explanation paragraphs when their "
        "discourse work calls for them. Do not assign paragraph-count, character, or word quotas."
    ),
    "draft": (
        "Write each section as readable, connected academic prose from its contract and cited "
        "structures, with substantive development rather than a compressed summary. Across the "
        "article, realize an introduction that establishes the problem and question, multiple "
        "developed body sections that advance the claims, and a conclusion that answers the "
        "question and synthesizes the argument. Each section must make its local claim, use at "
        "least one contract-listed concrete evidence item and one concrete example or case, "
        "explain the warrant linking evidence to claim, and express a boundary where the "
        "argument genuinely needs one. Distinguish evidence in prose: an external source "
        "supports an externally "
        "stated fact only when its identified source record/span is verified and cited; RAG "
        "retrieval alone is not verification. Memo close-reading is an internal provenance class "
        "for what the source material says, frames, or implies; it is not an independent external "
        "fact and the provenance label belongs in structured metadata, not reader-facing prose. "
        "Rewrite such material as a directly stated but appropriately qualified authorial "
        "interpretation. Never refer to the memo, supplied memo, input text, source notes, the "
        "user, or the drafting process in prose unless that document or process is itself the "
        "declared object of research. LLM inference stays unverified. Treat every "
        "preferred, interpretive, or unverified claim as an authorial argument with the "
        "appropriate scope; never render it as externally verified fact. State the paper's "
        "provisional conclusion directly. Do not repeat internal labels such as unverified, "
        "future work, or reference candidate in reader-facing prose. Keep review status in "
        "metadata. Treat user assertions as the paper author's propositions; "
        "qualify them when needed but never write 'the user says'. Obey memo_annotation "
        "constraints in the locked propositions and evidence contracts: other_speaker content "
        "must not be attributed to the user, needs_review must remain qualified, and added_theory "
        "or self_citation may be used as scholarship only after bibliographic verification. "
        "For evidence whose allowed_use is attributed_scholarship_citation, never attach a bare "
        "footnote to an otherwise unrelated proposition. The reader-facing sentence must name "
        "the scholar or work and explain which concept, argument, comparison, method, or "
        "counterposition is being borrowed and why it matters to the local claim. "
        "Every ID listed in a section contract's required_scholarship_evidence_ids is mandatory: "
        "assign it to at least one paragraph, include it in realized_evidence_ids, and write its "
        "visible attribution and local argumentative purpose in that paragraph. Return one "
        "evidence_treatment record per used evidence ID, and exactly one record per distinct ID; "
        "never duplicate a treatment when the same evidence is mentioned in more than one "
        "paragraph. Name its claim, basis, verification "
        "status, prose treatment, and qualifier ID when the claim is interpretive or unverified. "
        "For every evidence treatment, copy one exact claim-bearing sentence from a paragraph "
        "that lists that evidence ID into support_sentence. This is an internal evidence-to-prose "
        "anchor and does not create a footnote. Several memo or inference evidence IDs may share "
        "one support_sentence without creating several notes. For attributed scholarship set "
        "prose_treatment=attributed_scholarship and also return the citation_purpose, an "
        "attribution_sentence describing the intended reader-facing source mention, and a "
        "purpose_clause describing the source's local argumentative use. These metadata fields "
        "are guidance, not text that must be copied verbatim. In the actual paragraph, write "
        "naturally and include a unique complete sentence that names the cited author or work "
        "and makes its local purpose clear; do not rely on metadata alone or attach a bare "
        "footnote. Only attributed scholarship "
        "can create a bibliography footnote. For other evidence, return empty strings for those "
        "three citation fields. "
        "All stable IDs and schema labels belong only in the structured JSON metadata. Never "
        "print claim, evidence, warrant, qualifier, proposition, plan, or section IDs in the "
        "reader-facing section text, including parenthetical forms such as (c10-e). For Japanese "
        "output, express memo-based interpretation as independent academic prose; never expose "
        "internal labels such as memo_close_reading or the English phrase close reading, and "
        "never use mechanical scaffold phrases such as 提示メモ, このメモ, or 入力文. "
        "Realize every required argument move through the semantic paragraph plan without "
        "turning the move list into a paragraph template. Map moves and paragraphs many-to-many: "
        "a paragraph may combine several tightly related moves, and a substantial move may span "
        "several paragraphs. Preserve the planned overlap in move_indices and cover every "
        "required move at least once. Choose paragraph count and length from the local discourse "
        "task; short transitions and longer evidence or explanation paragraphs are both valid. "
        "Do not use paragraph count or character length as a completeness proxy, and do not use "
        "bullet-like fragments. Each paragraph must list exactly the evidence IDs it realizes; "
        "these paragraph-level IDs determine where a later footnote may be placed. Return "
        "reader-facing prose only in the paragraphs array and do "
        "not also return a duplicate combined text field. Report the article "
        "role, realized evidence/warrant/qualifier IDs, "
        "and concrete examples in the structured output. Do not invent a fixed total word, "
        "character, page, or whole-document paragraph count; length is user-driven only when "
        "explicitly specified, otherwise satisfy structural development minimums. Do not "
        "inherit memo "
        "headings "
        "or section order. When target_section_ids is present, return every target exactly once "
        "and return no other section; use the complete locked plan only as read-only global and "
        "transition context. Report IDs used and any new propositions with provenance."
        " IDs created under new_propositions are proposed statements only. Never copy a "
        "new_propositions ID into paragraph evidence_ids, section evidence_ids, "
        "realized_evidence_ids, or evidence_treatment. Those fields may contain only evidence "
        "IDs supplied by the target contract or evidence projection. When restart_findings are "
        "present, prevent every listed defect in the regenerated draft. If the target evidence "
        "projection lacks support requested by a finding, omit the unsupported factual detail or "
        "rewrite it as a comparison procedure without asserting the missing fact. Never attach "
        "nearby but propositionally mismatched evidence. A URL or record that appears only under "
        "new_propositions remains a proposed source and is not an accepted evidence ID."
    ),
    "audit": (
        "Inspect structural and semantic fidelity: claim/topic drift, unsupported claims, "
        "missing warrants, evidence mismatch, lost qualifiers, omitted counterarguments, weak "
        "transitions, and contract violations. Compare each drafted section's actual conclusion "
        "with its declarative section_answer, and each drafted paragraph's inference with its "
        "planned central_claim and consequence. Report a conclusion_mismatch when prose changes "
        "the answer instead of developing it; do not demand verbatim wording or paragraph counts. "
        "Compare the introduction against the explicit "
        "introduction_contract and report a major structure_invalid finding when it does not "
        "orient a first-time reader through the research object/background, present problem, "
        "positive purpose or thesis, scope/materials, and analytic axis in the planned order, or "
        "when it opens with detached theory or a generic example. Treat a scholarship footnote "
        "without a visible author/work attribution and an explicit local reason for using that "
        "source as a major citation_overclaim finding. Treat reader-facing references to the "
        "supplied "
        "memo, input text, user, or drafting scaffold as major defects unless the document itself "
        "is the declared research object. Flag repeated status language such as 未確認, "
        "今後検討, or 参照候補 when it replaces an argued conclusion. Preserve genuine "
        "scope limits, and leave the ultimate semantic support decision to human review. Every "
        "evidence treatment has a support_sentence used only to map internal provenance to "
        "prose. Multiple non-scholarship treatments may share one support_sentence and do not "
        "thereby create multiple footnotes. Only prose_treatment=attributed_scholarship can "
        "render a footnote; apply the one-note-per-sentence-or-comma-clause rule only to those "
        "records. "
        "Return actionable findings only."
    ),
    "polish": (
        "Improve local wording, repetition, grammar, and cohesion while preserving all claims, "
        "evidence, qualifiers, citations, and section conclusions. changed_claim_ids must "
        "remain empty. When target_section_ids is present, return every target exactly once and "
        "return no other section; adjacent_sections are read-only transition context."
    ),
}


# V30 contracts remain available for historical Runs. New Runs use distinct stage
# names so a saved V30 response is never interpreted using a revised JSON schema.
STAGE_SCHEMAS["research_problem_v31"] = _schema(
    {
        "research_problem": _record(
            {
                "research_question": _string(),
                "prior_positions": _array(
                    _record({"proposition": _string(), "source_ids": _array(_string())})
                ),
                "problematisation": _record(
                    {
                        "type": _string(
                            [
                                "contradiction",
                                "gap",
                                "unexplained_case",
                                "theoretical_tension",
                                "assumption_challenge",
                                "source_internal",
                                "other",
                            ]
                        ),
                        "description": _string(),
                        "evidence_ids": _array(_string()),
                        "proposition_ids": _array(_string()),
                    }
                ),
                "provisional_answer": _string(),
                "contribution_target": _string(),
            }
        )
    }
)
STAGE_SCHEMAS["argument_graph_v31"] = deepcopy(STAGE_SCHEMAS["argument_graph"])
STAGE_SCHEMAS["argument_graph_v31"]["properties"]["claim_scopes"] = _array(
    _record(
        {
            "claim_id": _string(),
            "proposition": _string(),
            "scope_conditions": _array(_string()),
            "epistemic_status": _string(
                ["established", "supported_interpretation", "provisional", "unresolved"]
            ),
            "evidence_basis_ids": _array(_string()),
        }
    )
)
STAGE_SCHEMAS["argument_graph_v31"]["required"].append("claim_scopes")
STAGE_SCHEMAS["defense_obligations_v31"] = _schema(
    {
        "obligations": _array(
            _record(
                {
                    "id": _string(),
                    "target_claim_id": _string(),
                    "challenge": _string(),
                    "basis_ids": _array(_string()),
                    "materiality": _string(),
                    "response": _string(),
                    "resolution_location": _string(
                        [
                            "same_section",
                            "later_section",
                            "note_candidate",
                            "scope_absorbed",
                            "unresolved",
                        ]
                    ),
                    "disposition": _string(
                        ["body", "note_candidate", "scope_absorbed", "internal_only", "drop"]
                    ),
                    "source_argument_node_ids": _array(_string()),
                }
            )
        )
    }
)
STAGE_SCHEMAS["section_contracts_v31"] = deepcopy(STAGE_SCHEMAS["section_contracts"])
_v31_sections = STAGE_SCHEMAS["section_contracts_v31"]["properties"]["sections"]["items"]
_v31_section_fields = _v31_sections["properties"]
_v31_section_fields.update(
    {
        "section_task": _string(),
        "section_output": _string(),
        "section_output_contract": _record(
            {
                "proposition": _string(),
                "output_type": _string(
                    [
                        "documented_result",
                        "interpretive_result",
                        "provisional_result",
                    ]
                ),
                "required_evidence_level": _string(
                    ["direct", "traceable", "reviewable"]
                ),
            }
        ),
        "thesis_link": _string(),
        "required_input_section_ids": _array(_string()),
        "defense_obligation_ids": _array(_string()),
    }
)
_v31_sections["required"].extend(
    [
        "section_task",
        "section_output",
        "section_output_contract",
        "thesis_link",
        "required_input_section_ids",
        "defense_obligation_ids",
    ]
)
_v31_development = _v31_section_fields["development_expectations"]["properties"]
_v31_development["minimum_evidence_items"]["minimum"] = 0
_v31_development["minimum_concrete_examples"]["minimum"] = 0
_v31_development["required_argument_moves"]["minItems"] = 0
_v31_development["required_argument_moves"]["items"] = _string()
_v31_intro = STAGE_SCHEMAS["section_contracts_v31"]["properties"]["introduction_contract"]
_v31_intro["properties"]["moves"]["minItems"] = 0
_v31_intro["properties"]["moves"]["items"]["properties"]["move"] = _string()
_v31_intro["properties"].update(
    {
        "research_question": _string(),
        "documented_prior_positions": _array(
            _record({"proposition": _string(), "source_ids": _array(_string())})
        ),
        "problematisation": _string(),
        "provisional_answer": _string(),
        "contribution_target": _string(),
        "roadmap_section_ids": _array(_string()),
    }
)
_v31_intro["required"].extend(
    [
        "research_question",
        "documented_prior_positions",
        "problematisation",
        "provisional_answer",
        "contribution_target",
        "roadmap_section_ids",
    ]
)
STAGE_SCHEMAS["section_contracts_v31"]["properties"]["conclusion_contract"] = _record(
    {
        "section_id": _string(),
        "research_question": _string(),
        "established_section_ids": _array(_string()),
        "synthesis_goal": _string(),
        "contribution_target": _string(),
        "scope": _string(),
        "unresolved_questions": _array(_string()),
    }
)
STAGE_SCHEMAS["section_contracts_v31"]["required"].append("conclusion_contract")
STAGE_SCHEMAS["semantic_paragraphs_v31"] = deepcopy(STAGE_SCHEMAS["semantic_paragraphs"])
_reader_brief_item_v31 = _record({"id": _string(), "text": _string()})
STAGE_SCHEMAS["naturalize_reader_brief_v31"] = _schema({
    "sections": _array(_record({
        "section_id": _string(),
        "section_result": _string(),
        "framing": _string(),
        "claims": _array(deepcopy(_reader_brief_item_v31)),
        "evidence": _array(deepcopy(_reader_brief_item_v31)),
        "warrants": _array(deepcopy(_reader_brief_item_v31)),
    })),
})
STAGE_SCHEMAS["audit_reader_brief_v31"] = _schema({
    "sections": _array(_record({
        "section_id": _string(),
        "meaning_preserved": _string(["true", "marginal"]),
        "natural_japanese": {"type": "boolean"},
        "internal_language_absent": {"type": "boolean"},
        "marginal_inferences": _array(_record({
            "source_field": _string([
                "section_result", "framing", "claims", "evidence", "warrants"
            ]),
            "source_item_id": _string(),
            "field": _string(["section_result", "framing", "claims", "evidence", "warrants"]),
            "item_id": _string(),
            "source_excerpt": _string(),
            "candidate_excerpt": _string(),
            "reason": _string(),
        })),
        "japanese_issues": _array(_record({
            "field": _string(["section_result", "framing", "claims", "evidence", "warrants"]),
            "item_id": _string(),
            "excerpt": _string(),
            "reason": _string(),
            "suggested_rewrite": _string(),
        })),
        "internal_issues": _array(_record({
            "field": _string(["section_result", "framing", "claims", "evidence", "warrants"]),
            "item_id": _string(),
            "excerpt": _string(),
            "reason": _string(),
            "suggested_rewrite": _string(),
        })),
        "reason": _string(),
    })),
})
_v31_paragraph = STAGE_SCHEMAS["semantic_paragraphs_v31"]["properties"]["sections"]["items"][
    "properties"
]["paragraphs"]["items"]["properties"]
_v31_paragraph["central_claim"] = {"type": ["string", "null"]}
_v31_paragraph["consequence"] = {"type": ["string", "null"]}
_v31_paragraph["rhetorical_moves"]["items"] = _string()
for _role in ("introduction", "body", "conclusion"):
    _stage = f"draft_{_role}_v31"
    STAGE_SCHEMAS[_stage] = deepcopy(STAGE_SCHEMAS["draft"])
    _draft_paragraph_schema = STAGE_SCHEMAS[_stage]["properties"]["sections"][
        "items"
    ]["properties"]["paragraphs"]["items"]
    _draft_paragraph = _draft_paragraph_schema["properties"]
    _draft_paragraph["move_indices"]["minItems"] = 0
    _draft_paragraph["citation_mentions"] = _array(
        _record({"reference_id": _string(), "text": _string()})
    )
    _draft_paragraph_schema["required"].append("citation_mentions")
    _draft_section = STAGE_SCHEMAS[_stage]["properties"]["sections"]["items"]["properties"]
    _draft_section["inference_ledger"] = _array(_record({
        "id": _string(),
        "judgment": _string(),
        "reasoning": _string(),
        "basis_ids": _array(_string()),
        "claim_id": {"type": ["string", "null"]},
        "support_relation": _string([
            "inferred_from_supplied_material",
            "model_judgment_without_direct_statement",
        ]),
        "paragraph_index": {"type": "integer", "minimum": 1},
        "anchor_excerpt": _string(),
    }))
    STAGE_SCHEMAS[_stage]["properties"]["sections"]["items"]["required"].append(
        "inference_ledger"
    )
    _draft_section["concrete_examples"]["minItems"] = 0
    _draft_treatment = _draft_section["evidence_treatment"]["items"]
    _draft_treatment["properties"]["support_sentence"] = _string()
    _draft_treatment["properties"]["support_paragraph_index"] = {
        "type": ["integer", "null"], "minimum": 1,
    }
    _draft_treatment["properties"]["support_sentence_index"] = {
        "type": ["integer", "null"], "minimum": 1,
    }
    _draft_treatment["required"] = list(_draft_treatment["properties"])
    if _role != "body":
        for _field in (
            "realized_evidence_ids",
            "realized_warrant_ids",
            "evidence_treatment",
        ):
            _draft_section[_field]["minItems"] = 0
STAGE_SCHEMAS["draft_conclusion_v31"]["properties"]["conclusion_synthesis"] = _record(
    {
        "final_answer": _string(),
        "synthesis": _string(),
        "contribution": _string(),
        "established_section_ids": _array(_string()),
        "scope": _string(),
        "unresolved_questions": _array(_string()),
        "new_evidence_ids": _array(_string()),
        "new_evidence_rhetorical_reason": _string(),
    }
)
STAGE_SCHEMAS["draft_conclusion_v31"]["required"].append("conclusion_synthesis")
STAGE_SCHEMAS["audit_v31"] = deepcopy(STAGE_SCHEMAS["audit"])
STAGE_SCHEMAS["audit_v31"]["properties"]["section_reviews"] = _array(
    _record(
        {
            "section_id": _string(),
            "article_role": _string(["introduction", "body", "conclusion"]),
            "status": _string(["clean", "findings"]),
            "finding_ids": _array(_string()),
        }
    )
)
STAGE_SCHEMAS["audit_v31"]["required"].append("section_reviews")
STAGE_SCHEMAS["repair_citation_paragraph_v31"] = _schema(
    {
        "text": _string(),
        "citation_mentions": _array(
            _record({"reference_id": _string(), "text": _string()})
        ),
    }
)
STAGE_SCHEMAS["polish_v31"] = deepcopy(STAGE_SCHEMAS["polish"])
STAGE_SCHEMAS["diagnose_body_prose_v31"] = _schema({
    "paragraphs": _array(_record({
        "paragraph_id": _string(),
        "issues": _array(_record({
            "issue_id": _string(),
            "category": _string([
                "enumeration", "writer_agency", "japanese_prose",
                "analysis_stall", "internal_language",
            ]),
            "excerpt": _string(),
            "description": _string(),
            "items": _array(_string()),
            "context_relation": _string(),
        })),
    })),
})
STAGE_SCHEMAS["extract_paragraph_meaning_v31"] = _schema({
    "paragraphs": _array(_record({
        "paragraph_id": _string(),
        "topic": _string(),
        "central_judgment": _string(),
        "supporting_observations": _array(_string()),
        "inferences": _array(_string()),
        "limits": _array(_string()),
        "editorial_only": _array(_string()),
        "citation_role": _string(),
    })),
})
STAGE_SCHEMAS["redraft_body_prose_v31"] = _schema({
    "paragraphs": _array(_record({
        "paragraph_id": _string(),
        "text_slots": _array(_string()),
        "addressed_issue_ids": _array(_string()),
        "review_reason": _string(),
    })),
})
STAGE_SCHEMAS["compose_body_prose_v31"] = deepcopy(
    STAGE_SCHEMAS["redraft_body_prose_v31"]
)
STAGE_SCHEMAS["polish_final_japanese_v31"] = _schema({
    "paragraphs": _array(_record({
        "paragraph_id": _string(),
        "text_slots": _array(_string()),
    })),
})
STAGE_SCHEMAS["audit_redrafted_prose_v31"] = _schema({
    "judgments": _array(_record({
        "paragraph_id": _string(),
        "content_preserved": {"type": "boolean"},
        "enumeration_resolved": {"type": "boolean"},
        "writer_agency_clear": {"type": "boolean"},
        "footnote_alignment_preserved": {"type": "boolean"},
        "natural_japanese": {"type": "boolean"},
        "analysis_progresses": {"type": "boolean"},
        "internal_language_absent": {"type": "boolean"},
        "remaining_issues": _array(_string()),
        "reason": _string(),
    })),
    "section_flow_acceptable": {"type": "boolean"},
    "section_reason": _string(),
})
STAGE_SCHEMAS["verify_redraft_claims_v31"] = _schema({
    "judgments": _array(_record({
        "paragraph_id": _string(),
        "all_changes_admissible": {"type": "boolean"},
        "changed_claims": _array(_record({
            "candidate_excerpt": _string(),
            "original_support_excerpt": _string(),
            "support_relation": _string([
                "paraphrase", "inferred_from_supplied_material",
                "model_judgment_without_direct_statement", "unsupported",
            ]),
            "basis_ids": _array(_string()),
            "reasoning": _string(),
            "certainty_preserved": {"type": "boolean"},
            "citation_alignment_preserved": {"type": "boolean"},
            "reason": _string(),
        })),
        "reason": _string(),
    })),
})
STAGE_SCHEMAS["audit_predicate_sentences_v31"] = _schema({
    "judgments": _array(_record({
        "paragraph_id": _string(),
        "sentences": _array(_record({
            "candidate_sentence": _string(),
            "predicate_excerpts": _array(_string()),
            "source_support_excerpt": _string(),
            "relation": _string([
                "paraphrase", "inferred_from_supplied_material",
                "model_judgment_without_direct_statement", "unsupported",
            ]),
            "actor_object_direction_preserved": {"type": "boolean"},
            "certainty_preserved": {"type": "boolean"},
            "citation_alignment_preserved": {"type": "boolean"},
            "reason": _string(),
        })),
    })),
})
STAGE_SCHEMAS["review_reader_prose_v31"] = _schema({
    "judgments": _array(_record({
        "paragraph_id": _string(),
        "natural_for_reader": {"type": "boolean"},
        "argument_progresses": {"type": "boolean"},
        "topic_clear": {"type": "boolean"},
        "unnatural_spans": _array(_record({
            "excerpt": _string(), "reason": _string(),
            "intended_referent": _string(), "natural_alternative": _string(),
        })),
        "reason": _string(),
    })),
    "section_flow_acceptable": {"type": "boolean"},
    "section_reason": _string(),
})
STAGE_SCHEMAS["redraft_section_heading_v31"] = _schema({
    "headings": _array(_record({
        "section_id": _string(), "heading_id": _string(),
        "text": _string(), "reason": _string(),
    })),
})
STAGE_SCHEMAS["audit_section_heading_v31"] = _schema({
    "judgments": _array(_record({
        "section_id": _string(), "heading_id": _string(),
        "meaning_preserved": {"type": "boolean"},
        "citation_alignment_preserved": {"type": "boolean"},
        "natural_japanese": {"type": "boolean"},
        "reason": _string(),
    })),
})
STAGE_SCHEMAS["classify_quote_provenance_v31"] = _schema({
    "judgments": _array(_record({
        "paragraph_id": _string(), "literal": _string(),
        "author_analytical_question": {"type": "boolean"},
        "direct_source_quote": {"type": "boolean"},
        "reason": _string(),
    })),
})
STAGE_SCHEMAS["japanese_diagnosis_v31"] = _schema(
    {
        "diagnoses": _array(
            _record(
                {
                    "block_id": _string(),
                    "issues": _array(
                        _record(
                            {
                                "dimension": _string(
                                    [
                                        "vocabulary", "collocation", "syntax",
                                        "reference_clarity", "abstraction", "rhythm",
                                        "academic_register",
                                    ]
                                ),
                                "description": _string(),
                            }
                        )
                    ),
                    "terms_to_keep": _array(_string()),
                }
            )
        )
    }
)
STAGE_SCHEMAS["japanese_paragraph_redraft_v31"] = _schema(
    {"revisions": _array(_record({"block_id": _string(), "text": _string()}))}
)
STAGE_SCHEMAS["japanese_section_review_v31"] = _schema(
    {
        "section_id": _string(),
        "ratings": _array(
            _record(
                {
                    "dimension": _string(
                        [
                            "vocabulary", "collocation", "syntax",
                            "reference_clarity", "abstraction", "rhythm",
                            "academic_register",
                        ]
                    ),
                    "acceptable": {"type": "boolean"},
                    "reason": _string(),
                }
            )
        ),
        "flow_needs_revision": {"type": "boolean"},
        "flow_findings": _array(_string()),
    }
)
STAGE_SCHEMAS["japanese_argument_preservation_v31"] = _schema(
    {
        "section_id": _string(),
        "checks": _array(
            _record(
                {
                    "block_id": _string(),
                    "preserved": {"type": "boolean"},
                    "reason": _string(),
                }
            )
        ),
    }
)
STAGE_SCHEMAS["research_question_v31"] = deepcopy(STAGE_SCHEMAS["research_question"])

STAGE_GUIDANCE.update(
    {
        "research_question_v31": (
            "Formulate one overarching, answerable research problem for the whole supplied "
            "manuscript, with its actual scope, provisional answer, unresolved research gaps, "
            "and rival interpretations. The question should name the relation under study "
            "rather than enumerate every example. Preserve substantive clusters throughout "
            "the memo as central or supporting parts of the argument when they address that "
            "relation; do not narrow the paper to its first journal examples or demote later "
            "clusters on labour, class, short comics, and production to future research merely "
            "to make the question shorter. Give a specific reason when a cluster really needs "
            "a separate paper or lacks enough material to serve the present argument. Keep "
            "works and genres as comparison objects; treat venues and platforms as distribution "
            "conditions and AI production environments as making conditions. Do not coordinate "
            "these categories under one comparison predicate. Partition every supplied "
            "proposition exactly once into topic_coverage, marking each topic central, "
            "supporting, context, research_lead, or excluded and explaining why. Never infer a "
            "prior-research gap or consensus without accepted source evidence. Use only supplied "
            "proposition IDs for memo provenance. Check the grammatical subject, predicate, "
            "and each object in both the question and provisional answer. When works, social "
            "relations, craft practices, and reception require different verbs, use separate "
            "clauses rather than applying one chain of verbs to every object."
        ),
        "research_problem_v31": (
            "Ratify the supplied provisional research question using only the supplied accepted "
            "evidence and memo propositions. Keep the question text exactly unchanged; a material "
            "change requires a new planning run. A prior research position needs accepted source "
            "IDs whose excerpts actually state that position. If no documented prior position "
            "exists, leave prior_positions empty and use a source_internal problematisation "
            "grounded in supplied proposition IDs; never invent a consensus, gap, or rival school. "
            "Explain why the question is needed and state the provisional answer and contribution. "
            "You may refine the provisional answer using only the supplied evidence and memo "
            "propositions; carry the ratified answer forward unchanged, while keeping the RQ text "
            "exactly fixed. Do not present unsupported findings as established. "
            "When defining scope, distinguish works or genres being compared from venues, "
            "platforms, and production conditions used to study them. Do not put these "
            "different kinds of things in one undifferentiated comparison-target list."
            " Research-problem categories are planning metadata: phrase the research question "
            "and provisional answer in ordinary scholarly prose, without calling the claim "
            "a causal proposition or saying it holds only 'within a scope'."
        ),
        "argument_graph_v31": (
            "Build one claim node per supplied claim with evidence and warrant. Counterexamples "
            "and rebuttals are internal checks and must be added only when material, never one per "
            "claim by quota. A rebuttal may connect to its claim through a same-claim "
            "counterexample: rebuttal --rebuts--> counterexample --challenges--> claim. "
            "Do not add a redundant direct rebuttal-to-claim edge for that chain. "
            "Evidence may support a warrant which in turn warrants the same claim: "
            "evidence --supports--> warrant --warrants--> claim. This typed, directed path is "
            "valid when all three nodes carry the same claim_id; do not add a redundant direct "
            "evidence-to-claim edge just to duplicate it. A direct evidence --supports--> claim "
            "edge is also valid when the evidence itself directly supports the claim. Omit an "
            "optional rebuttal when it has no material objection to answer. For every claim "
            "return claim_scopes: the scoped proposition, actual "
            "scope conditions, epistemic status, and evidence basis IDs. Each evidence_basis_id "
            "must be the exact ID of an evidence node in this response, never an upstream "
            "obligation ID, a non-evidence node, or an invented ID. If human_review_feedback "
            "is supplied, use it to repair the previous_candidate_graph, then check every "
            "basis ID against the returned evidence nodes. Incorporate a necessary "
            "qualification into the proposition rather than appending a generic caveat. An "
            "analytic relation may instead be an authorial interpretation argued from adjacent "
            "claims, with its reasoning recorded as a warrant rather than fabricated evidence. "
            "Preserve "
            "the V30 provenance and atomic evidence rules supplied in the context. In V31, "
            "An unverified core external proposition marked omit is a reviewable gap, not a "
            "reason to stop the article or to invent support. Do not create an evidence node "
            "or assert that exact fact; develop the claim using other supported atoms or a "
            "narrower authorial interpretation. Preserve the unresolved obligation in metadata. "
            "attribute_to_memo is internal provenance for an author-supplied proposition, not "
            "an instruction to write 'the memo says' in the manuscript. State an ordinary "
            "authorial interpretation directly and retain its proposition IDs in JSON. Visible "
            "attribution remains mandatory for other_speaker, needs_review, self_citation and "
            "added_theory annotations. Graph nodes, branch names, correction history, and "
            "verification labels are internal records, not wording to be copied into the article."
        ),
        "defense_obligations_v31": (
            "Evaluate internal counterexamples and rebuttals. Surface only a challenge that "
            "materially threatens a named claim and has a traceable basis and response. For each "
            "candidate return target claim, challenge, basis IDs, what fails if it succeeds, "
            "response, resolution location and disposition. Use internal_only or drop for a "
            "nonmaterial challenge. Use scope_absorbed when narrowing the claim resolves it. "
            "Do not invent a counterargument to fill a section."
        ),
        "section_contracts_v31": (
            "Preserve every locked discourse section and its article_role and claim IDs. Give each "
            "section a Japanese title that tells the reader its concrete subject and relation. "
            "Avoid literal translations, unexplained coined nouns, and abstract labels such as "
            "再束縛 when ordinary Japanese can express who classifies what and how. "
            "A title is not an inventory of required terms. Give each body "
            "section a concrete section_task, an established section_output, and "
            "a thesis_link "
            "explaining how that output helps answer the global research question. Assign only "
            "material body defense obligation IDs. Set the legacy minimum_evidence_items "
            "and minimum_concrete_examples fields to zero: neither is a prose quota. "
            "Select only argument moves that help this section; they are not a checklist. The "
            "section may contain claims from more than one top-level topic branch when its local "
            "question gives them a reader-facing reason to share a section. Explain what each "
            "branch contributes to the section_output. An authorial comparison or synthesis "
            "may be the section_output when its reasoning is coherent, even without a source "
            "stating the relation. Do not turn this judgment into an externally verified fact "
            "or attach a citation to a conclusion that its source does not state. If a coherent "
            "local result cannot be formed, reconsider the section boundary. "
            "paragraph_argument_move_catalog payload defines the only allowed paragraph-role IDs. "
            "Choose required_argument_moves only from those exact IDs; never invent, rename, or "
            "translate an ID. Use no label when no distinct paragraph role is needed. Introduction "
            "moves must use an introduction-group ID from that same catalog. A selected move "
            "suggests a substantive section-level operation; it is neither a prose quota nor an "
            "exclusive list for later paragraphs. Omit routine orientation, claim, evidence, "
            "warrant, consequence or transition steps when they add no distinct work. Introduction "
            "must state the locked research question's meaning and scope, documented prior "
            "positions with accepted "
            "source IDs if any, the actual problematisation, provisional answer, contribution "
            "target, and a roadmap of body section IDs. Do not invent a gap. Conclusion contract "
            "must identify body outputs to synthesize and the intended contribution. A body "
            "section_output must be a declarative object-level result that the section will "
            "establish. It must not describe a comparison frame, research procedure, checklist, "
            "material to collect, question to examine, or fact still to verify. Put those actions "
            "in section_task or required inputs, then state the warranted result itself in "
            "section_output. Return the same proposition in section_output_contract and classify "
            "it as documented_result, interpretive_result, or provisional_result with direct, "
            "traceable, or reviewable evidence. An interpretive_result remains a valid section "
            "result when its reasoning is clear even if no source states that conclusion or "
            "no direct basis ID is available. Use reviewable for an authorial judgment whose "
            "external support is absent; use direct only for a genuinely documented result. "
            "Express the reasoning in thesis_link and the section's warranted relationships. "
            "For each section, identify what a reader will understand after reading its analysis; "
            "do not turn required terms or argument-move labels into a sequence of definitions. "
            "Keep only a substantive limitation that changes the result's scope."
        ),
        "semantic_paragraphs_v31": (
            "Plan paragraphs by discourse work, not equal move allocation. Each body section "
            "must establish its section_output. A transition, source context, or synthesis "
            "paragraph may have null central_claim or consequence. Use rhetorical_moves only "
            "with exact IDs from paragraph_argument_move_catalog. The section's "
            "parallel_branch_contribution label is for genuinely distinct branches, not a "
            "default response to missing direct source support. The move "
            "cross_branch_synthesis is only for a relation actually developed in that "
            "paragraph. Cross-branch synthesis may "
            "realize the section's reasoned authorial interpretation without a source stating "
            "the relation verbatim. Neither label is mandatory. Arrange analytical paragraphs "
            "so their observations and reasoning build toward the section result; do not assign "
            "each paragraph its own evidence-status disclaimer. "
            "required_argument_moves are suggestions, not an exclusive allowed list; use another "
            "catalog ID when it describes the paragraph better. Never create free-form names. "
            "Move labels are optional "
            "planning metadata, not a coverage checklist. Record only a move that materially "
            "helps explain the paragraph's work; empty move lists and uncovered planned moves "
            "are valid. Do not add paragraphs or prose to satisfy a move label. Preserve claim "
            "and evidence IDs and return every section exactly once. Choose a central relation, "
            "change, contrast, or concrete observation for each analytical paragraph. Let "
            "secondary terms enter only where they help establish that point; do not give each "
            "term its own definition sentence. Do not make a paragraph whose only result is a "
            "description of what the article will inspect."
        ),
        "draft_introduction_v31": (
            "Write only the targeted introduction from its introduction_contract and grounded "
            "research_problem. Establish the research object and explain why the locked RQ "
            "matters. State the provisional answer, scope, contribution target and faithful "
            "body roadmap. If unresolved_required_citation_sources is nonempty, preserve "
            "its author-supplied "
            "assertions as review obligations and use a qualified attribution only when its "
            "author/work identity is supplied. Preserve the argument and never invent "
            "a bibliography record, reference ID, quotation, or footnote. Do not silently "
            "discard the research question or prior-position distinction. "
            "Prioritize the explicit research problem and introduction contract. Move indices "
            "are optional metadata: record a listed move only when it accurately describes "
            "paragraph work, and leave them empty otherwise. Do not add or reshape prose to "
            "cover a move list. "
            "Do not mention the input memo in reader-facing prose, including メモは, "
            "メモが示す, 出発点となるメモ, or メモに挙がる. Turn its grounded ideas into "
            "direct prose while retaining the source IDs in metadata. "
            "Never invent prior scholarship or a gap. A generic counterargument, concrete "
            "example, or separate caveat is not mandatory. Preserve provenance and citation IDs "
            "for sources actually used. A bibliographic reference candidate without a checked "
            "passage must not be cited merely to describe the candidate; omit its footnote and "
            "evidence metadata unless it supplies a specific concept or argument used here. "
            "For a self citation, use 筆者 plus the exact selected work and an attribution "
            "verb; use 自著 only when it reads naturally as scholarly self-reference. "
            "Assign one footnote to a cited sentence or distinct "
            "comma-delimited clause. When several works jointly support the same sentence or "
            "clause, keep their evidence records separate and use one grouped footnote. "
            "In the scope and roadmap, give works, genres, distribution venues, platforms, "
            "and production conditions their distinct roles. Do not coordinate them as if "
            "they were all comparable works or all the same kind of medium."
            " Keep contract labels and verification vocabulary out of the reader-facing text. "
            "State what is observed or argued directly and express limits with concrete "
            "objects and conditions. Keep established scholarly terms such as 作業概念, "
            "適用範囲, and 因果機構 when they express the argument. Explain what the terms "
            "mean here rather than replacing them by generic synonyms. Keep input-artifact "
            "labels such as 草稿 and 生ログ in provenance metadata."
        ),
        "draft_body_v31": (
            "Write only the targeted body sections. Each section must complete its section_task, "
            "establish section_output with warranted evidence and explain its thesis_link. "
            "The reader_content_brief supplies the section's answer and concise argumentative "
            "framing. Make its reasoned authorial interpretation the thread of the section, "
            "including when the relation has no directly attesting source. Give observations "
            "and reasoning their own work across paragraphs instead of repeating a source-gap "
            "caveat. Record the interpretation in inference_ledger. "
            "An unresolved_required_citation_sources record is a mandatory final-review "
            "obligation, not verified evidence: retain its assertion in review metadata and "
            "as a qualified attribution when its author/work identity is supplied. Preserve "
            "the section's argument, but do not invent its source, reference ID, "
            "quotation, or footnote. Do not erase a section's claim to hide the gap. "
            "Separate source facts and attributed positions from the article's own inference. "
            "Return source IDs for authorial inferences as derivation metadata, but never make "
            "a source author appear to have stated the article's inference. Original-source "
            "checks belong in review metadata, never in reader-facing paragraphs. "
            "Never narrate what the input memo says with メモは, メモが示す, "
            "メモに挙がる, or similar wording. Write the bounded interpretation itself "
            "and keep memo provenance in JSON metadata. "
            "Surface only assigned material defense obligations, naming their target and "
            "resolving them at the planned location. Express scope in the claim itself where "
            "possible. When a grounded counterexample identifies a mistaken relation between "
            "named examples, state the corrected relation and explain what changes in the "
            "argument. Do not substitute a process statement such as saying the examples will "
            "not be used, that their roles cannot be determined, or that source checking remains. "
            "Paragraphs need not each be a complete claim-and-consequence unit. "
            "A section may develop more than one topic branch. Keep each branch's claims, "
            "examples, evidence, and contribution identifiable. When the section result "
            "connects them, explain the author's interpretive reasoning across paragraphs "
            "rather than presenting each branch as an unresolved item. A source need not state "
            "the connection verbatim; do not invent checkable facts or imply that a citation "
            "verifies the author's own synthesis. "
            "Section_task, section_output, thesis_link, evidence, and assigned defense "
            "obligations determine the section's work. Move indices are optional metadata: "
            "record a listed move only when it accurately describes paragraph work, and leave "
            "them empty otherwise. Do not add or reshape prose to cover a move list. "
            "Preserve evidence IDs and paragraph citation lineage for sources actually used. "
            "Only references outside required_scholarship_evidence_ids are optional merely "
            "because they are linked to a claim; omit those unless a specific concept, argument, "
            "method or finding does work in the local inference. Every reference inside "
            "required_scholarship_evidence_ids is a deliberate source-use commitment selected "
            "from the annotated evidence and must be realized with its actual author/work, the "
            "specific concept or argument being used, and its function in this section. It need "
            "not be presented as proof of the paper's empirical claim; explain its narrower "
            "theoretical, methodological, comparative or counterpositional role. Do not write "
            "a sentence merely to say that a source does not directly prove the claim. Name "
            "the exact work in any "
            "self citation: name the exact work and attribute the earlier argument with "
            "筆者は『作品名』で論じた or an equivalent natural attribution. "
            "自著 may remain when it naturally identifies the writer's published work. "
            "Assign one footnote to each cited sentence or distinct "
            "comma-delimited clause. When several works jointly support the same sentence or "
            "clause, keep separate evidence records but use one grouped footnote; do not invent "
            "different claims for the works merely to obtain separate notes. The note belongs "
            "after the claim-bearing sentence or clause, not "
            "immediately after a work title or piled at a paragraph end. Write substantive "
            "interpretation or findings within the "
            "available evidence, with scope built into the claim. Do not replace the section "
            "output with a list of checks that future research would need. Never embed HTTP "
            "links in reader-facing prose; citation identifiers stay in structured metadata."
            " Keep section_task, section_output, thesis_link and epistemic classifications "
            "in metadata. Do not narrate the writing process with ここで確立できるのは, "
            "ここで捉えられるのは, or "
            "本稿の主張は因果命題ではなく. Say what the evidence shows in direct Japanese, "
            "and state any necessary boundary in plain words. Preserve defined terms and "
            "legitimate limits, including 適用範囲 or 因果機構 when they state substantive "
            "content. Keep 草稿 and 生ログ as provenance metadata rather than describing "
            "the input workflow in the article. "
            "When evidence is limited, narrow "
            "the sentence to the supported comparison and continue the analysis instead of "
            "turning the section into a checklist of materials to collect. When comparing works "
            "from different publication or distribution forms, such as a magazine work and an "
            "animation film, keep the work-level comparison distinct from magazine issue, "
            "release, screening, platform, and distribution conditions; never call film release "
            "or distribution a magazine掲載 condition. Attach evidence only to the exact "
            "proposition it supports. Leave an authorial comparison procedure uncited rather "
            "than attaching evidence that supports a different premise. If a kinship detail, "
            "medium type, or publication or distribution route lacks projected evidence, omit "
            "that detail and retain only a supported conditional comparison."
        ),
        "draft_conclusion_v31": (
            "Write only the targeted conclusion after reading the established body drafts and "
            "section outputs. Synthesize outputs across sections into a final answer to the "
            "locked research question; do not list section summaries. Natural paraphrases are "
            "valid when they preserve the question's object, relation and scope. "
            "In conclusion_synthesis.established_section_ids, list exactly the section_id values "
            "of the supplied Body drafts in their order; never include the Conclusion's own ID. "
            "The final answer "
            "may have the same meaning as the provisional answer when the Body supports it; ground "
            "that answer in the established results instead of requiring different wording. State "
            "the new contribution and integrate necessary scope into the answer. "
            "The RQ, established body outputs, cross-section synthesis, and contribution "
            "determine the conclusion's work. Move indices are optional metadata: record a "
            "listed move only when it accurately describes paragraph work, and leave them empty "
            "otherwise. Do not add or reshape prose to cover a move list. "
            "Do not refer to the input memo or describe the drafting or verification process "
            "as the article's result. Keep source provenance in metadata. "
            "Use existing evidence by default. Any new evidence requires an explicit rhetorical "
            "reason and must still be in the locked contract. Do not append routine limitation "
            "or future-work sentences. Open review items are not future work and must not be "
            "copied into the Conclusion. Answer the RQ with the established body outputs despite "
            "nonblocking review items. If a self citation is necessary, identify the exact "
            "selected work and say what 筆者 argued there; 自著 may remain when natural. "
            "Give each cited "
            "sentence or distinct comma-delimited clause one footnote. Group several works in "
            "one note when they jointly support the same attribution unit. Express the "
            "conclusion_synthesis.final_answer naturally and faithfully in the conclusion prose. "
            "The metadata remains an exact structured contract; the prose need not repeat its "
            "wording verbatim."
            " Do not print planning or verification labels in the conclusion; give the "
            "substantive answer directly, with concrete scope rather than a named claim type. "
            "Keep input-artifact labels such as 草稿 and 生ログ in provenance metadata. "
            "Preserve scholarly concepts and concrete limits when they are part of the answer. "
            "Keep "
            "grammatical roles precise: works may repeat or alter formal patterns, while readers "
            "encounter, interpret, or reuse works. Never say that works and readers jointly "
            "repeat or replace a publication context. When body sections establish comparison "
            "items rather than verified relations, state the contribution as a framework for "
            "examining those relations, not as relations already confirmed. "
            "When reader_facing_scholarship_citation_anchors are supplied, use their author and "
            "work fields to identify each cited source in a natural, complete sentence in the "
            "conclusion paragraph whose evidence_ids includes that record's evidence_id; put "
            "the same evidence_id and an attributed_scholarship treatment on that paragraph. "
            "Do not replace a named source with a generic phrase such as 理論文献. A "
            "bibliographic-identity-only record may be named as the manuscript's "
            "theoretical reference, but it is not empirical support for an individual case: "
            "do not state an unverified substantive argument from that work or use it to "
            "substitute for the manuscript's case evidence. Keep the citation attached to the "
            "sentence that names the author and work."
        ),
        "audit_v31": (
            "Audit Introduction, Body and Conclusion by their distinct communicative purposes. "
            "An authorial interpretive relation is not an unsupported external fact merely "
            "because no source states it directly. Check whether the section argues it "
            "coherently and records it in inference_ledger; flag invented checkable facts, "
            "false source attribution, or a genuine leap in reasoning. "
            "For every section check over-attribution, mistaken attribution of authorial "
            "inference, traceable inference derivation, leaked original-source review notes, "
            "achievement of an analytical section result, and leaked research-plan or audit "
            "meta-prose. Review the whole manuscript against this one checklist. "
            "Inspect the complete manuscript and return every remaining actionable defect in "
            "one response; do not stop after representative examples or defer another defect "
            "of the same kind to a later audit pass. "
            "Return exactly one section_reviews record for every target_section_ids entry, in "
            "the supplied order. Mark a section clean only after checking its role-specific "
            "contract, evidence use, reader-facing prose, and relation to the whole argument. "
            "When a section has findings, list every corresponding finding id in finding_ids; "
            "every returned finding id must occur in at least one section review. "
            "Introduction: state the locked RQ explicitly in meaning; accept natural paraphrases "
            "that preserve its object, relation and scope. Check that the prose communicates the "
            "question rather than requiring a verbatim quote. Also check grounded need, no "
            "invented "
            "prior position and accurate "
            "roadmap, and coherent comparison units. Flag a scope sentence that lists works, "
            "genres, venues, platforms, and production conditions under a single comparison "
            "predicate without stating their distinct roles. Body: section task/output/thesis "
            "link, warranted claim scope, only needed "
            "defenses with target and resolution. Conclusion: answer derivable from established "
            "body outputs, cross-section synthesis beyond summary, contribution, no unreasoned "
            "new evidence or routine limitation. Flag a paragraph that only lists caveats, "
            "source insufficiencies or tasks for future verification without establishing a "
            "section output. Flag negative-only citation purposes, work-to-footnote "
            "mismatch, embedded HTTP links and more than one visible note for the same sentence "
            "or comma-delimited clause. Multiple scholarship evidence records that name several "
            "works in that same attribution unit are valid when the renderer can group them into "
            "one note; do not demand invented differences between the works. Multiple memo or "
            "inference evidence "
            "records sharing a support_sentence are internal provenance and are not multiple "
            "footnotes. For each required_scholarship_obligations record, compare the prose with "
            "its author_supplied_assertions and author_supplied_source_spans. When an explicit "
            "author-supplied assertion attributes a concept or position to a named work, "
            "bibliographic_identity_only means that the passage was not independently checked; "
            "it does not mean that the supplied attribution is absent or that the citation has "
            "no argumentative purpose. Accept a sentence that explicitly attributes only that "
            "bounded proposition to the named author and connects it to the local argument. Do "
            "not flag or recommend deleting such a citation solely because "
            "substantive_support_verified "
            "is false. Still flag any claim that exceeds the supplied assertion, any source/work "
            "mismatch, and a citation that merely says a reference was added, listed, retained, "
            "or used without stating the attributed concept and its argumentative function. "
            "Never treat this rule as independent verification of the source's contents. "
            "Also preserve all V30 provenance, citation, source and "
            "structural checks. The render_stage field is pre_footnote_render: final footnote "
            "and bibliography sections do not exist yet. Do not report their mere absence as a "
            "defect. Instead audit whether each source-bearing sentence or distinct clause has "
            "valid paragraph evidence IDs and treatment metadata from which the later renderer "
            "can place one note at that sentence or clause. "
        "Use finding_type evidence_treatment_gap when a supported sentence lacks a matching "
        "evidence ID or treatment record, and evidence_mismatch when the attached ID supports "
        "a different object or proposition. A mismatch finding removes the bad attachment but "
        "does not authorize substituting a proposition merely because it uses similar words. "
        "When the sentence still needs additional evidence after that removal, emit a separate "
        "evidence_treatment_gap finding; when it is an authorial method statement, recommend "
        "removing the attachment and keeping the method as an inference. affected_section_ids "
        "must contain only sections whose reader-facing prose must change. Do not include a "
        "section merely because its contract, output or evidence was consulted while diagnosing "
        "another section. An Introduction-only wording defect targets only the Introduction; a "
        "Conclusion-only synthesis defect targets only the Conclusion. "
        "Treat prior_audit_resolutions as binding evidence and scope decisions. Do not restore a "
        "named attribution, factual relation, evidence attachment or claim scope that a prior "
        "resolution removed as unsupported unless you identify newly available allowed evidence "
        "that directly supports the restoration. When a finding itself says that a named speaker "
        "or role lacks supporting evidence, its replacement must remove or leave open that "
        "attribution or role; it must not instruct the repair to assert it. An explicit authorial "
        "comparison procedure, scope "
        "declaration, research question or roadmap is not a sourced factual claim merely because "
        "it names analytic fields. Do not demand an evidence treatment for a sentence that says "
        "what this article will compare, distinguish or examine unless that sentence also asserts "
        "an observed fact about the materials. Report concrete section IDs, replacement argument "
        "and repair scope."
        " Flag prose that reports the manuscript's planning, audit or verification operations "
        "instead of explaining its subject. Judge this by the whole sentence and its context: "
        "因果機構, 適用範囲, 作業概念 and other technical terms or evidence limits can be "
        "substantive academic language. A writer's concrete decision to compare or apply a "
        "theory is also legitimate prose. Supply an evidence-bounded replacement that states "
        "the relationship among objects; neither delete a term mechanically nor erase a real "
        "qualification. Never propose reader-facing "
        "wording that mentions the memo, supplied notes, input text, user, drafting process, "
        "or a reference candidate. When the only basis is supplied material, keep that "
        "provenance in metadata and phrase the bounded observation directly; if a named "
        "speaker's statement matters, attribute it to that speaker and attach the source note "
        "to the sentence without narrating the memo. Use repair_scope local_section whenever "
        "the defect can be corrected by rewriting an existing named section while preserving "
        "its locked task, output, thesis link, claims and evidence allow-list. This includes "
        "narrowing a Body synthesis or Conclusion answer to what the existing contracts and "
        "section outputs establish. Use plan_revision only when the locked plan itself lacks "
        "a required claim, dependency, section output or section, so prose repair cannot solve "
        "the defect. A supplied replacement sentence consistent with the current contract is "
        "local_section, not plan_revision. Use repair_scope none for a nonblocking observation. "
        "Use the same substantive obligations for generation, repair and audit. A local_section "
        "finding identifies the affected section for bounded repair; unresolved semantic "
        "findings remain visible for final human review without an intermediate decision. "
        "Reserve blocking for integrity failures that prevent a safe candidate from being built."
        ),
        "polish_v31": (
            "Polish the targeted prose locally while preserving the distinct Introduction, "
            "Body and Conclusion purposes in their locked role contracts. Do not add a generic "
            "counterargument, qualification, limitation, future-work sentence, citation or new "
            "evidence. Preserve the RQ's meaning and scope, section outputs, synthesis, "
            "paragraph boundaries, "
            "claim scope and citation lineage. "
            "Keep each cited work's explicit title in the paragraph that uses its "
            "theory; a surname alone does not preserve the paragraph's citation "
            "anchor. Keep the title tied to the same reference and claim. "
            "Rewrite category-mixed scope lists into "
            "separate sentences that identify what is compared and what is a distribution "
            "or production condition. Return changed_claim_ids empty."
            " Remove explanations of the author's internal planning or verification workflow "
            "from the prose. Preserve substantive scholarly terms, defined concepts, and "
            "evidence limits. Recompose a stalled paragraph around its actual analytical "
            "judgment rather than defining every term in a former list. Follow a theorist's "
            "claim with analysis of the object instead of another sentence about the writer's "
            "choice of theory. Do not add facts or repeat a stock authorial phrase."
        ),
    }
)
_BODY_PROSE_POLICY_V31 = """
目的は段落が対象について実際に論じる自然な日本語にすることである。主張、根拠、
重要な区別、因果方向、必要な限定、出典との対応を維持する。元の文の順序に従う必要はない。
主語の省略自体は誤りではない。文脈から主体を復元できる場合は認める。ただし論文冒頭と
各節の第一段落では、何を対象に論じるのか分かる話題設定を置く。各文の主題、述語の
行為主体、対象を区別する。主述が合わないときは主語の挿入だけで済ませず、文全体を
組み直す。並列された対象の一つ一つに述語が自然に係るか、比較対象が同じ水準か、
修飾語が意図した語に係るかを確認する。段落全体で話題を提示し、説明を進め、文を接続する。
列挙された名詞を順番に定義しない。段落の中心となる関係、変化、対立、具体的な観察を
選び、必要な概念だけを議論の進行に合わせて導入する。記号を接続詞に替えるだけ、
定義文を並べるだけ、抽象語で一括するだけでは議論は進まない。
理論家の主張を紹介したら、研究対象についての判断へ自然に進む。「本稿では」「筆者は」
「ここでは」や定型的な接続を反復して主体性を示そうとしない。段落の分析より分析計画の
説明が長い場合は、対象について何が言えるかを中心に組み直す。
文法的でも不自然な連語、抽象名詞と汎用述語の安易な組合せ、意味の曖昧な「〜化」を
点検する。研究質問、節の結論、比較仮説も校正する。意味を固定することと表現を固定する
ことは違う。書名と直接引用は保持し、論証管理情報、修正履歴、採否判断、検査・回答の語を
本文に混ぜない。専門用語は必要なら保つ。文字数の増減は
目的にせず、内容が進まない説明を増やさない。新たな書誌的事実や引用は作らない。
「相互作用を問う」のような研究上の問いを「相互に作用している」という確定した事実へ
強めない。ただし節全体で関係を論じて筆者の解釈として示すことと、関係が
外部的事実として確証済みだと述べることは区別する。出典の不在だけを理由に
解釈の各段落へ「未確認」「可能性がある」を繰り返さない。対象や適用範囲を
実際に変える仮説・留保は保つ。
既存HTMLの全タグ、階層、順序、属性、ID、節・段落の数と配置を保つ。編集はtext_slots
だけとし、スロット数・順序を厳守する。html_templateの{{TEXT:n}}は位置の説明であり
返却テキストに入れない。sup/aの脚注、citeの文献名、直接引用、脚注・参考文献は固定される。
固定された脚注の前後の文章を組み直しても、その脚注が支える主張を移したり変更したり
しない。段落の新設・削除・統合・分割、HTMLの再生成、Markdownへの変換は禁止する。
脚注と主張の対応を維持できない場合は暫定的に原文を保ち、対象段落と具体的な理由を報告する。
原文は意味差分の照合資料であり、日本語品質の合格例ではない。未解決段落は脚注前の
段落形成へ差し戻される。
初稿に記録されたLLMの解釈は、単に出典に明記されていないという理由で削除しない。
"""
STAGE_GUIDANCE["diagnose_body_prose_v31"] = _BODY_PROSE_POLICY_V31 + """
本文を編集せず、節全体と前後の文脈を読んで各段落の修正対象を抽出する。
paragraphsには今回診断する段落だけが入る。context_paragraphsは節全体の文脈として
読むが、そこにしかない段落を診断結果に含めない。paragraphsのparagraph_idだけを
同じ順序で一度ずつ返す。問題のない段落はissuesを空にする。
issue_idは段落内で一意とし、原文のexcerpt、個別のitems、段落が伝えるべき中心の関係を
context_relationに記録する。各項目の定義ではなく議論の停滞を特定する。節の要約や
論証設計を返す処理ではない。禁止語の有無だけで問題や適切さを判定しない。
名詞の連結や抽象語の組合せが不自然な場合、issue.excerptに正確な語句を記し、
descriptionにその語が何を指そうとしているか、読みにくい理由と自然な言い換えを記す。
「隣接文化」「漫画形式」のような圧縮表現は文脈中で意味が通るか個別に見る。
context_relationは原文から確認できる関係だけを書く。原文で作用の強さや向きが
定まらない場合、診断者がそれを補って確定しない。後続の改稿がこの診断を根拠に
原文より強い主張を作らないよう、曖昧さも正確に記す。
特に論文冒頭と節冒頭では話題設定、主体の復元可能性、述語と目的語、修飾関係、
自然な連語を診断する。各文だけでなく段落の流れとして何が妨げられているかを示す。
冒頭段落の第一文は前文なしで単独に読む。「対象に、～を問う」「～を検討する」などの
筆者の行為を表す述語で、誰がその行為をするかも話題も示せない場合は必ずissueに記録する。
後続の研究質問だけを直して第一文の問題を見落とさない。
"""
STAGE_GUIDANCE["redraft_body_prose_v31"] = _BODY_PROSE_POLICY_V31 + """
paragraphsにあるparagraph_idだけを同じ順序で一度ずつ返し、各段落のtext_slotsを
書き換える。context_paragraphsは節全体を読むための文脈であり、そこにしかない段落は
返さない。
diagnosesの問題を解消したらaddressed_issue_idsにそのIDを入れる。修正対象がなく自然な
段落は維持する。解消できない問題が一つでもあればその段落全体を原文のtext_slotsに戻し、
review_reasonに具体的な不足を記す。他の段落ではreview_reasonを空にする。
節全体を読み直し、文章の流れと構文の多様性を確認する。文章を短くするために情報を
再圧縮しない。新しい論証を設計せず、確認できる関係を文章として表す。
diagnosesは文章上の問題を見つける手掛かりであり、原文にない意味を保証しない。
原文で作用の向きが未確定なら、候補で「狭める」「増やす」などの一方向の結果を
無記録で断定しない。相対的な比重を述べる原文を、強い因果の断定へ変える場合は
提供された材料との推論経路を明示し、確実性を保つ。原文に明示されない具体化も、
読者の理解を進めるなら採用してよい。推論根拠は本文に言い訳として書かず、後段の
命題監査が本文外の記録へ残す。再起草時はrejection_feedbackを優先して一度だけ修復する。
冒頭に無理に「本稿では」を置かず、必要なら自然に主体を明示する。研究質問の文言も
意味、対象範囲、確実性を保って書き直す。脚注位置の制約で自然な文が成立しなければ
元のスロットを暫定的に保持し、paragraph_idと理由をreview_reasonに記す。
保持した原文を自然な文章として合格扱いにしない。
authored_question_literalsに挙げられた短い問いは筆者の分析上の問いなので、
意味を保って言い換えてよい。それ以外の引用符内の文言は保持する。
冒頭の第一文に問題があるなら、その文を実際に組み直す。研究質問の一部だけを直して
第一文を残さない。「本稿は」は必要な箇所で一度使ってよいが、各段落へ機械的に広げない。
不自然な名詞連結は元の語を残す前提で微修正せず、指している対象と関係を普通の
日本語で言い直す。診断に自然な言い換え案があっても文章全体の文脈で選び直す。
"""
STAGE_GUIDANCE["extract_paragraph_meaning_v31"] = """
原文の文体を写さず、各段落が実際に述べる内容を短い命題カードに分解する。
paragraphsにあるIDだけを同じ順で返す。中心判断、作品や資料についての観察、
筆者の推論、限定、固定脚注の役割を区別する。文面の良し悪しを直す工程ではない。
原文にない具体化を原文の主張として記録しない。補う判断が必要ならinferencesに
明示し、供給材料との推論経路を簡潔に述べる。因果の方向、断定の強さ、比較の範囲を
原文より強めない。引用した著者の主張と筆者の適用を区別する。
「自著」は資料の帰属を示す語であり、「論じた」行為の主体ではない。原文が
「筆者が『書名』で論じた」と自然に述べる場合、その主体と文献の関係を区別して記録する。
出典に同じ結論がないことは、筆者の解釈を段落ごとに留保する理由ではない。
解釈の理由は節全体の判断と前後の段落から読み取り、資料の未確認という
作業上の状態はeditorial_onlyに置く。対象の範囲を実際に狭める条件はlimitsに残す。
カードは本文の完成文や見出しの焼き直しにせず、誰・何が、何とどう関わるかを
普通の語で記す。本文の主張の範囲を実際に制限する条件だけをlimitsに置く。
書誌確認、修正履歴、検査の進み具合、先行研究の未確認状態など、対象についての
結論を制限しない作業上の留保はeditorial_onlyに置く。重要な反証、作用方向の
不確実性、引用著者の立場と筆者の判断の区別をeditorial_onlyへ退避しない。"""
STAGE_GUIDANCE["compose_body_prose_v31"] = """
あなたは学術日本語本文の編集者である。これは節ごとに一度だけ行う最終改稿である。
V32では書誌同定のみの引用段落に限って採否の読後評価を行うが、再起草や
最終推敲は行わない。その前提で節全体を読み、
自然さ、論旨、原文の意味、引用の支持対象を一度の出力で整える。
paragraphsには節内の編集可能な段落がすべて入り、source_text_slotsが現在の本文である。
修正不要な段落は原文のtext_slotsをそのまま返す。不自然な箇所だけを直し、
局所修正で主語・述語や文の運びが自然にならない場合は段落の文を組み直す。
各paragraph_idを同じ順に一度ずつ返し、addressed_issue_idsは空配列にする。
section_scopeとsupplied_materialには節の判断と短い命題・引用対応がある。
supplied_material.writer_directivesには、序論の問い・暫定回答・対象範囲・貢献、
列挙から選ばれた中心関係、中心概念を適用する節の候補が短く記録されている。
これは本文へ転記する語句一覧ではない。関係の内容を読み、必要な例だけを
自然な文の主語・述語で説明する。中心概念は定義後、指定された節で対象に
どう働くかを示せる場合に使い、単なる反復は避ける。
書誌同定、出典照合、適用の検証状況は本文外の管理情報であり、引用済みの
文献を「参照候補」「検討するための文献」として紹介し直さない。原文が文献の
視角を論点へ結びつけているなら、その関係を保って論を進める。資料の内容を
新たに断定できない場合は元の文を保持し、作業状況を本文へ書き足さない。
それらを原文の意味と脚注の対応を守るために使い、出典にない確認可能な事実を加えない。
前後の節の短い文脈は文章の接続に使う。直接の出典がない著者の解釈を
各段落の定型的な留保に変えず、節の論旨として自然に述べる。
列挙された要素を短い名詞列や抽象語へ圧縮しない。必要な例を選ぶ場合も、各例が
中心判断をどう説明するのかを文の述語で示す。必要な区別を説明せずに削除しない。
無関係な項目を網羅する必要はないが、省略は論旨と出典の対応を変えない場合に限る。
各段落のtext_slotsはslot_count個とし、html_templateの{{TEXT:n}}へ順に入る。
既存の引用要素・脚注要素の直前と直後が自然な文になるようにする。
引用要素そのものは出力しない。引用の支持対象を変えない。
protected_literalsの文字列は、本文中の対応する段落に同じ回数、同じ表記で残す。
これは書名・数字・直接引用などを守るためである。『書名』などの並列では
閉じ括弧と次の開き括弧の間に「・」を置く。書名内部の入れ子括弧は変えない。
writer_attribution_anchorsには元稿で自然に示された筆者の行為が入る。
「筆者が『書名』で論じた」は保ち、「自著『書名』で論じた」へ変えない。
「自著」は著作の帰属を示す語であり、「論じた」の行為主体ではない。
最初の文で対象と主題を自然に示し、観察から判断へ進める。用語を一つずつ
定義しない。段落が何を分析する予定かだけを述べて終わらせない。
抽象名詞の連結や独自の「〜化」表現を避け、主体・対象・述語が自然に結びつく
普通の学術日本語を使う。「視点が関係に接続する」のように二者の関係を
曖昧な「接続する」で済ませず、何と何がどの点で関わるかを述べる。
推論の記録や検査の説明を本文に書かない。
原文にない解釈的具体化は、節の判断と提供された根拠から説明できる範囲に限る。
支持のない確認可能な事実は加えない。review_reasonは、固定されたHTML要素や
引用・脚注の位置のために自然な改稿が成立しない場合だけ、具体的な理由を記す。
修正可能な問題をreview_reasonへ退避せず、本文で解決する。
"""
STAGE_GUIDANCE["polish_final_japanese_v31"] = """
あなたは学術論文の最終本文を推敲する日本語編集者である。候補の語を一つずつ
置換するのでなく、段落の中心となる観察と判断が自然につながるように全文を
組み直す。paragraphs の ID と順序を守り、各 text_slots の個数を変えない。
html_template の {{TEXT:n}} 以外の要素は出力せず、脚注・引用の前後が自然に
つながるようにする。protected_literals の表記と回数を守る。
candidate_context_paragraphs と前後の節は文脈であり、編集対象は paragraphs
だけである。feedback の不自然な連語、主語と述語、比較の水準、定義の反復、
分析計画にとどまる文章を具体的に直す。序論では対象についての問題を示し、
問いや方法を順に列挙するだけで終えない。節の冒頭は話題を自然に導入する。
旧稿が渡されても文体の手本にしない。現在の中心判断、確実性、引用の支持先を
保持し、確認可能な事実を創作しない。推論で具体化した場合は断定を強めず、
内部の検査・編集・根拠管理の言葉を本文に出さない。
不自然な表現を残すより、一文の組み立てを変える。問題のない文を変更する必要はない。
既に自然な「筆者が『書名』で論じた」を「自著『書名』で論じた」へ変えない。
列挙を抽象語で言い換えるだけで説明を省かず、語と述語の係り先を具体的に示す。
並列する『書名』同士の間には「・」を置き、書名内部の入れ子括弧は維持する。
"""
STAGE_GUIDANCE["audit_redrafted_prose_v31"] = _BODY_PROSE_POLICY_V31 + """
独立した検証者として、元の段落と候補を段落単位で照合し、節全体を読み直す。
診断や改稿の意図ではなく、原文と候補そのものを内容保持の根拠にする。
context_paragraphsは原文、candidate_context_paragraphsは今回実際に採用する節全体である。
fixed_citation_contextの脚注・書誌とcitation_bindingsも照合し、支持関係を推測で補わない。
judgmentsはpayload.paragraphsにあるparagraph_idだけを、その順序で一度ずつ返す。
context_paragraphsとcandidate_context_paragraphsは文脈として読むが、そこにしかない
paragraph_idの判定は返さない。診断に漏れた問題も確認する。
content_preservedは節の中心判断、実質的な限定・反論、根拠との関係、
因果方向、出典が保持された場合にtrueとする。原文の各段落にある補助的な
項目や具体例を同じ段落へ残すことは求めない。区別が節の主張の射程を
変えない場合、「明示的規則と暗黙の慣行」や「作品と記事」の内訳が候補の
当該段落にないことだけではfalseにしない。節全体で意味が保たれるかを見る。
editorial_only_by_paragraphの作業上の留保は本文から省いてよい。ただしその分類が
実際の主張の適用範囲や確実性を変える場合は誤分類として退ける。旧稿の表現を
品質基準とせず、節契約と根拠に照らして判断する。
原文に明記されない解釈的具体化でも、供給された材料から妥当に推論でき、
確実性と脚注の対応を保つなら、それだけでfalseにしない。命題監査が推論を別記する。
原文が問う・仮定する関係を候補が外部的な既成事実として断定したら
content_preservedをfalseにする。ただし節の議論から筆者の解釈として
述べ、外部的事実や引用元の確定的主張へ格上げしていなければ、それだけで
falseにしない。
各候補の変更された述語について、原文がその行為・対象・作用の強さ・作用の向きを
実際に支持するか確認する。原文が「行動範囲との関係で比重が大きい」と述べるだけなら、
候補が「行動範囲を狭める」と制限方向を確定することは内容保持ではない。
作用の向きが未確定なのも保持すべき限定である。原文の問いや可能性を外部的な事実の断定へ
変えたらcontent_preservedをfalseにし、該当表現をreasonへ記す。他の留保や
脚注が残っていても相殺しない。新しい解釈の根拠が不足する場合もfalseにする。
反対に原文が示す方向を自然な動詞で言い換えただけなら、その語だけで不合格にしない。
enumeration_resolvedは段落の中心となる関係や観察が自然な文章として
論を進める場合にtrueとする。全項目の逐次説明を要求しない。節の結論を
変える区別の削除、定義文の連続、記号置換、空疎な抽象化は不合格にする。
writer_agency_clearは筆者の採用と理論家の主張が区別できる場合に限る。無関係な受動態を
直すことは要求しない。footnote_alignment_preservedは固定された各脚注の支持対象が
維持された場合に限る。natural_japaneseは意味の圧縮が解消され、語彙・構文・指示関係が
自然な場合に限る。論文冒頭と節冒頭では話題の導入と省略された主体の復元可能性を、
各文では述語と目的語、修飾、比較構文、自然な連語を確認する。共有された述語が並列する
目的語の一つにでも不自然ならnatural_japaneseをfalseにする。単に主語を明示しただけの
文を合格にしない。analysis_progressesは定義や方法の説明を繰り返さず、対象について
読み取れることを示す場合に限る。internal_language_absentは修正履歴、応答、検査、
論証グラフの分類名が本文に流入していない場合に限る。未解決事項はremaining_issuesと
reasonに具体的に記す。
最後にsection_flow_acceptableで文の連続性、不必要な冗長さ、構文の画一化を評価する。
冒頭段落は第一文を前文なしで読んで評価し、誰が「問う」「検討する」のか不明なら
natural_japaneseをfalseにする。
文字数増加自体を不合格にしない。判断できない項目を合格にしない。編集案は返さない。
"""
STAGE_GUIDANCE["verify_redraft_claims_v31"] = """
あなたは日本語校正の評価者ではなく、原文から候補への命題変化だけを独立に検査する。
diagnoses、改稿意図、文章の自然さ、脚注の存在を内容保持の根拠にしない。
comparisonsの各paragraph_idについて、原文と候補の可視本文を直接照合する。
候補で変わった主語・述語・対象・作用の向き・強さ・比較・限定・事実の追加を
changed_claimsへ具体的に列挙する。各candidate_excerptは候補から、
original_support_excerptは原文から正確に抜き出す。原文に支持箇所がなければ
original_support_excerptを空文字列にする。自然な言い換えはparaphraseとする。
原文にある補助的な列挙や具体例を候補が省いたことだけでは変更を不適切と
みなさない。節の中心判断、実質的な適用範囲、因果方向、確実性、出典との
関係が変わる省略だけを問題にする。各段落に同じ内訳を再現させない。
原文に明示されない具体化も採用できる。supplied_basis_idsにある命題・出典から
判断した場合はinferred_from_supplied_material、そこに直接の記述はないが
LLMの推論として採る場合はmodel_judgment_without_direct_statementとする。
どちらもreasoningへ推論過程を具体的に書き、使ったbasis_idsを列挙する。
根拠IDを創作しない。出典が直接その判断を述べたように偽装しない。
推論の結果を筆者の解釈として述べることはできる。原文の問い・仮説・可能性を
外部的な確定事実へ強めず、既存の脚注が
支持していない新事実をその脚注に負わせない。これらに反する候補は
unsupportedとし、certainty_preservedまたはcitation_alignment_preservedをfalseにする。
材料がなくとも妥当な解釈判断ならmodel_judgment_without_direct_statementで
記録できる。ただし書誌・年代・掲載誌・人数など確認可能な事実は作らない。
変更された命題がなければchanged_claimsは空にする。変更が一つでも不適切なら
all_changes_admissibleをfalseにする。理由には決定的な差を述べる。
全paragraph_idを一度ずつ同じ順序で返す。本文は修正しない。
"""
STAGE_GUIDANCE["audit_predicate_sentences_v31"] = """
独立した命題監査者として、candidate_sentencesに列挙された各文を必ず同じ順序で
一度ずつ返す。各文にある主要な述語をpredicate_excerptsへ本文どおり抜き出し、
主語・対象・作用の向き・強さ・確実性を原文および提供資料と照合する。
一文に複数の述語がある場合は全て列挙し、一つでも原文の問いや相関を外部的に
確証済みの因果の断定へ
強めたらactor_object_direction_preservedかcertainty_preservedをfalseにする。
predicate_excerptsは候補文に、source_support_excerptは原文に、それぞれ文字列として
そのまま含まれる連続した箇所だけを書く。脚注番号や固定書名をまたいで文を再構成した
近似引用は使わない。原文に対応する連続箇所がない場合は空文字列にし、推論または
根拠不足として関係を判断する。
format_repair_feedbackがある場合は、命題判定を改めて確認しながら、指摘された
引用の文字列を原文・候補文から正確に取り直す。意味判定を通すために根拠のない
引用を作ったり、falseをtrueに変えたりしない。
元の文面は文章品質の基準ではなく命題差分を知る資料である。原文にない具体化は
推論として採用できるが、必ずrelationに推論種別を記す。脚注の支持対象を変えない。
見落としを防ぐため、改稿者のchanged_claimsや文章監査の判断を参照しない。
"""
STAGE_GUIDANCE["review_reader_prose_v31"] = """
候補の日本語を初めて読む学術論文読者として独立に評価する。原文、診断、修正意図、
命題監査を見ない。候補段落と前後の候補だけを読む。論文冒頭・節冒頭の話題が
自然に立ち上がるか、主題・主体・対象と述語が結びつくか、語の組合せが自然かを
確認する。定義の連続、分析計画の説明、同じ構文の反復、抽象語の連結、論証管理語を
不合格にする。節全体で同じ資料不足や推論の留保を繰り返し、解釈的な結論へ
進まない場合もsection_flow_acceptableをfalseにする。必要な対象範囲の限定は
その理由だけで不合格にしない。引用済みの文献を参照候補と呼び直したり、
出典照合や理論適用の作業状況を本文で説明したりする段落は、自然で論を進める
文章として合格にしない。不自然な名詞の連結や連語は、禁止語の数ではなく
該当する正確なexcerpt、何を指すのか、なぜ読みにくいか、自然な言い換えを
unnatural_spansに記す。
「隣接文化」「漫画形式」のように意味の説明なしに名詞をつなげた箇所も点検する。
問題があるのにnatural_for_readerをtrueにしない。段落が対象について何かを述べて前進する場合だけargument_progressesを
trueにする。本文を編集せず、judgmentsはpayload.paragraphsにあるparagraph_id
だけを、その順序で一度ずつ返す。candidate_context_paragraphsは文脈として読み、
そこにしかないparagraph_idの判定は返さない。
"""
STAGE_GUIDANCE["redraft_section_heading_v31"] = """
各節の見出しを、最初の段落と最後の段落が実際に述べる中心の関係に合わせて
自然な日本語にする。元の見出しやsection_resultの抽象名詞をそのまま写さず、
比較する対象と、その間で問う関係を一つ選ぶ。
「～の可視性」「再前景化」など内容を隠す抽象語は、具体的な関係が分かるなら言い換える。
「欲望表現が別の作品群へ移る」のように抽象物が移動する見出しは避ける。
本文が人物の配役を比べるなら、誰が欲望し、誰の身体が見られるのかを
問う見出しにする。節で確認していない変化や因果を断定しない。
新しい作品情報、因果関係、論点は足さない。判断できなければ元のtextをそのまま返す。
全heading_idを同じ順序で返す。タグ、属性、ID、脚注・文献の見出しは変えない。
"""
STAGE_GUIDANCE["audit_section_heading_v31"] = """
元の見出し、候補、節の第一段落を照合する独立した評価者として判定する。
候補が新しい主張や事実を加えず節の中心を表している場合だけmeaning_preservedをtrueにする。
固定された引用・脚注と矛盾しない場合だけcitation_alignment_preservedをtrueにする。
初見の日本語読者に意味が明瞭な場合だけnatural_japaneseをtrueにする。
全heading_idを同じ順序で返す。編集案は返さない。
"""
STAGE_GUIDANCE["classify_quote_provenance_v31"] = """
本文の引用符付き短文が、筆者が分析のために立てた問いか、資料からの直接引用かを
段落と固定脚注・書誌の文脈から判定する。判定対象はquote_candidatesの全件で、
paragraph_idとliteralを順序も含め正確に返す。直接引用、資料の発言、作品内の
台詞はdirect_source_quoteをtrueにし、author_analytical_questionをfalseにする。
筆者が議論を進めるために自分の言葉で立てた問いだけ後者をtrueにする。
出所を判断できなければ両方falseにして固定する。引用符の形だけで推定しない。
本文の修正案は返さない。
"""

# Workflow node IDs are accepted directly so the runner can dispatch without renaming stages.
STAGE_ALIASES = {
    "review_manuscript_reasoning": "reasoning_review",
    "formulate_research_question": "research_question",
    "build_claim_tree": "claim_tree",
    "plan_research_queries": "research_query_plan",
    "build_argument_graph": "argument_graph",
    "generate_hypotheses": "hypotheses",
    "build_discourse_tree": "discourse_tree",
    "plan_manuscript_structure": "manuscript_structure",
    "lock_section_contracts": "section_contracts",
    "plan_semantic_paragraphs": "semantic_paragraphs",
    "draft_sections": "draft",
    "audit_structure": "audit",
    "polish_prose": "polish",
}

# OpenAI's current model guidance assigns Luna to scoped extraction/triage, GPT-5.6
# Sol low to focused writing and editing, and GPT-5.6 Sol medium to work that needs
# judgment and completeness. Keep this table explicit so a stage cannot silently
# inherit a more expensive (or less capable) default.
STAGE_MODEL = {
    "proofreading_selection": "gpt-5.6-sol",
    "decompose_memo": "gpt-6-luna",
    "classify_propositions": "gpt-6-luna",
    "reasoning_review": "gpt-5.6-sol",
    "research_question": "gpt-5.6-sol",
    "claim_tree": "gpt-5.6-sol",
    "research_query_plan": "gpt-6-luna",
    "argument_graph": "gpt-5.6-sol",
    "hypotheses": "gpt-5.6-sol",
    "discourse_tree": "gpt-5.6-sol",
    "manuscript_structure": "gpt-5.6-sol",
    "section_contracts": "gpt-5.6-sol",
    "semantic_paragraphs": "gpt-5.6-sol",
    "draft": "gpt-5.6-sol",
    "audit": "gpt-5.6-sol",
    "polish": "gpt-5.6-sol",
}

STAGE_REASONING_EFFORT = {
    "proofreading_selection": "low",
    "decompose_memo": "low",
    "classify_propositions": "low",
    "reasoning_review": "medium",
    "research_question": "medium",
    "claim_tree": "medium",
    "research_query_plan": "low",
    "argument_graph": "medium",
    "hypotheses": "medium",
    "discourse_tree": "medium",
    "manuscript_structure": "medium",
    "section_contracts": "medium",
    "semantic_paragraphs": "medium",
    "draft": "medium",
    "audit": "medium",
    "polish": "low",
}

STAGE_MODEL["repair_citation_paragraph_v31"] = "gpt-5.6-sol"
STAGE_REASONING_EFFORT["repair_citation_paragraph_v31"] = "medium"
STAGE_GUIDANCE["repair_citation_paragraph_v31"] = (
    "Repair exactly one manuscript paragraph whose required work title is missing or whose "
    "title-to-reference mapping is ambiguous. Preserve its claim, reasoning, qualification, "
    "sentence role, and all supplied evidence IDs; do not add facts or remove required evidence. "
    "Use only the supplied confirmed bibliography records. Include every required author and "
    "work naturally in the paragraph. Return one citation_mentions record per distinct cited "
    "work, with its exact supplied reference_id and the exact visible title span, including "
    "visible quotation marks. Never choose by author name or list order alone, invent a title, "
    "or return an empty citation list to bypass a required citation. The caller will verify each "
    "title-to-reference mapping and preserve the original paragraph metadata."
)

for _v31_draft_stage in (
    "draft_introduction_v31",
    "draft_body_v31",
    "draft_conclusion_v31",
):
    STAGE_GUIDANCE[_v31_draft_stage] += (
        " Treat support_sentence as internal explanatory metadata, not an exact-match locator "
        "or a required verbatim quote. It may summarize or paraphrase the generated "
        "prose and is not "
        "independent evidence. Paragraph-level evidence_ids are the authoritative link between "
        "evidence and prose; do not reject otherwise valid prose solely because support_sentence "
        "is not verbatim. When a paragraph visibly names a work listed in "
        "reader_facing_scholarship_citation_anchors, add one citation_mentions record with that "
        "work's exact reference_id and the exact reader-facing title span, including its visible "
        "quotation marks. Keep this as separate structured metadata; never emit HTML tags or IDs "
        "in paragraph text, and never infer a title-to-reference match from author names alone. "
        "For every evidence_treatment, set support_paragraph_index and "
        "support_sentence_index to the 1-based paragraph and sentence carrying its actual "
        "claim when the evidence is linked to more than one sentence. The indexes locate "
        "prose, while support_sentence remains an explanatory hint; never attach evidence "
        "to the first sentence merely because the hint is a paraphrase."
    )

STAGE_GUIDANCE["audit_v31"] += (
    " Treat support_sentence as a mapping hint, not a verbatim quote requirement. When an "
    "allowed evidence ID is attached to a generated paragraph, do not report a defect solely "
    "because support_sentence paraphrases that paragraph. Report a missing evidence link only "
    "when the ID is absent from every relevant paragraph."
)
STAGE_GUIDANCE["semantic_paragraphs_v31"] += (
    " Read enumeration_context_contracts.source_statements as the actual "
    "claim, memo-proposition, evidence, and warrant wording behind candidate "
    "lists, not as an order for explaining their items. Select the relation or "
    "observation that advances the section result. Keep the underlying distinctions "
    "traceable in metadata; mention a term in prose only when it helps the reader "
    "understand that result. A relation may be the writer's interpretation and need "
    "not be stated verbatim in a source."
    " If prose_recomposition_feedback_v31 is present, reconsider paragraph boundaries "
    "and the order of explanation for the named sections. Keep claims, evidence, "
    "certainty, and citation obligations. The old rendered wording is not a quality "
    "template; form reader-facing paragraphs from the argument and evidence again."
)
STAGE_GUIDANCE["naturalize_reader_brief_v31"] = (
    "Rewrite the supplied section result, claim wording, evidence wording, warrant wording, "
    "and framing as natural Japanese notes for a skilled academic writer. These notes are "
    "not article prose. Preserve every section and item ID in the same order, and preserve "
    "the object, relationship, uncertainty, time scope, and substantive attribution "
    "to a named source. Source kind, verification status, and source IDs are separate "
    "metadata; never turn them into statements about an input memo, accepted evidence, "
    "or material supplied to the system. An unverified proposition remains a hypothesis "
    "or interpretation, not an externally verified finding. An interpretive_result may "
    "answer a question in the author's own voice; do not recast that answer as an "
    "externally established fact. Do not add checkable facts. "
    "Apply this distinction to introductions, body sections, and conclusions. "
    "Keep substantive limits, but remove editorial decisions about adoption, "
    "bibliographic matching status, and instructions to verify sources. "
    "Replace opaque planning phrases "
    "with ordinary expressions that say who or what does what. For example, explain what "
    "is meant by a phrase like 漫画技法 or 隣接文化 instead of copying its noun chain. "
    "Do not replace a broad object such as 隣接文化 with only one example such as magazine "
    "articles; an example in the evidence does not narrow the stated scope. If the supplied "
    "material does not define that object's full extent, use a broad ordinary phrase without "
    "inventing its members. "
    "Do not turn every listed term into a separate writing instruction. The framing field "
    "should state only the question, scope, or synthesis needed for this section; omit "
    "workflow history and instructions about how to draft. Keep it concise rather than "
    "expanding it into a draft introduction. "
    "Do not compress an actor, several actions, and their effects into a chain of abstract "
    "nouns. State the central relation in ordinary language and leave secondary items in "
    "their individually identified claim or evidence fields. A coined four-kanji compound "
    "is not a substitute for naming who does what to which object. Preserve an established "
    "specialist term when its source and local meaning are supplied. "
    "If audit_feedback is present, inspect "
    "previous_candidate_sections and correct the cited fields without repeating the "
    "rejected wording. If repair_issues is present, change only the named field and item "
    "for each issue; copy every other field exactly from previous_candidate_sections. "
    "Resolve the quoted excerpt with a natural expression while preserving its meaning. "
    "Return all original IDs exactly."
)

# Reader-facing drafts receive a compact card, while the complete locked
# obligations remain available to validators. Do not make the model realize
# every planning list as a list in prose.
STAGE_GUIDANCE["draft_introduction_v31"] += (
    " The introduction is the reader's first encounter with the subject. Begin "
    "with one intelligible scene, medium, or work and explain the question it raises "
    "before naming the conceptual answer. Use writer_directives.introduction as a "
    "direction of travel, not a table of contents. Do not summarize every section, "
    "platform, theme, or action in a single sentence. Give the first body transition "
    "and reserve later examples for the paragraphs that can explain them. A valid "
    "introduction can state its scope and contribution without listing all members "
    "of each category."
)
STAGE_GUIDANCE["draft_body_v31"] += (
    " For a heterogeneous set of magazines, imprints, works, or platforms, select "
    "the subject needed for this paragraph, say what distinguishes it, then move to "
    "the next subject in its own sentence or paragraph. A comma-separated subject "
    "followed by one generic predicate does not establish a comparison. When a work "
    "is an example, introduce why that work tests the present claim before the "
    "interpretive result; use a natural cue such as 例えば when the turn needs it. "
    "primary_work_cards contain catalogue descriptions or corpus excerpts with "
    "their provenance. Use only relevant, supported details to orient a reader; do "
    "not infer a plot or consulted edition from a bare title. If a card has no useful "
    "description, explain the observed relation already present in the section's "
    "evidence instead of inventing narrative facts."
)
STAGE_GUIDANCE["draft_conclusion_v31"] += (
    " Synthesize the developed relation rather than reciting the complete list of "
    "media, works, themes, and actions from the planning material."
)
STAGE_GUIDANCE["compose_body_prose_v31"] += """
既存段落で読点や中黒により三つ以上の対象・動作・抽象語が並んでいる場合、
そのうち節の判断に必要な関係を選び、具体的な主語と述語で説明する。
羅列を短い羅列に縮めたり、四字熟語のような造語一語に置き換えたりしない。
原文が「どの作品が隣り合うか」と行為や配置を述べているなら、
「作品同士の隣接関係」のような名詞へ圧縮しない。誰が作品を同じ号や棚に置き、
読者が何を続けて目にするのかを、出典と原文の確実性を保って文で述べる。
「欲望表現が別の媒体へ移る」のように抽象物を動かす表現は、作品間で
誰が欲望し、誰の身体が見られ、その描かれ方がどう異なるかへ展開する。
この例示の作品配置や描写を、資料にない事実として他の節へ転用しない。
意味の確立した「通過儀礼」などの専門用語は、不自然な造語として消さない。
比較対象が異なる水準なら、各対象が何を示すかを別の文で述べる。
作品カードの概要は出所を確認した範囲でだけ使い、未確認のあらすじを補わない。
具体例へ移る箇所では、必要なら「例えば」などで論旨上の役割を示す。
"""
STAGE_GUIDANCE["audit_reader_brief_v31"] = (
    "Independently compare the original semantic brief and its proposed reader-facing "
    "wording for each section. Judge whether the same substantive claim, uncertainty, "
    "scope, evidence relation, and substantive attribution to a named source survive. "
    "Provenance such as 'the input memo says' belongs in structured metadata and may "
    "be omitted from the reader-facing wording. A clearer paraphrase or omission "
    "of a redundant planning label is acceptable. A compact framing need not "
    "repeat every listed example, theme, or distinction from the planning notes. "
    "For an introduction, omission of a subsidiary distinction such as works versus "
    "articles or explicit rules versus tacit practices is acceptable when the main "
    "research question and broad scope remain understandable. Do not call such "
    "compression a meaning change merely because an enumeration disappeared. "
    "Return meaning_preserved as the string 'true' or 'marginal' only. "
    "Use 'marginal' whenever preservation is uncertain, including an added or "
    "narrowed claim, stronger cause, changed scope, or changed citation meaning. "
    "Do not hide a material concern or convert it to 'true' to pass the stage: "
    "describe it concretely in reason for final human review. "
    "A plausible authorial inference that is not explicit in the original is also "
    "marginal for the internal review record, but it may still be written directly "
    "as the section's interpretation. Distinct historical streams may also interact; "
    "record their newly inferred relation as marginal without requiring hypothesis "
    "wording in every paragraph. Keep the distinction between the streams and their comparison "
    "somewhere in the section, rather than requiring every field to repeat the method. "
    "For every marginal inference, record its exact original and candidate "
    "excerpts, their respective fields and item IDs, and the reasoning in "
    "marginal_inferences. The source and candidate may be in different fields. "
    "These records go to "
    "human review outside the article. Return an empty marginal_inferences array for "
    "'true'. If an exact field-level excerpt cannot be identified, keep the "
    "marginal_inferences array empty and explain the concern in reason; the section "
    "will still be listed for human review. Do not reject a qualified "
    "interpretation solely because its relation was not stated verbatim. Also judge "
    "whether the new "
    "wording narrows a broad research object to one example found in the evidence; for "
    "instance, 隣接文化 cannot silently become magazine articles alone. Treat that as a "
    "scope change even when the example is genuine. Also judge whether the new "
    "Japanese uses natural word combinations and excludes workflow or prompt language. "
    "Return one verdict for every section ID in the same order. Give a concrete reason "
    "for every 'marginal' meaning value and every false boolean; do not treat "
    "fidelity to the original wording as a quality goal. "
    "For each unnatural Japanese expression, add a japanese_issues entry identifying its "
    "exact candidate field, item ID (empty for section_result or framing), verbatim "
    "excerpt, explanation, and suggested rewrite. Return an empty japanese_issues array "
    "only when natural_japanese is true. Assess individual collocations, not only the "
    "overall section. If repair_issues is present, only the repaired fields are supplied: "
    "verify their meaning, naturalness, and absence of internal language without reopening "
    "unrelated fields of the section. For each internal or editorial-management "
    "phrase, return an internal_issues entry with exact candidate field, item ID, "
    "verbatim excerpt, reason, and suggested rewrite. Return an empty internal_issues "
    "array only when internal_language_absent is true. Ordinary scholarly limits "
    "and qualified interpretations are not internal language."
)
for _stage in (
    "draft_introduction_v31", "draft_body_v31", "draft_conclusion_v31",
):
    STAGE_GUIDANCE[_stage] += (
        " Build each paragraph around a useful relation or observation in the "
        "audited reader_content_brief. The evidence projection supplies source facts "
        "and citation lineage, not wording to imitate. An interpretive relation may "
        "be reasoned from the supplied material or by the model; record that "
        "judgment in inference_ledger rather than inserting a routine caveat "
        "into the prose. If prose_recomposition_feedback_v31 is present, "
        "redraft the affected sections from this reader brief, resolving the cited "
        "language problems without preserving old sentence structure or inventing facts."
    )
    STAGE_GUIDANCE[_stage] += (
        " Draft fresh reader-facing paragraphs from the audited brief and supplied material; "
        "do not imitate any old manuscript wording or convert every brief item into a "
        "sentence. For each substantive interpretive judgment not directly stated in "
        "the supplied material, add an inference_ledger record at the same time as "
        "the prose. Give the exact paragraph_index and a verbatim anchor_excerpt, "
        "the judgment and its reasoning, known basis_ids when available, and the "
        "related claim_id or null. Use inferred_from_supplied_material when the "
        "reasoning draws on supplied statements and give at least one known basis ID; use "
        "model_judgment_without_direct_statement when it does not. An empty ledger "
        "is valid only when no such judgment appears. The ledger is internal: do "
        "not print provenance labels or a routine uncertainty disclaimer in the article. "
        "Do not present invented dates, quotations, publication facts, or source IDs "
        "as verified facts."
        " Use new_propositions for proposed changes to the argument. Use "
        "inference_ledger for interpretive judgments actually written in prose; "
        "a ledger entry is not a new evidence ID or a verified citation."
        " An interpretive causal reading may be argued at its actual scope when "
        "its reasoning is recorded; do not turn it into an externally established fact."
    )

STAGE_GUIDANCE["japanese_diagnosis_v31"] = (
    "Diagnose the supplied Japanese scholarly paragraphs without editing them. "
    "Return exactly one diagnosis for every block_id in input order. Evaluate "
    "vocabulary, natural word combinations, subject-predicate and particle syntax, "
    "demonstrative references, excessive nominalisation and abstraction, repeated "
    "constructions/endings, and academic register independently. A technical term is "
    "not an error merely because it is specialised or contains 化. Distinguish defined "
    "terms from ordinary explanatory wording. Also find definition chains, repeated "
    "self-reference, paragraphs that describe a research plan instead of doing analysis, "
    "ambiguous 化 formations, internal correction or response language, and predicates "
    "that do not fit their subjects or objects. For each sentence distinguish the "
    "topic, the predicate's actor, and its object. An omitted actor is acceptable "
    "when readers can recover it; an opening must establish its topic. Test a shared "
    "predicate against every object and compare items at the same conceptual level. "
    "Identify terms and source names that must "
    "remain. Do not revise the argument, citations, claims or qualifications."
)
STAGE_GUIDANCE["japanese_paragraph_redraft_v31"] = (
    "Recompose the original paragraphs as natural Japanese humanities or social "
    "science prose from their claims, observations, source relations, and adjacent context. "
    "Do not follow the old sentence sequence when it creates a list of definitions or "
    "procedural commentary. Use diagnoses and any previous failed-attempt feedback. "
    "Correct each finding rather than returning the same failed wording. Introduce "
    "the topic clearly in an opening paragraph. Do not insert 本稿 mechanically; "
    "redesign a mismatched topic-predicate sentence. Research questions and section "
    "conclusions may be reworded while retaining meaning, scope, certainty and evidence. "
    "A question about whether a relation exists must not become a claim that it exists. "
    "You may split "
    "or combine sentences inside one paragraph, change word order, state a subject, and "
    "replace unnatural noun chains with verbs. Do not change paragraph boundaries, "
    "claims, contrasts, caveats, counterarguments, source attribution, numbers, direct "
    "quotations, titles, footnote markers or defined technical terms. Keep every source name "
    "needed for a citation anchor in the same paragraph. Return only changed plain "
    "paragraph text, never HTML or new IDs. Develop the actual analysis before explaining "
    "the method. Avoid mechanical synonym substitution, routine caveats, repetitive "
    "authorial subjects, and uniform short sentences."
)
STAGE_GUIDANCE["japanese_section_review_v31"] = (
    "Read the consecutive paragraphs as a section, including the neighbouring section "
    "excerpts. Rate each of the seven supplied Japanese-quality dimensions exactly once, "
    "independently, for the exact candidate paragraphs supplied. Check repeated "
    "demonstratives, transitions, sentence patterns, and endings, as well as the flow "
    "between concrete description and abstraction. Check whether a sequence of paragraphs "
    "advances the argument rather than repeating term definitions, prospective methods, "
    "or generic qualifications. Set flow_needs_revision true only when the section's "
    "paragraph flow has a concrete defect that requires rewriting. In that case, list "
    "only actionable defects in flow_findings. When the flow is acceptable, set "
    "flow_needs_revision false and return an empty flow_findings list. Do not put praise, "
    "neutral summaries, or descriptions of what the paragraphs do in flow_findings. "
    "Explain each unacceptable dimension. Evaluate Japanese "
    "style only; the argument and evidence are checked by a separate comparison. "
    "For the first paragraph, check topic establishment and whether omitted actors "
    "are recoverable. Inspect predicate-object fit, modifier scope, parallel comparisons, "
    "natural collocations and flow; do not decide by forbidden-word counts."
)
STAGE_GUIDANCE["japanese_argument_preservation_v31"] = (
    "Compare each original paragraph with its rewritten paragraph in the context of "
    "the locked section contracts. Check every block ID independently. Preserve the "
    "core claim, material distinctions, limits, negations, causal direction, uncertainty, "
    "attribution, counterargument, and the relationship between evidence and claim. "
    "The original paragraph is not a wording template: do not reject a candidate merely "
    "because it removes a redundant example, changes a metaphor, or combines explanatory "
    "details that do not change the proposition's scope. Evaluate preserved meaning across "
    "the section: an auxiliary distinction may be omitted from one paragraph when it is "
    "not needed for the section's central judgment. Do not require every old list item or "
    "the explicit pair '明示的規則と暗黙の慣行' or '作品と記事' in the same paragraph. "
    "A new concrete example is allowed "
    "only when it is an explicit interpretation of supplied content and does not assert a "
    "new bibliographic or historical fact. Mark preserved false for an actual substantive "
    "loss, addition, or change, and identify its effect on the claim rather than citing a "
    "surface word difference. Do not rate the Japanese style here. Give an actionable "
    "explanation for every failure."
)

_V31_EXPLANATORY_PROSE_POLICY = (
    " Keep the locked claim, evidence, warrant, counterargument, qualification, "
    "scope, and source relations in structured metadata. In reader-facing prose, "
    "establish an object-level judgment rather than narrating the discourse plan, "
    "contract, audit, or verification process. A list is a clue to several possible "
    "relations, not a checklist to define item by item. Choose the relation, change, "
    "tension, or observation that moves the argument; introduce other terms only "
    "when the reader needs them. Explain a theorist's idea and proceed naturally "
    "to the subject being analysed. Writer agency can be clear without repeated "
    "本稿, 筆者, or ここでは openings. Keep genuine qualifications, citations, and "
    "defined terms. Allow an interpretive judgment beyond a verbatim source "
    "statement and record its basis and reasoning in inference_ledger at drafting. "
    "Later revisions may restate those judgments but must not introduce a new "
    "substantive interpretation without an inference record. "
    "Do not present invented dates, quotations, or bibliographic details as facts. "
    "Do not add length to prove that every concept has been explained."
    " Treat a sequence of three or more media, works, themes, or actions as planning "
    "material, not as a sentence to copy. Choose the one example or contrast that makes "
    "the present judgment intelligible; introduce other items in their own later contexts "
    "only if they contribute distinct reasoning. Do not replace a list with a shorter list "
    "or a single opaque umbrella noun. When several actions matter, give their actor, "
    "order, and effect in sentences instead of joining nominalizations with ・ or commas. "
    "A reader must understand why a named example was chosen before seeing the conclusion "
    "drawn from it. Mark a turn from a general point to an example with a natural cue such "
    "as 例えば or この点は…に見られる when the turn would otherwise be unclear, without "
    "mechanically prefixing every work title. Expand an unfamiliar compact noun phrase "
    "into who or what acts, on what, and where or for whom; retain established technical "
    "terms such as 通過儀礼 when they carry an identifiable scholarly meaning."
)
for _stage in (
    "draft_introduction_v31",
    "draft_body_v31",
    "draft_conclusion_v31",
    "polish_v31",
    "japanese_paragraph_redraft_v31",
    "repair_citation_paragraph_v31",
):
    STAGE_GUIDANCE[_stage] += _V31_EXPLANATORY_PROSE_POLICY
for _stage in (
    "draft_introduction_v31", "draft_body_v31", "draft_conclusion_v31",
):
    STAGE_GUIDANCE[_stage] += (
        " The V31 reader_content_brief is the only prose-planning input. Its claims, "
        "evidence, warrants, and section result are semantic obligations, not sentences "
        "to copy. Its short writer_directives carry the introduction's question, "
        "provisional answer, scope, contribution and argument roadmap; a chosen "
        "relation behind a source enumeration; and selected applications of the "
        "article's central concept. Use those relations in sentences, never as a "
        "list to reproduce. The application retains section tasks, correction history, "
        "other move labels, and citation-review status for validation; "
        "their absence from this payload is intentional. Earlier generic references to "
        "locked_plan, semantic_paragraph_plan, enumeration_context_contracts, and "
        "required move coverage do not apply to this V31 draft. Choose the order, number, "
        "and boundaries of paragraphs from the section's actual analysis. Do not make one "
        "paragraph per claim or a sentence per listed item. The introduction may orient the "
        "reader to the body without enumerating every section; roadmap IDs are not a prose "
        "quota. A central concept introduced in the introduction should do "
        "analytical work in a relevant body section: explain how it operates on "
        "that section's object rather than repeating its definition or dropping "
        "the term after the introduction. Do not use opaque literal translations "
        "such as 感情的欲望 or 再束縛 unless the precise meaning is explained and "
        "the wording is natural in context. Prefer an ordinary predicate naming "
        "the relation. A source mentioned only to demonstrate bibliography identity is internal "
        "review information and must not appear in prose or citation_mentions. A cited "
        "theory must help explain a particular object-level judgment; record an unverified "
        "interpretive application in inference_ledger rather than adding a routine caveat. "
    "Keep the section's central claims, material scope and uncertainty, citation bindings, "
    "and output metadata. Auxiliary enumerated items need not each appear in prose."
    " In the introduction, let the reader encounter one representative medium or scene "
    "before the abstract question. Do not preview every medium, theme, operation, and "
    "section in the opening paragraphs. The full article structure remains in metadata. "
    "When a broad definition is needed, show one concrete action and its consequence, "
    "then state the concept; do not define it with a string of nominalized operations. "
    "In a body section, never make heterogeneous publications, publishers, and genres "
    "the joint subject of a predicate that applies differently to each. Separate their "
    "situations or choose the contrast that explains the section result."
    )
STAGE_GUIDANCE["audit_v31"] += (
    " Check whether the central judgment, material scope, supported relations, "
    "and the writer's use of theory survive. An auxiliary enumeration may be "
    "omitted or merged when its individual items do not alter those obligations; "
    "do not require a pair of examples or categories to recur in the same paragraph. "
    "Judge meaning in context; neither a technical word nor a first-person "
    "construction is by itself a defect. Report an unresolved relation without "
    "inventing a premise or requiring a uniform prose template. Flag a section "
    "that progresses through term definitions or descriptions of planned analysis "
    "without making an object-level judgment. Review inference_ledger as provenance "
    "for model reasoning, not as a demand for routine caveats in the article."
)

STAGE_SCHEMAS["scholarship_attribution_judgment_v31"] = _schema(
    {
        "judgments": _array(
            _record(
                {
                    "candidate_id": _string(),
                    "classification": _string(
                        ["attribution", "contextual_use", "mere_reference", "unclear"]
                    ),
                    "reason": _string(),
                }
            )
        )
    }
)
STAGE_GUIDANCE["scholarship_attribution_judgment_v31"] = (
    "For each supplied unique citation-anchor sentence, judge only the semantic role of "
    "its predicate: does it attribute an intellectual contribution (such as an argument, "
    "interpretation, analysis, concept, or way of understanding) to the named author's "
    "work; does it instead state a specific analytical purpose for using the named work "
    "without claiming its substantive contents are verified; or does it merely mention, "
    "consult, cite, list, or refer to that work? Return "
    "attribution only when the sentence's predicate makes the source's intellectual "
    "contribution part of the sentence's asserted content. Return contextual_use when "
    "the predicate states what concrete comparison, question, distinction, or framing "
    "the writer uses the work for, while making no claim that the work proves a factual "
    "point. A bare reference or an unexplained disclaimer remains mere_reference. "
    "For a self-citation, the "
    "sentence must attribute the contribution to the writer's named prior work; merely "
    "saying the writer referred to that work is mere_reference. For example, "
    "筆者は『作品名』で作品の意味を配置から捉えた is attribution, while "
    "『作品名』を配置の比較軸を立てるために参照する is contextual_use, and "
    "筆者は『作品名』を参照した is mere_reference. Judge neither whether the statement "
    "is true nor whether the cited source substantively proves another claim. Do not "
    "re-check or infer author/work identity, source validity, evidence strength, citation "
    "purpose, or claim support; those are handled by separate deterministic contracts. "
    "Do not repair or rewrite the sentence. Return exactly one judgment for each supplied "
    "candidate_id and no other IDs; use unclear when the predicate's attribution cannot "
    "be determined from the sentence."
)

for _stage in (
    "research_question_v31",
    "research_problem_v31",
    "argument_graph_v31",
    "defense_obligations_v31",
    "section_contracts_v31",
    "semantic_paragraphs_v31",
    "naturalize_reader_brief_v31",
    "audit_reader_brief_v31",
    "draft_introduction_v31",
    "draft_body_v31",
    "draft_conclusion_v31",
    "audit_v31",
    "polish_v31",
    "japanese_diagnosis_v31",
    "japanese_paragraph_redraft_v31",
    "japanese_section_review_v31",
    "japanese_argument_preservation_v31",
    "diagnose_body_prose_v31",
    "extract_paragraph_meaning_v31",
    "compose_body_prose_v31",
    "polish_final_japanese_v31",
    "redraft_body_prose_v31",
    "audit_redrafted_prose_v31",
    "verify_redraft_claims_v31",
    "audit_predicate_sentences_v31",
    "review_reader_prose_v31",
    "redraft_section_heading_v31",
    "audit_section_heading_v31",
    "classify_quote_provenance_v31",
):
    STAGE_MODEL[_stage] = "gpt-5.6-sol"
    STAGE_REASONING_EFFORT[_stage] = "medium"

STAGE_MODEL["scholarship_attribution_judgment_v31"] = "gpt-5.6-sol"
STAGE_REASONING_EFFORT["scholarship_attribution_judgment_v31"] = "medium"

PROMPT_RENDERER_VERSION = "authoring-codex-prompt-v30"
PROMPT_POLICY = "structured-authoring-v1"
CODEX_EXECUTION_POLICY = "codex-cli-ephemeral-read-only-v1"


@dataclass(frozen=True)
class AuthoringRequest:
    stage: str
    canonical_stage: str
    payload: dict[str, Any]
    context: str
    schema: dict[str, Any]
    guidance: str
    model: str
    reasoning_effort: str
    prompt: str
    prompt_policy: str = PROMPT_POLICY
    prompt_renderer_version: str = PROMPT_RENDERER_VERSION
    execution_policy: str = CODEX_EXECUTION_POLICY


def describe_authoring_request(
    stage: str,
    payload: dict[str, Any],
    context: str = "",
    *,
    model: str | None = None,
) -> AuthoringRequest:
    """Render the exact V30 request for execution, hashing, and diagnostics."""

    canonical_stage = STAGE_ALIASES.get(stage, stage)
    if canonical_stage not in STAGE_SCHEMAS:
        raise FukamiruError(f"Unknown authoring stage: {stage}")
    _validate_codex_output_schema(STAGE_SCHEMAS[canonical_stage])
    output_language = str(payload.get("output_language") or payload.get("language") or "").strip()
    language_contract = ""
    if output_language:
        language_contract = (
            f"Output language: {output_language}. Write every generated natural-language "
            "field in that language. Stable IDs, schema enums, proper names, citations, and "
            "verbatim source_span values are exempt.\n"
        )
    guidance = STAGE_GUIDANCE[canonical_stage]
    prompt_prefix = (
        "You are a structured authoring stage in a research system. Return only JSON matching "
        "the supplied schema. Preserve stable IDs from the input. Add generated material only "
        "with explicit provenance; distinguish retrieved sources from inference and mark "
        "unverified information honestly.\n"
        "When human_question_feedback is supplied, apply its answers and overall_comments "
        "during generation, revision and review. A llm_overreach decision rejects that request "
        "as excessive: do not repeat the same demand for the same target. This is an author "
        "decision, never external evidence or permission to invent sources, upgrade source "
        "verification, or accept broken references.\n"
        f"{language_contract}"
        f"Stage: {stage}\nTask: {guidance}\n\n"
    )
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return AuthoringRequest(
        stage=stage,
        canonical_stage=canonical_stage,
        payload=payload,
        context=context,
        schema=STAGE_SCHEMAS[canonical_stage],
        guidance=guidance,
        model=model or STAGE_MODEL[canonical_stage],
        reasoning_effort=STAGE_REASONING_EFFORT[canonical_stage],
        prompt=(
            prompt_prefix + f"Retrieved context (may be empty):\n{context or '(none)'}\n\n"
            f"Input JSON:\n{payload_json}"
        ),
    )


class CodexAuthoringAdapter:
    """Run one schema-constrained authoring stage through ephemeral Codex CLI."""

    model = "gpt-6-luna"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return resolve_codex_cli(
            self.settings.translation.codex_command,
            required_models=set(STAGE_MODEL.values()),
            which=shutil.which,
        ).available

    @staticmethod
    @contextmanager
    def observe_invocations(
        callback: Callable[[dict[str, Any]], None],
        metadata: dict[str, Any] | None = None,
    ):
        token = _AUTHORING_INVOCATION_OBSERVER.set((callback, dict(metadata or {})))
        try:
            yield
        finally:
            _AUTHORING_INVOCATION_OBSERVER.reset(token)

    @staticmethod
    def execution_contract(stage: str) -> dict[str, Any]:
        canonical_stage = STAGE_ALIASES.get(stage, stage)
        if canonical_stage not in STAGE_SCHEMAS:
            raise FukamiruError(f"Unknown authoring stage: {stage}")
        return {
            "canonical_stage": canonical_stage,
            "model": STAGE_MODEL[canonical_stage],
            "reasoning_effort": STAGE_REASONING_EFFORT[canonical_stage],
            "schema": STAGE_SCHEMAS[canonical_stage],
            "prompt_policy": "structured-authoring-v1",
            "guidance_sha256": hashlib.sha256(STAGE_GUIDANCE[canonical_stage].encode()).hexdigest(),
        }

    @staticmethod
    def validate_result(stage: str, result: dict[str, Any]) -> None:
        contract = CodexAuthoringAdapter.execution_contract(stage)
        _validate_schema(result, contract["schema"])

    @staticmethod
    def _prepare_worker_home(user_home: Path, temp_dir: Path) -> Path:
        """Create a private writable Codex home for one authoring invocation."""
        source_home = user_home / ".codex"
        auth_path = source_home / "auth.json"
        if not auth_path.is_file():
            raise FukamiruError(
                "Codex authentication is unavailable; run `codex login status` first."
            )
        worker_home = temp_dir / "codex-home"
        worker_home.mkdir()
        shutil.copy2(auth_path, worker_home / auth_path.name)
        return worker_home

    @staticmethod
    def _prepare_windows_ca_bundle(worker_home: Path) -> Path | None:
        """Export Windows trust roots for the native Codex TLS client."""
        enum_certificates = getattr(ssl, "enum_certificates", None)
        if os.name != "nt" or enum_certificates is None:
            return None
        certificates = [
            ssl.DER_cert_to_PEM_cert(encoded)
            for encoded, encoding_type, _trust in enum_certificates("ROOT")
            if encoding_type == "x509_asn"
        ]
        if not certificates:
            return None
        bundle = worker_home / "windows-root-ca.pem"
        bundle.write_text("".join(certificates), encoding="ascii")
        return bundle

    def generate(
        self,
        stage: str,
        payload: dict[str, Any],
        context: str = "",
        *,
        observation_callback: Callable[[dict[str, Any]], None] | None = None,
        observation_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract = self.execution_contract(stage)
        request = describe_authoring_request(stage, payload, context, model=str(contract["model"]))
        canonical_stage = request.canonical_stage
        executable = require_codex_cli(
            self.settings.translation.codex_command,
            required_models={request.model},
            which=shutil.which,
        ).resolved_executable
        assert executable is not None

        job_root = self.settings.paths.runtime_root / "tmp" / "codex-authoring"
        job_root.mkdir(parents=True, exist_ok=True)
        schema = request.schema
        model = request.model
        reasoning_effort = request.reasoning_effort
        prompt = request.prompt
        prompt_prefix = prompt[: prompt.index("Retrieved context")]
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        started = time.monotonic()
        stdout = ""
        output_characters = 0
        status = "failed"
        error_kind: str | None = None
        try:
            try:
                with tempfile.TemporaryDirectory(prefix="job-", dir=job_root) as temp_name:
                    temp_dir = Path(temp_name)
                    user_home = Path.home()
                    worker_home = self._prepare_worker_home(user_home, temp_dir)
                    schema_path = temp_dir / "schema.json"
                    output_path = temp_dir / "output.json"
                    schema_path.write_text(json.dumps(schema), encoding="utf-8")
                    args = [
                        executable,
                        "exec",
                        "--ephemeral",
                        "--ignore-user-config",
                        "--ignore-rules",
                        "--json",
                        "--sandbox",
                        "read-only",
                        "--skip-git-repo-check",
                        "--model",
                        model,
                        "-c",
                        f"model_reasoning_effort={reasoning_effort}",
                        "--output-schema",
                        str(schema_path),
                        "-o",
                        str(output_path),
                        "-",
                    ]
                    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
                        command = [
                            os.environ.get("COMSPEC", "cmd.exe"),
                            "/d",
                            "/s",
                            "/c",
                            subprocess.list2cmdline(args),
                        ]
                    else:
                        command = args
                    environment = os.environ.copy()
                    environment["NO_COLOR"] = "1"
                    environment["PYTHONUTF8"] = "1"
                    environment["PYTHONIOENCODING"] = "utf-8"
                    environment["HOME"] = str(user_home)
                    environment["CODEX_HOME"] = str(worker_home)
                    ca_bundle = self._prepare_windows_ca_bundle(worker_home)
                    if ca_bundle is not None:
                        environment["SSL_CERT_FILE"] = str(ca_bundle)
                        environment["REQUESTS_CA_BUNDLE"] = str(ca_bundle)
                    for variable in (
                        "OPENAI_API_KEY",
                        "OPENAI_BASE_URL",
                        "OPENAI_ORG_ID",
                        "OPENAI_PROJECT_ID",
                        "CODEX_API_KEY",
                    ):
                        environment.pop(variable, None)
                    try:
                        process = subprocess.run(
                            command,
                            input=prompt,
                            text=True,
                            encoding="utf-8",
                            errors="strict",
                            capture_output=True,
                            cwd=temp_dir,
                            timeout=self.settings.translation.timeout_seconds,
                            env=environment,
                            check=False,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise FukamiruError(
                            f"Codex authoring stage {stage} timed out after "
                            f"{self.settings.translation.timeout_seconds}s"
                        ) from exc
                    stdout = process.stdout or ""
                    if process.returncode != 0:
                        diagnostics = []
                        if process.stderr and process.stderr.strip():
                            diagnostics.append("stderr:\n" + process.stderr.strip())
                        if process.stdout and process.stdout.strip():
                            diagnostics.append("stdout:\n" + process.stdout.strip())
                        detail = "\n".join(diagnostics) or "unknown Codex error"
                        detail = detail[-6000:]
                        if "invalid_json_schema" in detail.casefold():
                            raise NonRetryableFukamiruError(
                                f"Codex authoring stage {stage} rejected its output schema: "
                                f"{detail}"
                            )
                        if any(
                            marker in detail.casefold()
                            for marker in (
                                "at capacity",
                                "rate limit",
                                "rate_limit",
                                "too many requests",
                                "usage limit",
                                "quota exceeded",
                                "overloaded",
                            )
                        ):
                            raise ExternalServiceUnavailable(
                                f"Codex authoring stage {stage} is temporarily unavailable: "
                                f"{detail}"
                            )
                        raise FukamiruError(
                            f"Codex authoring stage {stage} failed "
                            f"(exit {process.returncode}): {detail}"
                        )
                    if not output_path.is_file():
                        raise FukamiruError(
                            "Codex did not create the structured authoring output"
                        )
                    raw_output = output_path.read_text(encoding="utf-8")
                    output_characters = len(raw_output)
                    try:
                        result = json.loads(raw_output)
                    except json.JSONDecodeError as exc:
                        raise FukamiruError("Codex returned invalid authoring JSON") from exc
                    _validate_schema(result, schema)
                    status = "completed"
                    return result
            except OSError as exc:
                raise FukamiruError(
                    f"Could not run Codex authoring stage {stage}: {exc}"
                ) from exc
        except Exception as exc:
            error_kind = type(exc).__name__
            raise
        finally:
            bound_observer = _AUTHORING_INVOCATION_OBSERVER.get()
            callback = observation_callback or (bound_observer[0] if bound_observer else None)
            metadata = (
                dict(observation_metadata or {})
                if observation_callback is not None
                else dict(bound_observer[1])
                if bound_observer
                else {}
            )
            if callback is not None:
                observation = {
                    "stage": canonical_stage,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "status": status,
                    "prompt_characters": len(prompt),
                    "prompt_utf8_bytes": len(prompt.encode("utf-8")),
                    "payload_characters": len(payload_json),
                    "context_characters": len(context),
                    "schema_characters": len(json.dumps(schema, separators=(",", ":"))),
                    "output_characters": output_characters,
                    "payload_sha256": hashlib.sha256(payload_json.encode()).hexdigest(),
                    "prompt_prefix_sha256": hashlib.sha256(prompt_prefix.encode()).hexdigest(),
                    "latency_seconds": time.monotonic() - started,
                    "error_kind": error_kind,
                    "usage": extract_codex_usage(stdout),
                    "metadata": metadata,
                }
                with suppress(Exception):
                    callback(observation)


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON Schema subset used here, including nested required fields."""
    expected = schema.get("type")
    if isinstance(expected, list):
        if value is None and "null" in expected:
            return
        expected = next((kind for kind in expected if kind != "null"), expected[0])
    valid = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected in valid and not valid[expected](value):
        raise FukamiruError(f"Codex output schema violation at {path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise FukamiruError(f"Codex output schema violation at {path}: invalid value")
    if "const" in schema and value != schema["const"]:
        raise FukamiruError(
            f"Codex output schema violation at {path}: value must be {schema['const']!r}"
        )
    if expected == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise FukamiruError(f"Codex output schema violation at {path}: missing {key}")
        if schema.get("additionalProperties") is False:
            extra = value.keys() - properties.keys()
            if extra:
                raise FukamiruError(
                    f"Codex output schema violation at {path}: unexpected {next(iter(extra))}"
                )
        for key, child in properties.items():
            if key in value:
                _validate_schema(value[key], child, f"{path}.{key}")
    elif expected == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
            "maxItems", float("inf")
        ):
            raise FukamiruError(f"Codex output schema violation at {path}: invalid item count")
        for index, child in enumerate(value):
            _validate_schema(child, schema["items"], f"{path}[{index}]")
    elif expected == "number":
        if value < schema.get("minimum", float("-inf")) or value > schema.get(
            "maximum", float("inf")
        ):
            raise FukamiruError(f"Codex output schema violation at {path}: number out of range")
    elif expected == "integer" and value < schema.get("minimum", float("-inf")):
        raise FukamiruError(f"Codex output schema violation at {path}: integer below minimum")
