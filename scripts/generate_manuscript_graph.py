"""Export the executable V32 manuscript workflow."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from export_workflow_graph import export

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/diagrams/manuscript-generation-data.js"


def main() -> None:
    data = export(version="32")
    if data["version"] != "32":
        raise ValueError("V32 diagram expects the V32 executable workflow")
    data["diagram_version"] = "32"
    data["source_ingestion_workflow"] = export(
        workflow_name="research_source_ingestion_workflow")
    data["source_ingestion_workflow"].pop("paragraph_argument_move_catalog")
    data["research_policy"] = {
        "plan_research_queries": {
            "summary": (
                "命題を短い日本語・英語のキーワード検索へ変換し、"
                "説明文や長すぎる検索語を除外します。NDLでは日本語、"
                "国際DBでは英語を優先し、原子命題自体は固定します。"
                "原メモの段落・注記・自己引用を渡し、書名と引用された概念を別々の検索語として保持します。"
            ),
            "input": ("原子命題、原メモの段落・注記、書名・概念の手掛かり、"
                      "固定した検証対象、出力言語"),
            "output": "1〜6件の短い検索語、検証対象、資料種別、計画状態",
        },
        "resolve_manuscript_evidence": {
            "summary": (
                "RAG有効時は生成済みの本文と索引を先に検索し、初回の外部検索は保留します。"
                "OCRや埋め込みは再生成しません。検索結果と成功した外部検索は実行単位でSQLiteに保存し、"
                "同じ検索を再実行しません。索引・資料・書誌情報が更新された場合はRAG検索を更新します。"
                "既知の書名確認は既存の速い経路を維持します。"
                "明示書名からローカル資料候補を探し、その資料内で概念と前後本文を読みます。"
                "巻号・叢書情報が付いた登録書名も照合し、巻や資料IDは混合しません。"
                "表記差による書名一致は候補の同定として記録し、書誌の同一性確認とは区別します。"
                "RAG無効時または追加調査では、OpenAlex・Crossref・NDL・Semantic Scholarの公式APIで"
                "未検索の検索語を上限付きで調査します。返却件数だけでは打ち切りません。"
                "429時は完了済み検索と未完了検索をSQLiteへ保存し、"
                "取得済み資料の検証を進め、追加調査でRetry-After後に未完了分を再開します。"
                "資料が一件も読めない場合は遅延再試行し、3回後も復旧しない場合は確認待ちにします。"
            ),
            "input": "検索計画、既存の検索チェックポイント、ローカルRAG・オンライン検索の設定",
            "output": (
                "検索候補、資料種別、抄録・本文抜粋とページ位置、DB応答、再開用チェックポイント"
            ),
        },
        "validate_evidence_support": {
            "summary": (
                "原メモの文脈と上限12件の候補を読み、命題への支持を検査します。"
                "RAG本文も語句一致だけでは排除せず、資料IDとページを保持します。"
                "PDFの文字順を保って書籍内の説明ページを探し、前後2～3ページを連続して読みます。"
                "再抽出した本文には原本ハッシュ・PDFページ・抽出ハッシュを付け、古い文字位置を流用しません。"
                "文字崩れのある日本語ページだけ既定3ページ・60秒以内でローカルOCRし、長い縦列を分割します。"
                "OCR修復文や文字崩れの疑いが残る範囲は画像確認待ちとし、根拠への自動昇格を拒否します。"
                "部分的な引用と不足事項を記録し、読解が必要と判定された資料だけ本文を追加取得します。"
                "引用が返らない未解決候補も読解対象とし、索引だけで資料を棄却しません。"
                "並列実行前に命題別の入力推計トークン枠を確保し、重要命題・明示的引用を優先します。"
                "初回検査と追加読解の枠を分け、既定で異なる3資料・追加2巡・命題180秒の受付期限を用います。"
                "不足事項に応じて同じ資料の概念と前後本文を6000字から12000字へ広げ、定義・条件・適用を分けます。"
                "解決、新情報なし、取得不可、入力・回数・時間予算を別々の理由として記録し、"
                "予算終了は未解決のまま後段へ渡します。既定の全命題合計は入力推計120000トークンで、"
                "再調査でも同じ配分と消費記録を引き継ぎます。出力・OCR課金はこの推計に含みません。"
                "300字条件は使いません。DOI・ISBNで同定できるRAG本文は再利用し、"
                "外部PDFも実行内で共有します。URLと書誌の一致も照合し、版の異なる資料は混同しません。"
                "全文を検索対象にし、時間・容量で止まった読解は未完了と記録します。"
                "暗号化されていても表示可能なPDFはRAG生成と同じ全ページ画像化・言語判定・"
                "NDLOCR-Lite／Mistral OCRで読解します。ローカル修復文の画像未確認状態は保持します。"
                "支持・推論の橋渡し・反証として引用照合できた資料だけ、別の永続Runへ引き継ぎます。"
                "そのRunは資料登録→抽出→OCR監査→構造化→Chunk生成→FTS→意味索引を実行し、"
                "原稿生成は完了を待ちません。PDFハッシュで重複登録を防ぎ、OCRは再実行しません。"
                "引用文が実際の抜粋に存在する場合だけ根拠に採用し、書誌情報だけでは支持としません。"
                "本文検査後に関連引用のない語句一致だけの候補はサジェストからも除外します。"
            ),
            "input": "固定した原子命題、抄録・本文付き候補と取得位置",
            "output": ("引用照合済み根拠、未確認の義務、部分的な引用と不足事項、"
                       "共有本文、命題別の消費記録・停止理由"),
        },
        "bounded_research_retry": {
            "summary": (
                "未解決の必須義務・対抗仮説を優先し、調査対象に選ばれたpreferred命題も含めて"
                "最大4件を一度だけ追加調査します。RAGで確認済みの義務は外部調査を省き、"
                "明示的な先行研究追加・対抗説・論文指定は残します。"
                "不足事項と読んだ資料の引用から検索語を更新し、初回RAGで使った未送信の検索語も利用します。"
                "短い検索語・同義語・書籍・一次資料を区別して公式DBを再調査し、なお不足するテーマだけ"
                "RustXNG v0.30.0（Standard）の一般Web検索で日本語研究資料と事実の出典を補います。"
                "語句一致やスニペットだけを根拠にせず、取得した内容を同じ根拠検査へ渡します。"
                "未読書籍は関連性条件を満たせば機械判別参考文献の候補として保持します。"
                "同じ引用照合を行い、執筆には取得済み資料を引き継ぎます。"
                "未完了のDB検索も引き継ぎます。取得済み資料がある場合は一度待機して再開し、"
                "なお失敗するDBは未完了と明記して他の資料の検証を進めます。"
            ),
            "input": "未解決の証拠義務、既存の検索計画・調査結果",
            "output": "再評価済みの根拠と未確認事項、bounded_research_retry_report",
        },
    }
    output = (
        "// Generated from the current executable manuscript workflow.\n"
        "// Do not edit by hand. Regenerate with scripts/generate_manuscript_graph.py.\n"
        f"window.MANUSCRIPT_GRAPH_V32 = {json.dumps(data, ensure_ascii=False, indent=2)};\n"
    )
    if "--check" in sys.argv:
        if TARGET.read_text(encoding="utf-8") != output:
            raise SystemExit("V32 DAG data is stale")
    else:
        TARGET.write_text(output, encoding="utf-8")
    print(
        f"{len(data['nodes'])} nodes, {len(data['edges'])} edges "
        f"(diagram V32, workflow V{data['version']})"
    )


if __name__ == "__main__":
    main()
