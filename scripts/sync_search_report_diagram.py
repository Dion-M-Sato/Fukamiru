"""Keep the UI-linked RAG diagram's Knowledge Search operation in sync."""

from __future__ import annotations

import json
import re
import sys
from html import escape
from pathlib import Path

from fukamiru.authoring.codex import CodexAuthoringAdapter
from fukamiru.knowledge.report_bibliography import BIBLIOGRAPHY_STAGE, MAX_BIBLIOGRAPHY_REPAIRS
from fukamiru.knowledge.report_research import PLAN_STAGE
from fukamiru.knowledge.search_reports import (
    FLOW,
    MAX_CONTEXT_CHARACTERS,
    MAX_EXCERPT_CHARACTERS,
    MAX_REPORT_REPAIRS,
    STAGE,
    VERIFY_STAGE,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source_path = ROOT / "docs/diagrams/knowledge-ingestion.workflow.json"
    html_path = ROOT / "docs/diagrams/knowledge-ingestion.html"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    contract = CodexAuthoringAdapter.execution_contract(STAGE)
    labels = ["本文・書誌から候補を検索", "関連資料・人物を選別", "前後の本文を読む",
              "原資料で書誌を照合", "論点をつないで報告", "主張・引用・使用書誌を再検査",
              "報告を保存", "クリックで右ペイン表示"]
    source["searchReport"] = {
        "operation": "POST /api/search-reports", "starts_run": False,
        "stage": STAGE, "model": contract["model"],
        "reasoning_effort": contract["reasoning_effort"],
        "stages": {stage: {key: CodexAuthoringAdapter.execution_contract(stage)[key]
                           for key in ("model", "reasoning_effort")}
                   for stage in (PLAN_STAGE, BIBLIOGRAPHY_STAGE, STAGE, VERIFY_STAGE)},
        "bibliography": "used_sources_only; source_page_proofs; independent_audit",
        "relevance": "name_identity; contextual_body_reading; no_quota_filling",
        "repairs": {"bibliography_per_source": MAX_BIBLIOGRAPHY_REPAIRS,
                    "synthesis": MAX_REPORT_REPAIRS, "provider_schema_failures": 0},
        "storage": "state.db:knowledge_search_reports",
        "search_storage": "knowledge.db", "canonical_format": "validated_html",
        "max_excerpt_characters": MAX_EXCERPT_CHARACTERS,
        "max_context_characters": MAX_CONTEXT_CHARACTERS,
        "steps": [{"id": key, "label": label} for key, label in zip(FLOW, labels, strict=True)],
    }
    section = (
        '<section id="knowledge-search-report-flow" class="terminal" '
        'aria-label="Knowledge Search レポート">'
        '<h2>Knowledge Search · 検索レポート</h2><p>'
        + ' → '.join(escape(label) for label in labels)
        + '</p><p>本文と書誌から候補を探し、人名の混同・重複・索引・献辞を選別します。'
        '前後の本文を読み、論点をつないだ文章として報告します。'
        f'{escape(contract["model"])} / {escape(contract["reasoning_effort"])}。'
        '使用した文献だけを原資料の書誌ページ・画像で照合し、本文の主張と書誌を独立に再検査。'
        '原文にない引用や表題紙だけの内容説明は拒否します。'
        'OCRで見出しが落ちた目次や書誌カバーも本文から除外。'
        '著者名の展開は完全な名前の原文引用を要求し、書誌・報告は各1回だけ訂正できます。'
        '独立再検査では留保も確認し、読取前の計画や内部状態は完成報告に表示しません。'
        '確認済みHTML・使用文献・本文の根拠・採否記録をstate.dbに保存。'
        'knowledge.dbの原資料は保持します。根拠不足なら理由を示し、検査失敗時は保存しません。'
        '一覧から右ペインで再読し、HTMLを保存できます。取り込みRunは開始しません。</p></section>'
    )
    document = html_path.read_text(encoding="utf-8")
    marker = r'<section id="knowledge-search-report-flow".*?</section>'
    if re.search(marker, document, flags=re.S):
        document = re.sub(marker, lambda _: section, document, flags=re.S)
    else:
        if "<footer>" not in document:
            raise ValueError("Workflow report footer is missing")
        document = document.replace("<footer>", section + "\n<footer>", 1)
    pattern = r'(<script id="workflow-data" type="application/json">)(.*?)(</script>)'
    match = re.search(pattern, document, flags=re.S)
    if not match:
        raise ValueError("Workflow data is missing")
    data = json.loads(match[2])
    data["workflows"]["corpus"]["searchReport"] = source["searchReport"]
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    document = document[:match.start(2)] + encoded + document[match.end(2):]
    serialized = json.dumps(source, ensure_ascii=False, indent=2) + "\n"
    if "--check" in sys.argv:
        if source_path.read_text(encoding="utf-8") != serialized:
            raise SystemExit("Knowledge Search diagram source is stale")
        if html_path.read_text(encoding="utf-8") != document:
            raise SystemExit("Knowledge Search diagram HTML is stale")
    else:
        source_path.write_text(serialized, encoding="utf-8")
        html_path.write_text(document, encoding="utf-8")


if __name__ == "__main__":
    main()
