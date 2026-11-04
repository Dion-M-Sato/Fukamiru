"""Sync the existing RAG map's manuscript-source handoff with the executable graph."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from export_workflow_graph import export

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/diagrams/knowledge-ingestion.workflow.json"
REPORT = ROOT / "docs/diagrams/knowledge-ingestion.html"
NOTE = (
    " 原稿生成で表示可能な暗号化PDFを読んだ場合も同じ画像化・言語別OCRを使います。"
    "引用照合により利用価値を確認した資料はresearch_source_ingestion V1へ渡し、"
    "資料登録→抽出→OCR監査→構造化→Chunk生成→FTS→意味索引へ進めます。"
    "原稿生成はこのRunを待たず、完了済みOCRは再実行しません。PDFハッシュで登録を一意にします。"
)


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    handoff = export(workflow_name="research_source_ingestion_workflow")
    handoff.pop("paragraph_argument_move_catalog")
    source["researchSourceHandoff"] = handoff
    source["singleWorkflow"]["researchSourceHandoff"] = handoff
    for workflow in (source, source["singleWorkflow"]):
        note = workflow["meta"]["note"].split(" 原稿生成で表示可能な暗号化PDF", 1)[0]
        workflow["meta"]["note"] = note + NOTE
    document = REPORT.read_text(encoding="utf-8")
    pattern = r'(<script id="workflow-data" type="application/json">)(.*?)(</script>)'
    match = re.search(pattern, document, flags=re.S)
    if match is None:
        raise ValueError("RAG report data script is missing")
    data = json.loads(match[2])
    data["workflows"]["corpus"]["researchSourceHandoff"] = handoff
    data["workflows"]["corpus"]["meta"] = source["meta"]
    data["workflows"]["single"] = source["singleWorkflow"]
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("</", "<\\/")
    updated = document[:match.start(2)] + encoded + document[match.end(2):]
    serialized = json.dumps(source, ensure_ascii=False, indent=2) + "\n"
    if "--check" in sys.argv:
        if SOURCE.read_text(encoding="utf-8") != serialized or document != updated:
            raise SystemExit("RAG research handoff diagram is stale")
    else:
        SOURCE.write_text(serialized, encoding="utf-8")
        REPORT.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
