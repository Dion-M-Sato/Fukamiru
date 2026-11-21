"""Discover, read and verify sources before composing a Knowledge Search report."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fukamiru.authoring.codex import CodexAuthoringAdapter
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.report_bibliography import BibliographyUnresolved, extract_bibliography
from fukamiru.knowledge.report_sources import MAX_SOURCES, ReportCorpus
from fukamiru.knowledge.search_reports import create_report

PLAN_STAGE = "knowledge_search_plan"


def research_report(
    query: str,
    results: list[dict[str, Any]],
    corpus: ReportCorpus,
    generate: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    candidates = corpus.candidates(query.strip(), results)
    audit = []
    # Determine intent before a spelling gate: capitalized topic words can have
    # legitimate cross-language semantic matches with no literal spelling match.
    eligible = candidates
    if not eligible:
        return create_report(query, [], generate, audit=audit, retrieved_count=len(results))
    plan = generate(
        PLAN_STAGE,
        {
            "query": query.strip(),
            "max_sources": MAX_SOURCES,
            "candidates": [
                {
                    "candidate_id": row["candidate_id"],
                    "title": row["title"],
                    "bibliography": {
                        key: row["bibliography"].get(key)
                        for key in ("id", "title", "authors", "publisher", "publication_date")
                    },
                    "hits": [{**hit, "text": hit["text"][:800]} for hit in row["hits"]],
                    "metadata_match": row["metadata_match"],
                    "literal_match": row["literal_match"],
                }
                for row in eligible
            ],
        },
    )
    CodexAuthoringAdapter.validate_result(PLAN_STAGE, plan)
    by_id = {row["candidate_id"]: row for row in eligible}
    selected = [row["candidate_id"] for row in plan["selections"]]
    excluded = [row["candidate_id"] for row in plan["exclusions"]]
    if (
        len(selected) > MAX_SOURCES
        or len(selected) != len(set(selected))
        or not set(selected + excluded) <= set(by_id)
        or set(selected) & set(excluded)
        or any(
            not row["reason"].strip()
            or not row["entity"].strip()
            or not row["reading_queries"]
            or len(row["reading_queries"]) > 6
            or any(not q.strip() or len(q) > 500 for q in row["reading_queries"])
            for row in plan["selections"]
        )
    ):
        raise FukamiruError("読取計画の資料参照が不正です。")
    for row in plan["exclusions"]:
        audit.append({**row, "title": by_id[row["candidate_id"]]["title"], "outcome": "excluded"})
    for candidate_id in sorted(set(by_id) - set(selected + excluded)):
        audit.append(
            {
                "candidate_id": candidate_id,
                "title": by_id[candidate_id]["title"],
                "outcome": "excluded",
                "reason": "報告の読取対象に選ばれませんでした。",
            }
        )
    evidence = []
    bibliographies = {}
    images = {}
    limitations = []
    for selection in plan["selections"]:
        candidate = by_id[selection["candidate_id"]]
        if plan["query_kind"] == "name" and not candidate["literal_match"]:
            audit.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "title": candidate["title"],
                    "outcome": "excluded",
                    "reason": "人物の同定根拠がありません。",
                }
            )
            continue
        source = corpus.read({**candidate, "entity": selection["entity"]})
        body = corpus.body(source, selection["reading_queries"])
        if not body:
            audit.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "title": candidate["title"],
                    "outcome": "excluded",
                    "reason": "内容の根拠となる本文がありません。",
                }
            )
            continue
        try:
            bibliography, source_images = extract_bibliography(source, corpus, generate)
        except BibliographyUnresolved as exc:
            audit.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "title": candidate["title"],
                    "outcome": "unresolved",
                    "reason": str(exc),
                }
            )
            limitations.append(
                f"書誌を確定できなかった資料は報告の根拠に採用していません：{source['title']}"
            )
            continue
        asset = source["source_asset_id"]
        bibliographies[asset] = bibliography
        images.update(source_images)
        evidence.extend(body)
        # Metadata establishes identity, never the work's substantive argument.
        transcriptions = list(
            {
                (proof["page_id"], proof["quote"]): {
                    "page_id": proof["page_id"],
                    "page_number": proof["page_number"],
                    "quote": proof["quote"],
                    "method": "image",
                    "image_sha256": proof["image_sha256"],
                }
                for proof in bibliography["proofs"].values()
                if proof["method"] == "image"
            }.values()
        )
        identity_text = "\n\n".join(p["text"] for p in bibliography["verification_pages"])
        if transcriptions:
            identity_text = (
                "原資料の書誌ページ画像からの転記（独立再検査待ち）:\n"
                + "\n\n".join(item["quote"] for item in transcriptions)
                + "\n\n書誌ページのOCR原文:\n"
                + identity_text
            )
        evidence.append(
            {
                "kind": "identity",
                "source_asset_id": asset,
                "title": source["title"],
                "path": source["path"],
                "entity": selection["entity"],
                "content_sha256": source["sha256"],
                "pages": [{**p, "printed_label": None} for p in bibliography["verification_pages"]],
                "text": identity_text,
                "bibliographic_transcriptions": transcriptions,
                "reading_scope": "bibliographic_pages",
            }
        )
        audit.append(
            {
                "candidate_id": candidate["candidate_id"],
                "title": source["title"],
                "outcome": "read",
                "entity": selection["entity"],
                "reason": selection["reason"],
                "pages_read": [p["number"] for item in body for p in item["pages"]],
            }
        )
    return create_report(
        query,
        evidence,
        generate,
        bibliographies=bibliographies,
        scope=plan["scope"],
        query_kind=plan["query_kind"],
        audit=audit,
        limitations=limitations,
        candidate_ambiguities=plan["ambiguities"],
        image_pages=images,
        retrieved_count=len(results),
    )
