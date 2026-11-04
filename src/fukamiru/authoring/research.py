"""Deterministic research obligations for structure-first authoring."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

_MEMO_ANNOTATION_PATTERN = re.compile(
    r"[\(（](?P<label>(?:理|他|要)(?:/[^:：()（）]{1,24})?)[:：]"
    r"\s*(?P<detail>.*)[\)）]\s*$"
)

_EMPIRICAL_TYPES = {
    "fact",
    "observation",
    "interpretation",
    "hypothesis",
    "example",
}
_REQUIRED_TYPES = {"fact", "observation", "example"}
_PREFERRED_TYPES = {"interpretation", "hypothesis", "analysis", "synthesis"}
_NONEMPIRICAL_TYPES = {"scope", "method", "definition"}
_DISCOURSE_PHRASES = (
    "以上より",
    "以上から",
    "以上を踏まえ",
    "以上",
    "以下のように",
    "以下",
    "前述した",
    "前述の",
    "前述",
    "後述する",
    "後述の",
    "後述",
    "このように",
    "このことから",
    "したがって",
    "つまり",
    "一方で",
    "なお",
    "また",
    "さらに",
    "しかし",
    "ただし",
)
_NONEMPIRICAL_PATTERNS = (
    re.compile(r"本稿|本研究|本論文|本章|ここでは|本節"),
    re.compile(
        r"(?:論じ|扱|検討|考察)(?:る|する)?(?:話題|論点|対象|範囲)"
        r".{0,32}(?:含まれ|含む|位置づけ|挙げ)"
    ),
    re.compile(
        r"(?:話題|論点|対象|範囲)(?:に|には|として).{0,32}"
        r"(?:含まれ|含む|がある|が挙げられ|を挙げ)"
    ),
    re.compile(
        r"(?:対象|範囲|議論|分析|検討|定義|比較|考察)(?:を|として|に)?"
        r"(?:扱|限定|定義|用い|行|進め|位置づけ)"
    ),
    re.compile(r"(?:対象|範囲).{0,12}(?:限定|限る|絞る)"),
    re.compile(r"(?:方法|手法|分析法).{0,12}(?:用いる|採用する|定める)"),
    re.compile(r"(?:focus(?:es)? on|scope is|we define|this paper defines)", re.IGNORECASE),
)
_META_CAVEAT_PATTERNS = (
    re.compile(r"(?:後続|後段|次の段階|今後).{0,24}(?:検証|確認|調査|分析)(?:する|が必要|を要する)"),
    re.compile(r"(?:要検証|未検証|検証が必要|確認が必要|調査が必要|今後の課題)"),
)
_QUALIFIED_INFERENCE_PATTERNS = (
    re.compile(
        r"(?:メモ(?:上)?(?:では|に|上の)|仮説|候補|可能性|"
        r"と考えられる|と考えられている|と捉えられる|として捉えられる|"
        r"と整理されている|として整理されている|"
        r"と記述されている|として記述されている|"
        r"と論じている|と論じられている|として論じている|"
        r"と説明されている|として説明されている|"
        r"という見立てがある|という解釈が提示されている|"
        r"という観察がある|と位置づけられる|"
        r"枠組みが提示されている|方向が示されている|検討する|検討できる)"
    ),
    re.compile(
        r"\b(?:hypothes(?:is|ize[sd]?)|may|might|could|suggests?|"
        r"can be (?:read|understood|interpreted) as)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:にくい|にくくな|しづらい|やすい|やすくな|しやすい)"),
)
_TEMPORAL_PATTERNS = (
    re.compile(r"(?:19|20)\d{2}年?(?:代|度)?"),
    re.compile(r"(?:明治|大正|昭和|平成|令和)\s*\d+年?"),
    re.compile(r"\b(?:19|20)\d{2}\b"),
)
_FACTUAL_PATTERNS = (
    re.compile(
        r"原因|因果|影響|結果|帰結|促進|抑制|引き起こ|もたら|生み出|"
        r"増加|減少|変化|異なる|相違|上回|下回|相関|関連|"
        r"発生|成立|存在|形成|導入|普及|実施|観察|測定|報告|記録|"
        r"確認|示し|示す|明らか|生じ|転換|開始|終了|参加者|調査対象|データ"
    ),
    re.compile(
        r"\b(?:caused?|causes|causing|effect|impact|increased?|decreased?|"
        r"different|before|after|since|during|observed?|reported?|"
        r"measured?|occurred?|established|introduced|adopted|implemented)\b",
        re.IGNORECASE,
    ),
)
_COMPARATIVE_PATTERNS = (
    re.compile(r"比較|対比|相違|異なる|上回|下回|より|最多|最少|同様"),
    re.compile(r"\b(?:compared?|versus|between|different|more than|less than)\b", re.I),
)

_SOURCE_INTERNAL_PATTERNS = (
    re.compile(r"(?:この|本)?メモ(?:上|内|では|には|に|が)|原稿(?:上|内|では|には|に|が)"),
    re.compile(r"(?:筆者|著者|話者|記録者)(?:は|が).{0,24}(?:述べ|記し|挙げ|整理|観察)"),
    re.compile(r"(?:生ログ|原文|元資料|入力文書).{0,32}(?:同一|再利用|重複|記載|記述|記され|含まれ|存在)"),
    re.compile(r"(?:参照文献(?:一覧)?|見出し|節|章|項目).{0,32}(?:記され|記載|明記|述べ)"),
    re.compile(
        r"(?:加筆|追記|草稿|原稿|本文).{0,32}"
        r"(?:反映|未反映|統合|未統合|採用|不採用|照合|未照合|書き直|削除)"
    ),
)
_PRIMARY_WORK_PATTERNS = (
    re.compile(r"(?:作中|作品内|当該場面|この場面|コマ|台詞|セリフ|描写|画面|頁|ページ)"),
    re.compile(r"[『「].{1,80}[』」].{0,24}(?:では|において|の場面|の描写)"),
)
_BIBLIOGRAPHIC_PATTERNS = (
    re.compile(r"[『「].{1,100}[』」].{0,40}(?:刊行|出版|発行|掲載|初出|連載|著者)"),
    re.compile(r"(?:刊行年|出版年|発行年|初出年|書誌|版元|出版社|掲載誌)"),
)
_THEORETICAL_ATTRIBUTION_PATTERNS = (
    re.compile(r".{1,40}(?:によれば|によると|は).{0,40}(?:論じ|指摘|定義|提唱|説明)"),
)
_RESERVATION_TYPES = {"reservation", "qualification", "caveat", "留保"}


def classify_proposition_verification_scopes(
    propositions: Sequence[Mapping[str, Any]],
    classifications: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Classify the authority needed to realize each atomic proposition."""

    classification_by_id = {
        _identifier(item.get("proposition_id")): _type_name(item.get("type"))
        for item in classifications
        if _identifier(item.get("proposition_id"))
    }
    results: list[dict[str, Any]] = []
    for proposition in propositions:
        proposition_id = _identifier(proposition.get("id"))
        if not proposition_id:
            continue
        text = _clean_text(proposition.get("text"))
        proposition_type = classification_by_id.get(proposition_id) or _type_name(
            proposition.get("proposition_type") or proposition.get("type")
        )
        annotation_kinds = {
            str(item)
            for item in proposition.get("memo_annotation_kinds") or []
            if str(item)
        }
        source_span = str(proposition.get("source_span") or "").strip()
        provenance = proposition.get("provenance")
        verification_status = (
            str(provenance.get("verification_status") or "").strip().casefold()
            if isinstance(provenance, Mapping)
            else ""
        )

        if {"added_theory", "self_citation"}.intersection(annotation_kinds) or any(
            pattern.search(text) for pattern in _THEORETICAL_ATTRIBUTION_PATTERNS
        ):
            scope, requirement, reason = (
                "theoretical_attribution",
                "attribution_required",
                "theory_or_named_attribution",
            )
        elif any(pattern.search(text) for pattern in _SOURCE_INTERNAL_PATTERNS) or (
            "other_speaker" in annotation_kinds
        ):
            scope, requirement, reason = (
                "source_internal",
                "none",
                "memo_or_speaker_internal_statement",
            )
        elif proposition_type in _RESERVATION_TYPES or any(
            pattern.search(text) for pattern in _META_CAVEAT_PATTERNS
        ):
            scope, requirement, reason = "reservation", "none", "explicit_reservation"
        elif proposition_type in _NONEMPIRICAL_TYPES or any(
            pattern.search(text) for pattern in _NONEMPIRICAL_PATTERNS
        ):
            scope, requirement, reason = (
                "method_or_scope",
                "none",
                "method_scope_or_definition",
            )
        elif any(pattern.search(text) for pattern in _PRIMARY_WORK_PATTERNS):
            scope, requirement, reason = (
                "primary_work_close_reading",
                "preferred",
                "primary_work_observation",
            )
        elif any(pattern.search(text) for pattern in _BIBLIOGRAPHIC_PATTERNS):
            scope, requirement, reason = (
                "bibliographic_identity",
                "external_required",
                "bibliographic_identity_fact",
            )
        elif verification_status == "not_applicable" and not any(
            pattern.search(text) for pattern in _TEMPORAL_PATTERNS
        ):
            # Decomposition explicitly marks memo close-reading, editorial state,
            # and analytic synthesis as not externally verifiable.  Respect that
            # contract before a coarse fact/observation type can promote the atom
            # to a hard evidence blocker.  Dates and bibliographic identities above
            # remain fail-closed even when the model emits inconsistent provenance.
            scope, requirement, reason = (
                "analytic_inference",
                "preferred",
                "provenance_marks_verification_not_applicable",
            )
        elif proposition_type in _PREFERRED_TYPES:
            scope, requirement, reason = (
                "analytic_inference",
                "preferred",
                "interpretive_or_hypothetical_atom",
            )
        elif any(
            pattern.search(text) for pattern in _QUALIFIED_INFERENCE_PATTERNS
        ) and not any(
            pattern.search(text)
            for pattern in (*_TEMPORAL_PATTERNS, *_FACTUAL_PATTERNS)
        ):
            scope, requirement, reason = (
                "analytic_inference",
                "preferred",
                "qualified_atom_without_external_fact_signal",
            )
        elif proposition_type in _REQUIRED_TYPES or any(
            pattern.search(text) for pattern in (*_TEMPORAL_PATTERNS, *_FACTUAL_PATTERNS)
        ):
            scope, requirement, reason = (
                "external_historical_fact",
                "external_required",
                "externally_checkable_atomic_fact",
            )
        else:
            scope, requirement, reason = (
                "analytic_inference",
                "preferred",
                "unclassified_atom_kept_qualified",
            )

        results.append(
            {
                "proposition_id": proposition_id,
                "proposition_type": proposition_type or "unclassified",
                "assertion": text,
                "source_span": source_span,
                "source_origin": _origin(proposition.get("provenance")),
                "verification_scope": scope,
                "verification_requirement": requirement,
                "classification_reason": reason,
                "memo_annotation_kinds": sorted(annotation_kinds),
                "memo_annotations": list(proposition.get("memo_annotations") or []),
            }
        )
    return results


def build_claim_proposition_bindings(
    claims: Sequence[Mapping[str, Any]],
    propositions: Sequence[Mapping[str, Any]],
    classifications: Sequence[Mapping[str, Any]],
    verification_scopes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind claim wrappers to atoms without merging their evidence duties."""

    proposition_by_id = {
        _identifier(item.get("id")): item
        for item in propositions
        if _identifier(item.get("id"))
    }
    type_by_id = {
        _identifier(item.get("proposition_id")): _type_name(item.get("type"))
        for item in classifications
        if _identifier(item.get("proposition_id"))
    }
    scope_by_id = {
        _identifier(item.get("proposition_id")): item
        for item in verification_scopes
        if _identifier(item.get("proposition_id"))
    }
    claim_ids_with_children = {
        _identifier(item.get("parent_id"))
        for item in claims
        if _identifier(item.get("parent_id"))
    }
    bindings: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = _identifier(claim.get("id"))
        if not claim_id:
            continue
        linked_ids = _linked_proposition_ids(claim, proposition_by_id)
        direct_id = _identifier(claim.get("proposition_id"))
        claim_level = _level_name(claim.get("level"))
        central_id = direct_id if direct_id in linked_ids else (
            linked_ids[0] if claim_level == "sub" and linked_ids else ""
        )
        for proposition_id in linked_ids:
            proposition_type = type_by_id.get(proposition_id) or _type_name(
                proposition_by_id[proposition_id].get("proposition_type")
                or proposition_by_id[proposition_id].get("type")
            )
            scope = scope_by_id.get(proposition_id, {})
            verification_requirement = str(
                scope.get("verification_requirement") or "preferred"
            )
            if proposition_type in _RESERVATION_TYPES:
                argumentative_role = "qualification"
            elif proposition_type == "example":
                argumentative_role = "example"
            elif proposition_type in _REQUIRED_TYPES:
                argumentative_role = "evidence"
            elif proposition_type in {"hypothesis", "interpretation", "analysis"}:
                argumentative_role = "premise"
            elif proposition_type in _NONEMPIRICAL_TYPES:
                argumentative_role = "method"
            else:
                argumentative_role = "support"
            argument_necessity = (
                "core"
                if proposition_id == central_id
                and (
                    claim_level == "sub"
                    or (claim_level == "main" and claim_id not in claim_ids_with_children)
                )
                else "supporting"
            )
            if verification_requirement in {"external_required", "attribution_required"}:
                realization_policy = (
                    "assert_if_verified"
                    if argument_necessity == "core"
                    else "attribute_or_omit_if_unverified"
                )
            elif str(scope.get("verification_scope") or "") == "source_internal":
                realization_policy = "attribute_to_memo"
            elif verification_requirement == "preferred":
                realization_policy = "qualified_inference"
            elif str(scope.get("verification_scope") or "") == "reservation":
                realization_policy = "retain_qualification"
            else:
                realization_policy = "assert_method_or_scope"
            bindings.append(
                {
                    "binding_id": f"binding:{claim_id}:{proposition_id}",
                    "claim_id": claim_id,
                    "claim_level": claim_level,
                    "proposition_id": proposition_id,
                    "argumentative_role": argumentative_role,
                    "argument_necessity": argument_necessity,
                    "verification_scope": scope.get("verification_scope"),
                    "verification_requirement": verification_requirement,
                    "realization_policy": realization_policy,
                }
            )
    return bindings


def build_atomic_evidence_obligations(
    propositions: Sequence[Mapping[str, Any]],
    verification_scopes: Sequence[Mapping[str, Any]],
    bindings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Create one stable evidence obligation per used atomic proposition."""

    proposition_by_id = {
        _identifier(item.get("id")): item
        for item in propositions
        if _identifier(item.get("id"))
    }
    scope_by_id = {
        _identifier(item.get("proposition_id")): item
        for item in verification_scopes
        if _identifier(item.get("proposition_id"))
    }
    bindings_by_proposition: dict[str, list[Mapping[str, Any]]] = {}
    for binding in bindings:
        proposition_id = _identifier(binding.get("proposition_id"))
        if proposition_id:
            bindings_by_proposition.setdefault(proposition_id, []).append(binding)

    obligations: list[dict[str, Any]] = []
    for proposition_id, proposition_bindings in bindings_by_proposition.items():
        proposition = proposition_by_id.get(proposition_id)
        scope = scope_by_id.get(proposition_id)
        if proposition is None or scope is None:
            continue
        assertion = _clean_text(scope.get("assertion") or proposition.get("text"))
        digest = hashlib.sha256(
            ("authoring-atomic-evidence-v1\x1f" + proposition_id + "\x1f" + assertion).encode()
        ).hexdigest()[:24]
        necessity = (
            "core"
            if any(item.get("argument_necessity") == "core" for item in proposition_bindings)
            else "supporting"
        )
        requirement = str(scope.get("verification_requirement") or "preferred")
        if requirement in {"external_required", "attribution_required"}:
            planned_realization = (
                "assert_if_verified"
                if necessity == "core"
                else "attribute_or_omit_if_unverified"
            )
        elif requirement == "preferred":
            planned_realization = "qualified_inference"
        elif str(scope.get("verification_scope") or "") == "source_internal":
            planned_realization = "attribute_to_memo"
        elif str(scope.get("verification_scope") or "") == "reservation":
            planned_realization = "retain_qualification"
        else:
            planned_realization = "assert_method_or_scope"
        obligations.append(
            {
                "obligation_id": f"evidence:{digest}",
                "proposition_id": proposition_id,
                "assertion": assertion,
                "source_span": str(scope.get("source_span") or ""),
                "source_origin": scope.get("source_origin"),
                "verification_scope": scope.get("verification_scope"),
                "verification_requirement": requirement,
                "argument_necessity": necessity,
                "planned_realization": planned_realization,
                "claim_ids": list(
                    dict.fromkeys(
                        _identifier(item.get("claim_id"))
                        for item in proposition_bindings
                        if _identifier(item.get("claim_id"))
                    )
                ),
                "binding_ids": [
                    _identifier(item.get("binding_id")) for item in proposition_bindings
                ],
                "argumentative_roles": list(
                    dict.fromkeys(
                        str(item.get("argumentative_role") or "support")
                        for item in proposition_bindings
                    )
                ),
                "memo_annotation_kinds": list(
                    scope.get("memo_annotation_kinds") or []
                ),
                "memo_annotations": list(scope.get("memo_annotations") or []),
            }
        )
    return obligations


def build_atomic_research_query_candidates(
    obligations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project atomic duties into bounded search jobs without weakening them."""

    candidates: list[dict[str, Any]] = []
    for obligation in obligations:
        requirement = str(obligation.get("verification_requirement") or "preferred")
        if requirement == "none":
            continue
        assertion = _clean_text(obligation.get("assertion"))
        if not assertion:
            continue
        necessity = str(obligation.get("argument_necessity") or "supporting")
        search_requirement = (
            "required"
            if requirement in {"external_required", "attribution_required"}
            and necessity == "core"
            else "preferred"
        )
        obligation_id = _identifier(obligation.get("obligation_id"))
        claim_ids = [str(item) for item in obligation.get("claim_ids") or [] if str(item)]
        candidates.append(
            {
                "query": _verification_query(assertion, []),
                "query_valid": len(assertion) >= 3,
                "scope_key": f"proposition:{obligation.get('proposition_id')}",
                "finding_id": obligation_id,
                "claim_id": claim_ids[0] if claim_ids else None,
                "claim_ids": claim_ids,
                "proposition_id": obligation.get("proposition_id"),
                "obligation_id": obligation_id,
                "severity": "major" if necessity == "core" else "minor",
                "requires_research": search_requirement == "required",
                "evidence_requirement": search_requirement,
                "verification_requirement": requirement,
                "verification_scope": obligation.get("verification_scope"),
                "argument_necessity": necessity,
                "planned_realization": obligation.get("planned_realization"),
                "allowed_support_modes": _allowed_support_modes(search_requirement),
                "origin": "authoring_atomic_proposition",
                "verification_status": "unverified",
                "linked_proposition_ids": [obligation.get("proposition_id")],
                "memo_annotations": list(obligation.get("memo_annotations") or []),
                "memo_annotation_kinds": list(
                    obligation.get("memo_annotation_kinds") or []
                ),
                "subclaim_ids": [obligation.get("proposition_id")],
                "subclaims": [assertion],
            }
        )
    return candidates


def build_research_lead_query_candidates(
    propositions: Sequence[Mapping[str, Any]],
    classifications: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Search memo leads without turning them into evidence obligations or claims."""

    lead_ids = {
        _identifier(item.get("proposition_id"))
        for item in classifications
        if item.get("argument_role") == "research_lead"
    }
    candidates = []
    for proposition in propositions:
        proposition_id = _identifier(proposition.get("id"))
        assertion = _clean_text(proposition.get("text"))
        if proposition_id not in lead_ids or len(assertion) < 3:
            continue
        candidates.append(
            {
                "query": _verification_query(assertion, []),
                "query_valid": True,
                "scope_key": f"research_lead:{proposition_id}",
                "finding_id": f"lead:{proposition_id}",
                "obligation_id": f"lead:{proposition_id}",
                "proposition_id": proposition_id,
                "claim_ids": [],
                "severity": "info",
                "requires_research": False,
                "evidence_requirement": "preferred",
                "verification_requirement": "preferred",
                "origin": "authoring_research_lead",
                "verification_status": "unverified",
                "linked_proposition_ids": [proposition_id],
                "subclaim_ids": [proposition_id],
                "subclaims": [assertion],
            }
        )
    return candidates


def restore_memo_source_annotations(
    note_text: str,
    propositions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reattach author-supplied source constraints after LLM decomposition.

    The decomposition model may correctly extract the proposition while trimming a
    trailing ``(理: ...)``, ``(他: ...)``, or ``(要: ...)`` note.  Those labels are
    not stylistic debris: they distinguish later-added theory, another speaker's
    statement, and an author-marked verification/reasoning risk.  Match the exact
    returned source span back to its original memo line, restore that complete line,
    and expose a compact machine-readable constraint.  No annotation is invented.
    """

    tagged_lines: list[dict[str, Any]] = []
    for line_index, raw_line in enumerate(note_text.splitlines(), start=1):
        match = _MEMO_ANNOTATION_PATTERN.search(raw_line)
        if match is None:
            continue
        label = match.group("label").strip()
        kinds: list[str] = []
        if "理" in label:
            kinds.append("added_theory")
        if "他" in label:
            kinds.append("other_speaker")
        if "要" in label:
            kinds.append("needs_review")
        if "自己引用" in label:
            kinds.append("self_citation")
        tagged_lines.append(
            {
                "line_index": line_index,
                "line": raw_line.strip(),
                "base": raw_line[: match.start()].rstrip(),
                "annotation": {
                    "label": label,
                    "kinds": kinds,
                    "detail": match.group("detail").strip(),
                    "raw": match.group(0).strip(),
                },
            }
        )

    restored: list[dict[str, Any]] = []
    matched_line_indexes: set[int] = set()
    annotated_proposition_count = 0
    for raw_proposition in propositions:
        proposition = dict(raw_proposition)
        source_span = str(proposition.get("source_span") or "").strip()
        normalized_span = _normalize_memo_match_text(source_span)
        matches: list[dict[str, Any]] = []
        if normalized_span:
            for tagged in tagged_lines:
                normalized_line = _normalize_memo_match_text(str(tagged["line"]))
                normalized_base = _normalize_memo_match_text(str(tagged["base"]))
                if (
                    normalized_span in normalized_line
                    or normalized_span in normalized_base
                    or (
                        len(normalized_base) >= 12
                        and normalized_base in normalized_span
                    )
                ):
                    matches.append(tagged)
        if matches:
            # A source span should normally resolve to one memo line.  If the same
            # sentence occurs more than once, retain every distinct author label.
            annotations: list[dict[str, Any]] = []
            seen_raw: set[str] = set()
            for tagged in matches:
                matched_line_indexes.add(int(tagged["line_index"]))
                annotation = dict(tagged["annotation"])
                if annotation["raw"] in seen_raw:
                    continue
                seen_raw.add(str(annotation["raw"]))
                annotations.append(annotation)
            proposition["source_span"] = str(matches[0]["line"])
            proposition["memo_annotations"] = annotations
            proposition["memo_annotation_kinds"] = sorted(
                {
                    str(kind)
                    for annotation in annotations
                    for kind in annotation.get("kinds", [])
                }
            )
            annotated_proposition_count += 1
        else:
            proposition["memo_annotations"] = []
            proposition["memo_annotation_kinds"] = []
        restored.append(proposition)

    unmatched = [
        {
            "line_index": item["line_index"],
            "line": item["line"],
            "label": item["annotation"]["label"],
        }
        for item in tagged_lines
        if int(item["line_index"]) not in matched_line_indexes
    ]
    return restored, {
        "tagged_source_line_count": len(tagged_lines),
        "matched_tagged_source_line_count": len(tagged_lines) - len(unmatched),
        "annotated_proposition_count": annotated_proposition_count,
        "unmatched_tagged_lines": unmatched,
    }


def preserve_unmatched_memo_source_annotations(
    note_text: str,
    propositions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize an exact memo proposition when decomposition omits a tagged line.

    An author tag is a source constraint, so a complete LLM omission must not be
    repaired by fuzzy-attaching it to an unrelated proposition. Preserve the
    tagged line itself as a supplemental proposition, then run the ordinary
    exact-span restoration again. This keeps the failure closed if even the
    deterministic fallback cannot account for every tag.
    """

    restored, initial_report = restore_memo_source_annotations(note_text, propositions)
    materialized: list[dict[str, Any]] = []
    existing_ids = {str(item.get("id") or "") for item in restored}
    for item in initial_report["unmatched_tagged_lines"]:
        line = str(item.get("line") or "").strip()
        match = _MEMO_ANNOTATION_PATTERN.search(line)
        if match is None:
            continue
        base = line[: match.start()].strip()
        text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", base).strip()
        if not text:
            continue
        line_index = int(item["line_index"])
        proposition_id = f"p-source-annotation-{line_index:04d}"
        suffix = 2
        while proposition_id in existing_ids:
            proposition_id = f"p-source-annotation-{line_index:04d}-{suffix}"
            suffix += 1
        existing_ids.add(proposition_id)
        restored.append(
            {
                "id": proposition_id,
                "text": text,
                "source_span": line,
                "provenance": {
                    "source": "memo",
                    "source_ids": ["note_text"],
                    "verification_status": "unverified",
                },
            }
        )
        materialized.append(
            {
                "line_index": line_index,
                "label": str(item.get("label") or ""),
                "proposition_id": proposition_id,
            }
        )

    restored, report = restore_memo_source_annotations(note_text, restored)
    report["materialized_tagged_source_line_count"] = len(materialized)
    report["materialized_tagged_source_lines"] = materialized
    return restored, report


def _normalize_memo_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", value)
    return "".join(value.split())


def build_research_query_candidates(
    claims: Sequence[Mapping[str, Any]],
    propositions: Sequence[Mapping[str, Any]],
    classifications: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Build one stable, evidence-coverage obligation candidate per claim.

    Claim and proposition provenance are recorded as origin metadata only. A memo
    source is never treated as evidence that an externally checkable claim is true.
    The returned candidates can be passed to the existing bounded query allocator;
    callers should allocate only records whose ``requires_research`` is true.
    """
    proposition_by_id = {
        _identifier(item.get("id")): item
        for item in propositions
        if _identifier(item.get("id"))
    }
    classification_by_id = {
        _identifier(item.get("proposition_id")): _type_name(item.get("type"))
        for item in classifications
        if _identifier(item.get("proposition_id"))
    }
    claims_by_id = {
        _identifier(item.get("id")): item
        for item in claims
        if _identifier(item.get("id"))
    }
    descendants = _descendant_claim_ids(claims)
    candidates: list[dict[str, Any]] = []

    for claim in claims:
        claim_id = _identifier(claim.get("id"))
        text = _clean_text(claim.get("text"))
        if not claim_id or not text:
            continue

        linked_ids = _linked_proposition_ids(claim, proposition_by_id)
        linked = [proposition_by_id[item] for item in linked_ids]
        linked_types = [
            classification_by_id.get(item_id)
            or _type_name(
                proposition_by_id[item_id].get("proposition_type")
                or proposition_by_id[item_id].get("type")
            )
            for item_id in linked_ids
        ]
        memo_annotations = _linked_memo_annotations(linked)
        memo_annotation_kinds = sorted(
            {
                str(kind)
                for annotation in memo_annotations
                for kind in annotation.get("kinds", [])
            }
        )
        claim_type = _type_name(
            claim.get("claim_type") or claim.get("proposition_type") or claim.get("type")
        )
        if not claim_type:
            direct_proposition_id = _identifier(claim.get("proposition_id"))
            claim_type = classification_by_id.get(direct_proposition_id) or _type_name(
                proposition_by_id.get(direct_proposition_id, {}).get("proposition_type")
                or proposition_by_id.get(direct_proposition_id, {}).get("type")
            )
        evidence_requirement = _evidence_requirement(
            text,
            claim_type=claim_type,
            claim_verification_status=_claim_verification_status(claim),
            linked_types=linked_types,
            linked=linked,
        )
        requires_research = evidence_requirement in {"required", "preferred"}
        subclaim_ids = [
            item_id
            for item_id in [claim_id, *descendants.get(claim_id, [])]
            if item_id in claims_by_id
        ]
        subclaims = list(
            dict.fromkeys(
                str(claims_by_id[item_id].get("text") or "").strip()
                for item_id in subclaim_ids
                if str(claims_by_id[item_id].get("text") or "").strip()
            )
        )
        provenance_origin = _origin(claim.get("provenance"))
        query = _verification_query(text, linked)
        level = _level_name(claim.get("level"))
        # A root claim with substantive descendants is a synthesis obligation,
        # not a second, overly broad retrieval query. Its truth is tested through
        # the evidence obligations of those descendants and the later warrant /
        # coherence gates. This also keeps coined umbrella terms out of exact-
        # anchor retrieval filters.
        if level == "main" and descendants.get(claim_id):
            evidence_requirement = "none"
            requires_research = False
        candidates.append(
            {
                "query": query,
                "query_valid": len(query) >= 3,
                "scope_key": f"claim:{claim_id}",
                "finding_id": claim_id,
                "claim_id": claim_id,
                "claim_level": level,
                "block_ids": [],
                "severity": "major" if level in {"main", "sub"} else "minor",
                "requires_research": requires_research,
                "evidence_requirement": evidence_requirement,
                "allowed_support_modes": _allowed_support_modes(evidence_requirement),
                "origin": "authoring_claim_tree",
                "claim_origin": provenance_origin,
                "linked_proposition_origins": {
                    item_id: _origin(linked_item.get("provenance"))
                    for item_id, linked_item in zip(linked_ids, linked, strict=True)
                },
                "verification_status": "unverified",
                "linked_proposition_ids": linked_ids,
                "memo_annotations": memo_annotations,
                "memo_annotation_kinds": memo_annotation_kinds,
                "subclaim_ids": subclaim_ids,
                "subclaims": subclaims or [text],
            }
        )
    return candidates


def select_economical_research_candidates(
    candidates: Sequence[Mapping[str, Any]], *, preferred_limit: int = 4
) -> list[dict[str, Any]]:
    """Keep hard evidence obligations and sample only a few optional searches.

    Authoring can produce many ``preferred`` evidence candidates from one memo.
    Treating every one as an independent retrieval obligation repeats query
    planning, search, and support auditing without improving the mandatory
    evidence gate.  Required claims remain uncapped; optional claims are ranked
    deterministically and bounded for an economical run.
    """

    records = [dict(item) for item in candidates if isinstance(item, Mapping)]
    required_indexes = {
        index
        for index, item in enumerate(records)
        if str(item.get("evidence_requirement") or "") == "required"
    }
    preferred = [
        (index, item)
        for index, item in enumerate(records)
        if str(item.get("evidence_requirement") or "") == "preferred"
    ]
    level_rank = {"sub": 3, "supporting": 2, "main": 1}
    preferred.sort(
        key=lambda pair: (
            -int(bool(pair[1].get("memo_annotation_kinds"))),
            -level_rank.get(str(pair[1].get("claim_level") or ""), 0),
            -int(str(pair[1].get("severity") or "") == "major"),
            pair[0],
        )
    )
    selected_preferred = {
        index for index, _item in preferred[: max(0, int(preferred_limit))]
    }
    for index, item in enumerate(records):
        if index in required_indexes:
            item["requires_research"] = True
            item["research_selection_reason"] = "required_external_evidence"
        elif index in selected_preferred:
            item["requires_research"] = True
            item["research_selection_reason"] = "bounded_preferred_sample"
        else:
            item["requires_research"] = False
            if str(item.get("evidence_requirement") or "") == "preferred":
                item["research_selection_reason"] = "qualified_inference_without_search"
    return records


def _identifier(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _type_name(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("type") or value.get("proposition_type")
    return str(value or "").strip().casefold().replace("-", "_")


def _origin(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("source") or value.get("origin")
    source = str(value or "llm_inference").strip().casefold()
    return (
        source
        if source in {"memo", "rag", "llm_inference", "user", "mixed"}
        else "llm_inference"
    )


def _claim_verification_status(claim: Mapping[str, Any]) -> str:
    provenance = claim.get("provenance")
    if isinstance(provenance, Mapping):
        return str(provenance.get("verification_status") or "").strip().casefold()
    return ""


def _linked_memo_annotations(linked: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for proposition in linked:
        for raw_annotation in proposition.get("memo_annotations") or []:
            if not isinstance(raw_annotation, Mapping):
                continue
            raw = str(raw_annotation.get("raw") or "").strip()
            key = raw or repr(sorted(raw_annotation.items()))
            if key in seen:
                continue
            seen.add(key)
            annotations.append(
                {
                    "label": str(raw_annotation.get("label") or ""),
                    "kinds": [
                        str(item)
                        for item in raw_annotation.get("kinds") or []
                        if str(item)
                    ],
                    "detail": str(raw_annotation.get("detail") or ""),
                    "raw": raw,
                }
            )
    return annotations


def _linked_proposition_ids(
    claim: Mapping[str, Any], propositions: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    values: list[object] = []
    direct = claim.get("proposition_id")
    if direct is not None:
        values.append(direct)
    direct_many = claim.get("proposition_ids")
    if isinstance(direct_many, Sequence) and not isinstance(direct_many, (str, bytes)):
        values.extend(direct_many)
    provenance = claim.get("provenance")
    if isinstance(provenance, Mapping):
        source_ids = provenance.get("source_ids")
        if isinstance(source_ids, Sequence) and not isinstance(source_ids, (str, bytes)):
            values.extend(source_ids)
    return list(dict.fromkeys(item for raw in values if (item := _identifier(raw)) in propositions))


def _descendant_claim_ids(
    claims: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for claim in claims:
        child_id = _identifier(claim.get("id"))
        parent_id = _identifier(claim.get("parent_id"))
        if child_id and parent_id:
            children.setdefault(parent_id, []).append(child_id)

    result: dict[str, list[str]] = {}
    for root in children:
        ordered: list[str] = []
        seen = {root}
        pending = list(children.get(root, []))
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            pending.extend(children.get(current, []))
        result[root] = ordered
    return result


def _evidence_requirement(
    text: str,
    *,
    claim_type: str,
    claim_verification_status: str,
    linked_types: Sequence[str],
    linked: Sequence[Mapping[str, Any]],
) -> str:
    if (
        len(text) <= 48
        and any(pattern.search(text) for pattern in _META_CAVEAT_PATTERNS)
    ):
        return "none"

    # A claim that explicitly presents itself as the memo author's reading, a
    # hypothesis, or a candidate explanation is not converted into a categorical
    # external-fact claim merely because it links to factual propositions.  The
    # factual atoms remain independently verifiable obligations; this synthesis is
    # allowed only as qualified memo/LLM inference when retrieval finds no support.
    if any(pattern.search(text) for pattern in _QUALIFIED_INFERENCE_PATTERNS):
        return "preferred"

    # Explicit dates in the claim itself remain externally checkable even when
    # the model happened to label the surrounding sentence as method/scope.
    if any(pattern.search(text) for pattern in _TEMPORAL_PATTERNS):
        return "required"

    linked_nonempirical = bool(linked_types) and all(
        item_type in _NONEMPIRICAL_TYPES for item_type in linked_types
    )
    explicitly_nonempirical = claim_type in _NONEMPIRICAL_TYPES or linked_nonempirical
    if explicitly_nonempirical and any(
        pattern.search(text) for pattern in _NONEMPIRICAL_PATTERNS
    ):
        return "none"
    if claim_type in _NONEMPIRICAL_TYPES and (
        not linked_types
        or all(item_type in _NONEMPIRICAL_TYPES for item_type in linked_types)
    ):
        return "none"

    # Claim-tree outputs explicitly use not_applicable for memo close-reading,
    # definitions, and analytic synthesis.  Such claims may be researched, but if
    # retrieval fails they must remain attributed/qualified inference instead of
    # becoming hard external-fact blockers.  Explicit dates above still fail closed.
    if claim_verification_status == "not_applicable":
        return "preferred"

    if claim_type in _REQUIRED_TYPES:
        return "required"

    if claim_type in _PREFERRED_TYPES or (
        linked_types
        and any(item_type in _PREFERRED_TYPES for item_type in linked_types)
        and not any(item_type in _REQUIRED_TYPES for item_type in linked_types)
    ):
        return "preferred"

    if any(pattern.search(text) for pattern in _FACTUAL_PATTERNS):
        return "required"

    if any(pattern.search(text) for pattern in _COMPARATIVE_PATTERNS):
        return "required"

    if any(item_type in _REQUIRED_TYPES for item_type in linked_types):
        return "required"

    # Unclassified analytic claims should be searched for, but can proceed only
    # when clearly labelled as inference if no external support is found.
    return "preferred"


def _allowed_support_modes(requirement: str) -> list[str]:
    return {
        "required": ["external_evidence"],
        "preferred": [
            "external_evidence",
            "qualified_memo_inference",
            "qualified_llm_inference",
        ],
        "none": ["no_evidence_required"],
    }[requirement]


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    for phrase in _DISCOURSE_PHRASES:
        text = text.replace(phrase, " ")
    text = re.sub(r"\b(?:claim|proposition|p|c)[-_:#]?\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s\u3000]+", " ", text)
    text = re.sub(r"^[\s、。,:;；]+|[\s、。,:;；]+$", "", text)
    return text.strip()


def _verification_query(claim_text: str, linked: Sequence[Mapping[str, Any]]) -> str:
    parts = [_clip(claim_text, 100)]
    claim_key = _query_key(claim_text)
    for proposition in linked:
        proposition_text = _clean_text(proposition.get("text"))
        if not proposition_text:
            continue
        if _query_key(proposition_text) == claim_key:
            continue
        if any(_query_key(proposition_text) == _query_key(existing) for existing in parts):
            continue
        parts.append(_clip(proposition_text, 55))
        if len(parts) == 3:
            break
    return " / ".join(item for item in parts if item)[:210]


def _query_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _clip(value: str, maximum: int) -> str:
    value = _clean_text(value)
    if len(value) <= maximum:
        return value
    return value[: maximum - 1].rstrip() + "…"


def _level_name(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return {
        "main_claim": "main",
        "subclaim": "sub",
        "supporting_claim": "supporting",
    }.get(normalized, normalized)
