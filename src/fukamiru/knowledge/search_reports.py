"""Grounded search summaries with immutable evidence snapshots and canonical HTML."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from copy import deepcopy
from html import escape
from pathlib import Path
from typing import Any

from fukamiru.authoring.codex import CodexAuthoringAdapter
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.report_sources import normalized, quote_key
from fukamiru.storage.runs import utc_now

STAGE = "knowledge_search_synthesis"
VERIFY_STAGE = "knowledge_search_verification"
MAX_REPORT_REPAIRS = 1
MAX_EXCERPT_CHARACTERS = 8000
MAX_CONTEXT_CHARACTERS = 60000
FLOW = [
    "search",
    "select_sources",
    "read_context",
    "verify_bibliography",
    "synthesize",
    "verify_claims",
    "save",
    "open_in_right_pane",
]


def prepare_evidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    remaining = MAX_CONTEXT_CHARACTERS
    seen = set()
    for result in results:
        key = normalized(str(result.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        text = str(result.get("text") or "").strip()
        if not text or remaining <= 0:
            continue
        excerpt = text[: min(MAX_EXCERPT_CHARACTERS, remaining)]
        remaining -= len(excerpt)
        evidence.append(
            {
                **deepcopy(result),
                "evidence_id": f"E{len(evidence) + 1}",
                "text": excerpt,
                "excerpt_truncated": len(excerpt) < len(text)
                or bool(result.get("context_truncated")),
            }
        )
    return evidence


def _source_key(item: dict[str, Any]) -> str:
    return str(item.get("source_asset_id") or item.get("path") or item["evidence_id"])


def create_report(
    query: str,
    results: list[dict[str, Any]],
    generate: Callable[..., dict[str, Any]],
    *,
    bibliographies: dict[str, dict[str, Any]] | None = None,
    scope: str = "",
    query_kind: str = "topic",
    audit: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
    candidate_ambiguities: list[str] | None = None,
    image_pages: dict[str, Path] | None = None,
    retrieved_count: int | None = None,
) -> dict[str, Any]:
    """Compose, audit and keep only used evidence and its source-page bibliography."""
    query = query.strip()
    if not query:
        raise FukamiruError("検索語を入力してください。")
    bibliographies = bibliographies or {}
    evidence = prepare_evidence([r for r in results if r.get("source_asset_id") in bibliographies])
    model = None
    used_bibliography = []
    verification = None
    if evidence:
        payload = {
            "query": query,
            "query_kind": query_kind,
            "scope": scope,
            "output_language": "ja",
            "evidence": evidence,
            "bibliographies": list(bibliographies.values()),
            "known_limitations": limitations or [],
            "candidate_ambiguities": candidate_ambiguities or [],
        }
        for attempt in range(MAX_REPORT_REPAIRS + 1):
            generated = generate(STAGE, payload)
            CodexAuthoringAdapter.validate_result(STAGE, generated)
            try:
                paragraphs = _validate_supports(generated, evidence)
            except FukamiruError as exc:
                if attempt == MAX_REPORT_REPAIRS:
                    raise
                payload = {
                    **payload,
                    "previous_draft": generated,
                    "verification_feedback": [{"target": "draft", "reason": str(exc)}],
                }
                continue
            used_ids = list(dict.fromkeys(ref for p in paragraphs for ref in p["evidence_ids"]))
            if not used_ids:
                if not generated["limitations"]:
                    raise FukamiruError("報告または回答できない理由が必要です。")
                evidence = []
                used_bibliography = []
                verification = None
                break
            if not any(p["kind"] in {"content", "synthesis"} for p in paragraphs):
                raise FukamiruError("表題紙の説明だけでは内容をまとめた報告になりません。")
            by_id = {item["evidence_id"]: item for item in evidence}
            used = [by_id[ref] for ref in used_ids]
            assets = list(dict.fromkeys(item["source_asset_id"] for item in used))
            used_bibliography = [
                {**deepcopy(bibliographies[asset]), "reference_id": f"R{i}"}
                for i, asset in enumerate(assets, 1)
            ]
            relevant_pages = {
                proof["page_id"]
                for b in used_bibliography
                for proof in b["proofs"].values()
                if proof["method"] == "image"
            }
            attached = {
                key: path for key, path in (image_pages or {}).items() if key in relevant_pages
            }
            options = {"image_paths": list(attached.values())} if attached else {}
            verification = generate(
                VERIFY_STAGE,
                {
                    "query": query,
                    "scope": scope,
                    "paragraphs": paragraphs,
                    "evidence": used,
                    "bibliographies": used_bibliography,
                    "limitations": generated["limitations"],
                    "attached_image_page_ids_in_order": list(attached),
                },
                **options,
            )
            CodexAuthoringAdapter.validate_result(VERIFY_STAGE, verification)
            if set(verification["checked_paragraph_ids"]) != {
                p["paragraph_id"] for p in paragraphs
            } or set(verification["checked_bibliography_ids"]) != {
                b["reference_id"] for b in used_bibliography
            }:
                raise FukamiruError("根拠・書誌の再検査対象が不足しています。")
            if (
                verification["supported"]
                and verification["coherent"]
                and not verification["issues"]
            ):
                evidence = used
                for item in evidence:
                    item["used_quotes"] = list(
                        dict.fromkeys(
                            support["quote"]
                            for p in paragraphs
                            for support in p["supports"]
                            if support["evidence_id"] == item["evidence_id"]
                        )
                    )
                for bibliography in used_bibliography:
                    bibliography["verification_status"] = "source_verified"
                break
            if attempt == MAX_REPORT_REPAIRS:
                raise FukamiruError("報告の根拠・書誌・文章の統合を再検査で確認できませんでした。")
            payload = {
                **payload,
                "previous_draft": generated,
                "verification_feedback": verification["issues"],
            }
        model = str(CodexAuthoringAdapter.execution_contract(STAGE)["model"])
    else:
        generated = {
            "summary": [],
            "findings": [],
            "limitations": [
                "関連する本文と書誌を確認できる資料が不足し、内容の報告を作成できませんでした。"
            ],
        }
    generated["limitations"] = list(
        dict.fromkeys([*(limitations or []), *generated["limitations"]])
    )
    report = {
        "id": str(uuid.uuid4()),
        "query": query,
        "title": f"調査レポート：{query}",
        "created_at": utc_now(),
        "result_count": len(evidence),
        "source_count": len(used_bibliography),
        "schema_version": 2,
        "retrieved_count": len(results) if retrieved_count is None else retrieved_count,
        "model": model,
        "scope": scope,
        "query_kind": query_kind,
        **generated,
        "evidence": evidence,
        "bibliography": used_bibliography,
        "selection_audit": audit or [],
        "candidate_ambiguities": candidate_ambiguities or [],
        "verification": verification,
    }
    report["canonical_html"] = render_report(report)
    CanonicalHTMLValidator().assert_valid(report["canonical_html"])
    return report


def _validate_supports(generated: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict]:
    allowed = {item["evidence_id"]: item for item in evidence}
    paragraphs = []
    for kind in ("summary", "findings"):
        for index, paragraph in enumerate(generated[kind], 1):
            target = f"{kind}-{index}"
            ids = paragraph["evidence_ids"]
            if not paragraph["text"].strip() or not ids or not set(ids) <= set(allowed):
                raise FukamiruError(f"{target}: 報告の根拠参照が不正です。")
            supported_ids = {p["evidence_id"] for p in paragraph["supports"]}
            if supported_ids != set(ids):
                raise FukamiruError(
                    f"{target}: 各根拠参照に対応する原文の引用が必要です。"
                    f"不足: {', '.join(sorted(set(ids) - supported_ids)) or 'なし'}; "
                    f"余分: {', '.join(sorted(supported_ids - set(ids))) or 'なし'}"
                )
            for support in paragraph["supports"]:
                quote = quote_key(support["quote"])
                if len(quote) < 12 or quote not in quote_key(
                    allowed[support["evidence_id"]]["text"]
                ):
                    raise FukamiruError(
                        f"{target}: 根拠 {support['evidence_id']} の引用が原文にありません。"
                        "空白を除き12文字以上の原文をそのまま引用してください: "
                        f"{support['quote'][:180]}"
                    )
            if paragraph["kind"] != "identity" and not any(
                allowed[ref].get("kind") == "body" for ref in ids
            ):
                raise FukamiruError(f"{target}: 内容の説明には本文の根拠が必要です。")
            paragraphs.append({**paragraph, "paragraph_id": f"{kind}-{index}"})
    return paragraphs


def _position(item: dict[str, Any]) -> str:
    if pages := item.get("pages"):
        numbers = [p["number"] for p in pages if p.get("number") is not None]
        labels = [str(p["printed_label"]) for p in pages if p.get("printed_label")]
        location = ""
        if numbers:
            location = (
                f"PDF p. {numbers[0]}"
                if numbers[0] == numbers[-1]
                else f"PDF pp. {numbers[0]}–{numbers[-1]}"
            )
        if labels:
            printed = labels[0] if labels[0] == labels[-1] else f"{labels[0]}–{labels[-1]}"
            location += f"（紙面・章の表示：{printed}）"
        return location or "位置情報なし"
    citation = item.get("citation") or {}
    start = citation.get("start_label") or citation.get("start_page")
    end = citation.get("end_label") or citation.get("end_page")
    if start is None:
        return "位置情報なし"
    return f"p. {start}" + (f"–{end}" if end is not None and end != start else "")


def render_report(report: dict[str, Any]) -> str:
    parts = [
        '<article id="search-report" lang="ja">',
        '<section id="report-header"><h2 id="report-title">',
        escape(report["title"]),
        '</h2><p id="report-meta">',
        escape(report["created_at"]),
        f" · 使用{report['source_count']}文献 · 根拠{report['result_count']}箇所</p>",
        '<p id="report-reading-scope">選んだ文献の一部の本文に基づく報告です。'
        "各参照から根拠の引用と読取範囲を確認できます。</p></section>",
    ]
    if not report["model"]:
        parts[-1] = (
            '<p id="report-reading-scope">根拠が不足しているため'
            "AIによる内容の報告は作成していません。</p></section>"
        )
    for key, label in (("summary", "報告"), ("findings", "論点の整理")):
        parts.append(f'<section id="{key}"><h2 id="{key}-heading">{label}</h2>')
        for index, paragraph in enumerate(report[key], 1):
            prefix = f"{key}-{index}"
            if key == "findings":
                parts.append(
                    f'<section id="{prefix}"><h3 id="{prefix}-heading">'
                    f"{escape(paragraph['heading'])}</h3>"
                )
            refs = " ".join(
                f'<a href="#evidence-{escape(ref)}">[{escape(ref)}]</a>'
                for ref in dict.fromkeys(paragraph["evidence_ids"])
            )
            parts.append(f'<p id="{prefix}-text">{escape(paragraph["text"])} {refs}</p>')
            if key == "findings":
                parts.append("</section>")
        if not report[key]:
            parts.append(f'<p id="{key}-empty">根拠のある{label}を作成できませんでした。</p>')
        parts.append("</section>")
    parts.append('<section id="limitations"><h2 id="limitations-heading">確認が必要な点</h2>')
    for index, text in enumerate(report["limitations"], 1):
        parts.append(f'<p id="limitation-{index}">{escape(text)}</p>')
    parts.append('</section><section id="bibliography"><h2 id="bibliography-heading">使用文献</h2>')
    bibliography_by_asset = {}
    for item in report.get("bibliography", []):
        ref = item["reference_id"]
        bibliography_by_asset[item["source_asset_id"]] = ref
        publication = " : ".join(
            filter(None, [item.get("publication_place"), item.get("publisher")])
        )
        fields = [publication, item.get("publication_title"), item.get("publication_date")]
        if item.get("volume"):
            fields.append("vol. " + item["volume"])
        if item.get("issue"):
            fields.append("no. " + item["issue"])
        if item.get("pages"):
            fields.append("pp. " + item["pages"])
        details = " · ".join(str(value) for value in fields if value)
        parts.append(
            f'<section id="bibliography-{ref}"><h3 id="bibliography-{ref}-heading">'
            f"[{ref}] {escape(' / '.join(item['authors']))}</h3>"
            f'<p id="bibliography-{ref}-citation"><cite>{escape(item["title"])}</cite>. '
            f"{escape(details)}.</p>"
        )
        identifiers = " · ".join(
            f"{label}: {item[key]}"
            for label, key in (("DOI", "doi"), ("ISBN", "isbn"))
            if item.get(key)
        )
        if identifiers:
            parts.append(f'<p id="bibliography-{ref}-identifiers">{escape(identifiers)}</p>')
        numbers = list(
            dict.fromkeys(
                str(proof["page_number"])
                for proof in item["proofs"].values()
                if proof.get("page_number") is not None
            )
        )
        parts.append(
            f'<p id="bibliography-{ref}-verification">原資料の書誌ページを照合し、'
            f"独立した再検査を通過。確認位置：PDF {escape(', '.join(numbers)) or '章・書誌情報'}。"
            "</p></section>"
        )
    if not report.get("bibliography"):
        parts.append('<p id="bibliography-empty">報告の根拠に採用した文献はありません。</p>')
    parts.append('</section><section id="evidence"><h2 id="evidence-heading">報告の根拠</h2>')
    parts.append(
        f'<p id="evidence-limit">検索候補 {report["retrieved_count"]}箇所から本文を読み直し、'
        "報告で実際に参照した箇所だけを掲載しています。</p>"
    )
    for item in report["evidence"]:
        prefix = f"evidence-{item['evidence_id']}"
        parts.extend(
            [
                f'<section id="{prefix}"><h3 id="{prefix}-title">[{item["evidence_id"]}] '
                f"{escape(str(item.get('title') or '無題の資料'))}</h3>",
                f'<p id="{prefix}-location">読取範囲：{escape(_position(item))}</p>',
            ]
        )
        if reference := bibliography_by_asset.get(item.get("source_asset_id")):
            parts.append(
                f'<p id="{prefix}-reference">使用文献 '
                f'<a href="#bibliography-{reference}">[{reference}]</a></p>'
            )
        for index, quote in enumerate(item.get("used_quotes") or [item["text"]], 1):
            parts.append(
                f'<blockquote id="{prefix}-quote-{index}"><p id="{prefix}-text-{index}">'
                f"{escape(quote)}</p></blockquote>"
            )
        parts.append(
            f'<p id="{prefix}-path">{escape(str(item.get("path") or "資料パス未記録"))}</p>'
        )
        if item["excerpt_truncated"]:
            parts.append(
                f'<p id="{prefix}-truncated">読取上限により本文の一部のみを確認しています。</p>'
            )
        parts.append("</section>")
    parts.append("</section></article>")
    return "".join(parts)


class SearchReportRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(self, report: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO knowledge_search_reports
               (id, query, title, created_at, result_count, source_count, model,
                report_json, canonical_html) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                *(
                    report[key]
                    for key in (
                        "id",
                        "query",
                        "title",
                        "created_at",
                        "result_count",
                        "source_count",
                        "model",
                    )
                ),
                json.dumps(report, ensure_ascii=False),
                report["canonical_html"],
            ),
        )

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT id, query, title, created_at, result_count, source_count, model
               FROM knowledge_search_reports ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,),
            )
        ]

    def get(self, report_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT report_json FROM knowledge_search_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        return json.loads(row["report_json"]) if row else None


def export_document(report: dict[str, Any]) -> str:
    return (
        '<!doctype html><html lang="ja"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(report['title'])}</title><style>"
        "body{font:16px/1.8 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 24px;"
        "color:#1b211a;background:white;overflow-wrap:anywhere}h2{font-size:1.4rem}h3{font-size:1.1rem}"
        "section{margin:24px 0}blockquote{margin:16px 0;padding:0 16px;"
        "border-left:3px solid #687866;"
        "white-space:pre-wrap}a{color:#245524}</style>" + report["canonical_html"] + "</html>"
    )
