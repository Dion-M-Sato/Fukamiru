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
ADDITIONS_NOTE = (
    " 追加取り込みはSHA-256が一致する取り込み済み資料をalready_ingestedとして除外し、"
    "追加・内容変更分だけを対象件数に含めます。既存Itemと失敗履歴は保持します。"
    "除外資料の未完了OCRジョブは履歴を残して取り消し、不要な言語待ちを解除します。"
    "PDF解析版の更新や派生HTMLの欠落だけでは保存済み本文・OCR・Chunkを再生成しません。"
)


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    handoff = export(workflow_name="research_source_ingestion_workflow")
    handoff.pop("paragraph_argument_move_catalog")
    source["researchSourceHandoff"] = handoff
    source["singleWorkflow"]["researchSourceHandoff"] = handoff
    source["corpusSourceScope"]["existing_sources"] = "skip_ready_canonical_sha256"
    source["corpusSourceScope"]["parser_upgrade"] = "preserve_canonical_content"
    source["indexProgressDisplay"].update(
        run_details="numeric_phase_progress",
        unknown_total="show_actual_count_without_percentage",
        numeric_units="preserve_reported_unit",
    )
    for node in source["nodes"]:
        if node["id"] == "register_sync_batch":
            node["description"] = "SHA-256で取込済み資料を除外し、追加分の対象件数を確定"
            node["boundary"] = (
                "全Itemを登録した後、同じSHA-256の取込済み資料をalready_ingestedとして記録し、"
                "対象件数から除外します。元Item・状態・エラー履歴は保持します。"
            )
        if node["id"] == "locate_and_hash_source":
            node["boundary"] = (
                "同じSHA-256を排他し、保存済み本文を再利用します。"
                "PDF解析版の更新や派生HTML欠落による自動再処理は行いません。"
            )
    for workflow in (source, source["singleWorkflow"]):
        note = workflow["meta"]["note"].split(" 原稿生成で表示可能な暗号化PDF", 1)[0]
        note = note.split(" 追加取り込みはSHA-256", 1)[0]
        workflow["meta"]["note"] = note + ADDITIONS_NOTE + NOTE
    document = REPORT.read_text(encoding="utf-8")
    pattern = r'(<script id="workflow-data" type="application/json">)(.*?)(</script>)'
    match = re.search(pattern, document, flags=re.S)
    if match is None:
        raise ValueError("RAG report data script is missing")
    data = json.loads(match[2])
    data["workflows"]["corpus"]["researchSourceHandoff"] = handoff
    data["workflows"]["corpus"]["meta"] = source["meta"]
    data["workflows"]["corpus"]["nodes"] = source["nodes"]
    data["workflows"]["corpus"]["corpusSourceScope"] = source["corpusSourceScope"]
    data["workflows"]["corpus"]["indexProgressDisplay"] = source["indexProgressDisplay"]
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
