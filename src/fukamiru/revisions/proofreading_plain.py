"""Japanese diagnosis sees coherent prose; DOM addressing stays in the application."""

from __future__ import annotations

import re
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_repair as assets
from fukamiru.revisions.proofreading_context import strict

AUDIT_PROMPT = """
候補段落の日本語を読む確認です。candidate_paragraphsが現在の段落全文です。
本文を書き直したり、書名を補ったりせず、日本語の診断だけを返してください。
語感・連語・頻度に基づく改稿は生成ノードで済んでおり、そのLLM-jp案を採用して進めます。
ここは別の言い方を探す再改稿ではなく、案を本文に組み込む際の破損を確認する工程です。
誤字、語句の脱落・重複、引用符で切れた助詞・述語のつながり、著者名と書名の取り違え、
原文の主体・否定・条件の意図しない変化を確認します。連語の検査は、語句の誤記や欠落に
よって文意が壊れた箇所を特定するために行います。原文と候補のどの関係が壊れたかを示します。
自然に読めて意味と語調が保たれる候補には「問題なし」とします。
学術的に硬い、冗長さを減らせる、より好ましい同義語があるという理由だけでは修正を求めません。
文意を保つ同義表現の選択を再審査しません。
原文と違う言葉であることだけで誤りにしません。人名・書名や確立した
概念を知らないことを誤りの根拠にしません。元の主体・否定・条件が意図せず変わった場合は
その語句を指摘します。原典の真偽や理論の適用可否は判定対象ではありません。
一般語句の「」と『』の選択など表記だけの問題は次の担当が扱うので修復対象にしません。
各段落についてverdictを「問題なし」「修正が必要」「人間確認」から選び、
診断内容をdiagnosisに日本語で説明します。修正すべき問題はdiagnosisに具体的に書きます。
「人間確認」は、日本語の意味自体が判断不能な場合だけです。修正可能な誤字・助詞・述語・
引用符の問題は「修正が必要」にします。paragraphsでは各paragraph_idをキーにし、その中へverdictとdiagnosisを必ず返します。
applied_repairsだけが実際に適用された修復です。空なら修復履歴を想像しません。
修復後は、指摘した破損が解消し、新たな欠落や意味の変更がないかを確認して閉じます。
新たな言い換えの提案を重ねたり、同じ意味の表現間で往復したりしません。
本文や修正案は出力せず、指定JSONだけを返します。
"""

REPAIR_PROMPT = """
候補段落の指摘箇所だけを直します。段落全文の再生成や、指摘外の同義語置換はしません。
生成済み案の語感と硬い学術的語調を尊重します。破損の修復を超える言い換えはしません。
candidate_paragraphsが現在の本文全文、findingsが確認された問題です。
repairsにはparagraph_id、現在の候補に一度だけあるbefore、その部分のafter、reasonを返します。
beforeは候補から正確にコピーし、存在しない空白や記号を足しません。短い語句が複数ある場合は
前後の文脈もbeforeに含め、一意に指定します。afterはその部分だけです。
書名・引用本文を変更しません。書名を修正範囲の前後の目印に含めても、その字句自体は保ちます。
IDや位置計算、スロット分配はアプリが行うので不要です。元の候補の語感・文体を維持します。
全beforeは今回の同じ候補に対する置換です。すでに直した後の文章をbeforeに使いません。
修正が不要と考える指摘はhuman_reviewへ理由を返します。日本語で説明し、指定JSONのみ。
"""


def audit_schema(paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    return strict(
        {
            "paragraphs": strict(
                {
                    p["paragraph_id"]: strict(
                        {
                            "verdict": {
                                "type": "string",
                                "enum": ["問題なし", "修正が必要", "人間確認"],
                            },
                            "diagnosis": {"type": "string"},
                        }
                    )
                    for p in paragraphs
                }
            )
        }
    )


def repair_schema(paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    return strict(
        {
            "repairs": {
                "type": "array",
                "items": strict(
                    {
                        "paragraph_id": {
                            "type": "string",
                            "enum": [p["paragraph_id"] for p in paragraphs],
                        },
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "reason": {"type": "string"},
                    }
                ),
            },
            "human_review": assets.human_schema(paragraphs),
        }
    )


def input_prose(
    context: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "original_paragraphs": [
            {"paragraph_id": p["paragraph_id"], "text": p["visible_text"]} for p in paragraphs
        ],
        "candidate_paragraphs": [
            {"paragraph_id": p["paragraph_id"], "text": p["visible_text"]}
            for p in assets.rendered(context, rows)
        ],
        "applied_repairs": [e for h in history if h.get("applied") for e in h.get("repairs", [])],
    }


def findings(result: dict[str, Any], prose: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {p["paragraph_id"] for p in prose["candidate_paragraphs"]}
    if set(result["paragraphs"]) != expected:
        raise NonRetryableFukamiruError("adoption.audit_coverage_mismatch")
    issues = []
    for pid, row in result["paragraphs"].items():
        if row["verdict"] == "人間確認":
            raise NonRetryableFukamiruError(
                "adoption.language_review_needs_human: " + row["diagnosis"]
            )
        if row["verdict"] == "修正が必要":
            issues.append({"paragraph_id": pid, "reason": row["diagnosis"]})
    return issues


def editable_ranges(template: str, slots: list[str]) -> tuple[str, list[tuple[int, int, int]]]:
    visible = "".join(ET.fromstring(template).itertext())
    matches = list(re.finditer(r"\{\{TEXT:(\d+)\}\}", visible))
    if [int(m[1]) for m in matches] != list(range(len(slots))):
        raise NonRetryableFukamiruError("adoption.ambiguous_editable_template")
    output, ranges, end = "", [], 0
    for m in matches:
        output += visible[end : m.start()]
        index = int(m[1])
        start = len(output)
        output += slots[index]
        ranges.append((start, len(output), index))
        end = m.end()
    output += visible[end:]
    return output, ranges


def apply_repairs(
    context: dict[str, Any], rows: list[dict[str, Any]], changes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = deepcopy(rows)
    originals = {
        p["paragraph_id"]: p for p in context["japanese_proofreading_context"]["plan"]["paragraphs"]
    }
    current = {r["paragraph_id"]: r for r in result}
    ranges = {}
    for pid, row in current.items():
        ranges[pid] = editable_ranges(originals[pid]["html_template"], row["text_slots"])
    grouped: dict[tuple[str, int], list[tuple[int, int, str]]] = {}
    actual = []
    for change in changes:
        pid = change["paragraph_id"]
        if pid not in current:
            raise NonRetryableFukamiruError("adoption.repair_unknown_paragraph")
        text, spans = ranges[pid]
        before, after = change["before"], change["after"]
        if not before or text.count(before) > 1:
            raise NonRetryableFukamiruError("adoption.repair_unresolved_exact_anchor")
        if before == after:
            continue
        offset = text.index(before) if before in text else None
        matches = SequenceMatcher(None, before, text, autojunk=False).get_matching_blocks()
        delta_records = []
        for kind, a, b, x, y in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
            if kind == "equal":
                continue
            if offset is not None:
                start, end = offset + a, offset + b
                matched_anchor = before
            else:
                # Match the intended delta, never the model's inaccurate unchanged context.
                # Changed source characters and surrounding anchor must match exactly once.
                anchors = {}
                for block in matches:
                    if not block.size or not (block.a <= a <= b <= block.a + block.size):
                        continue
                    left, right = a - block.a, block.a + block.size - b
                    if (
                        left + right < 2
                        or (a > 0 and left == 0)
                        or (b < len(before) and right == 0)
                    ):
                        continue
                    if a == b and not (left and right):
                        continue
                    anchor = before[block.a : block.a + block.size]
                    if text.count(anchor) == 1:
                        anchors[(block.b + a - block.a, block.b + b - block.a)] = anchor
                if len(anchors) != 1:
                    raise NonRetryableFukamiruError("adoption.repair_unresolved_exact_delta_anchor")
                (start, end), matched_anchor = next(iter(anchors.items()))
            allowed = [
                (left, right, index)
                for left, right, index in spans
                if left <= start <= end <= right
            ]
            if len(allowed) != 1:
                raise NonRetryableFukamiruError(
                    "adoption.repair_changed_fixed_asset_or_ambiguous_scope"
                )
            left, _, index = allowed[0]
            grouped.setdefault((pid, index), []).append((start - left, end - left, after[x:y]))
            if offset is None:
                delta_records.append(
                    {
                        "paragraph_id": pid,
                        "before": text[start:end],
                        "after": after[x:y],
                        "reason": change["reason"],
                        "matched_source_anchor": matched_anchor,
                        "requested_before": before,
                        "requested_after": after,
                    }
                )
        actual.extend(delta_records or [deepcopy(change)])
    for (pid, index), edits in grouped.items():
        ordered = sorted(edits)
        if any(
            a[1] > b[0] or a[0] == a[1] == b[0] == b[1]
            for a, b in zip(ordered, ordered[1:], strict=False)
        ):
            raise NonRetryableFukamiruError("adoption.repair_overlapping_anchors")
        text = current[pid]["text_slots"][index]
        for start, end, after in reversed(ordered):
            text = text[:start] + after + text[end:]
        current[pid]["text_slots"][index] = text
    return result, actual
