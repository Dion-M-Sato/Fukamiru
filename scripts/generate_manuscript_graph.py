"""Export the executable V32 manuscript workflow."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from export_workflow_graph import export

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/diagrams/manuscript-generation-data.js"


def main() -> None:
    contextual = "--llmjp33b-high" in sys.argv
    adoption = "--llmjp33b-adopt" in sys.argv
    profile = "llmjp33b_adopt" if adoption else "llmjp33b_high" if contextual else "local_v32"
    data = export(version="32", proofreading_profile=profile)
    if data["version"] != "32":
        raise ValueError("V32 diagram expects the V32 executable workflow")
    data["diagram_version"] = "32"
    data["source_ingestion_workflow"] = export(
        workflow_name="research_source_ingestion_workflow")
    data["source_ingestion_workflow"].pop("paragraph_argument_move_catalog")
    data["review_policy"] = {
        "generate_proofreading_candidates": {
            "purpose": "文IDに対応する局所修正を生成",
            "summary": (
                "元の文ID・段落ID・原文ハッシュをモデルの応答と照合し、全文を生成させず局所置換だけを受け取ります。"
                "置換IDと位置はプログラムが確定し、常体・敬体、時制・否定・モダリティ、引用・書名を検査します。"
                "適合する置換を組み合わせ、不適合な置換だけを原文と対象段落付きで人間確認へ保存します。"
                "三つの独立標本と変更なしの結果もSQLiteへ記録し、途中再開で二重生成しません。"
            ),
            "input": "文ID・元文ID・段落ID・原文ハッシュ、原文、文体条件",
            "output": "文IDを保持した局所置換と候補ID、採用・保留の置換ID、原文対応の履歴",
        },
        "preprocess_proofreading_candidates": {
            "purpose": "局所修正の原文対応と文体を検査",
            "summary": (
                "局所置換からの再構成を照合し、検査後の無記録な変更や文体変更を候補選択へ流しません。"
            ),
            "input": "原文ID・ハッシュ、候補、局所置換の保存記録",
            "output": "対応と文体を検査した候補、または人間確認",
        },
        "rank_proofreading_candidates": {
            "purpose": "原文と局所修正候補を比較して選択",
            "summary": (
                "Solは原文と検査済み候補を比較し、文IDごとに候補IDだけを選びます。"
                "本文の書き直しは行いません。"
            ),
            "input": "文ID、原文、検査済み候補IDと本文",
            "output": "文IDと選択候補ID、または原文維持",
        },
        "publish_proofreading_shadow": {
            "purpose": "IDを保って検証済み局所修正を反映",
            "summary": (
                "適用直前に原文・局所置換履歴・ID対応を再検査します。保護されたインライン構造を平文化せず、"
                "段落の属性・IDを保ちます。表層処理が記録外の変更を加えた段落は戻し、元Runの履歴は変更しません。"
            ),
            "input": "選択候補ID、元のHTML、文・段落ID、原文ハッシュ、局所置換履歴",
            "output": "IDを維持した検証済みHTMLと適用・保留履歴",
        },
        "classify_audit_findings": {
            "summary": (
                "監査の主張ID・文IDは実際の節契約と初稿の所属から節へ対応づけます。"
                "複数節に属する指摘は各節へ展開し、元の主張IDと修正対象の文IDを保持します。"
                "所属不明の指摘を捨てず停止し、節の検査範囲と修復・人間確認の区別を維持します。"
            ),
            "input": "監査指摘、節契約、初稿の主張・文ID",
            "output": "正しい対象節と原指摘を保つ監査分類",
        },
        "render_evidence_footnotes": {
            "summary": (
                "脚注の書誌不足は検査を維持して停止します。一次の出版社・図書館情報を確認した"
                "運用者の書誌補正は、元の文献の同一性と厳格な書式検査を満たす場合だけ適用します。"
                "元入力・確認元・補正内容を履歴に残し、停止した脚注工程を一度だけ再開します。"
                "書名だけへの脚注は、単独・複数を問わず各書名の閉じ括弧『』の直前へ置きます。"
                "本文改稿と再描画でもその位置と脚注ID・番号を保ちます。"
                "参考文献用の書名抽出では脚注番号を除外し、番号付きの偽の書名を追加しません。"
                "保存済みの脚注生成前入力から、別Runで後半だけを実行できます。"
                "元Run・旧HTML・回答を保持し、新Runでは脚注以降の検証と提出保存をやり直します。"
                "既定の2試行が同一入力・同一エラーなら、失敗履歴を残して最新試行だけを再開します。"
                "人間の過剰要求判断とは別の経路で、本文・論証・主張の支持判定を変更しません。"
            ),
            "input": "保存済み入力、一次情報で確認した書誌、確認元URL",
            "output": "厳格に検査した脚注、元入力を含む補正履歴",
        },
        "redraft_body_prose": {
            "summary": (
                "一回の本文改稿と読者評価の後、確認不足・出典照合・理論適用の未検証を"
                "本文から除き、段落IDと原文を付けて人間確認へ保存します。"
                "元段落への差し戻し後にも検査するため、確認文を本文へ戻しません。"
                "対象範囲・条件・否定・反証は命題として保ち、書名・引用・脚注を変更しません。"
                "確認事項の抽出に失敗した場合は停止し、未処理の本文を完了扱いにしません。"
                "その後、引用の有無を問わず節をまたぐ全段落境界を点検します。"
                "中心語の引き継ぎと、付加・対比・理由などに合う接続表現を別々に判定します。"
                "文中の『も』『だけでなく』だけでは段落間の付加を明示したと扱いません。"
                "問題が残る次段落の冒頭文だけを一度修正し、全境界と意味保存を独立に再検査します。"
                "保護部分と第二文以降は固定し、意味が変わる修正は採用せず、人間確認へ回します。"
            ),
            "input": "改稿・差し戻し後のHTML、保護対象、段落ごとの内容",
            "output": "確認文を除いたHTML、全境界の中心語・接続表現の判定、修正前後、人間確認事項",
        },
        "human_approval": {
            "summary": (
                "中央で対象選択・全体コメント・承認を行い、右ペインでHTML本文と一問一答を確認します。"
                "質問ID・回答・「LLMの過剰要求である」の判断をSQLiteに保存し、"
                "回答を反映する再生成は段落形成の新しい世代から開始します。"
                "過剰と判断された同じ要求は再提示を抑制します。人間の判断を資料の根拠や"
                "出典確認済みの状態へ昇格させません。未送信の下書きは生成へ渡しません。"
                "既存の推敲・再監査にも同じ人間の判断を渡します。"
                "一貫性監査の人間判断・追加調査の指摘も質問にし、HTMLの段落と対応付けます。"
            ),
            "input": "HTML原稿、質問IDごとの回答・過剰要求の判断、全体コメント",
            "output": "承認記録、または人間の回答を持つ新しい段落形成・修正・再レビュー",
        },
    }
    for node_id in (
        "build_claim_tree", "build_argument_graph", "select_defense_obligations",
        "lock_section_contracts", "diagnose_japanese_prose", "redraft_japanese_paragraphs",
        "review_japanese_sections", "redraft_body_prose", "audit_final_findings",
    ):
        original = data["review_policy"].get(node_id, {})
        data["review_policy"][node_id] = {
            **original,
            "summary": original.get("summary", "") + (
                "概念の意味・対象への適用根拠・論証での役割を独立に検査します。"
                "必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。"
                "上流の生成は最大2回修復し、その都度独立に再検査します。"
                "不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。"
                "必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。"
                "原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。"
                "本文から除いた確認不足の条件は、人間確認へ保存します。"
                "内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。"
                "根拠カード内の不適切な概念・誤った適用も検査対象です。"
                "research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や"
                "新しい分析対象を作らないことを確認します。"
                "引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。"
                "引用符への出現だけで概念を適切・保持必須にはしません。"
                "内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。"
                "再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。"
                  "提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。"
                  "原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。"
                  "全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。"
                "根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。"
                "修復前後の報告書を上書きせず、失敗履歴も保持します。"
                "保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。"
                "保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。"
            ),
            "input": original.get("input", "") + " 原文の命題・出典、候補概念、節の文脈",
            "output": original.get("output", "") + " 概念ごとの判定・保持語・修復前後・再検査記録",
        }
    data["review_policy"]["redraft_japanese_paragraphs"]["summary"] += (
        "改稿候補が加えた根拠のない前提は採用せず、上流の段落生成へ指摘を戻します。"
        "既存の節ごと最大3回の改稿枠を使い、元の主張・節契約を改変せず再生成します。"
        "ID照合や提供先の形式エラーを意味修正へ読み替えません。"
    )
    data["review_policy"]["audit_final_findings"]["summary"] += (
        "自動脚注番号を意味検査用の本文から除き、書名中の本来の数字は保持します。"
        "HTMLと注の位置は変えず、固定済みHTMLの最終監査だけを別Runで再実行できます。"
        "元Runの本文・監査・確認事項は履歴として保持します。"
    )
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
                "論文・書籍・一次資料・書誌を区別して保存し、本文取得の有無と検証状況は別に保持します。"
            ),
            "input": "未解決の証拠義務、既存の検索計画・調査結果",
            "output": "再評価済みの根拠と未確認事項、bounded_research_retry_report",
        },
    }
    target = TARGET
    if contextual:
        target = TARGET.with_name("manuscript-proofreading-high-data.js")
        data["proofreading_profile"] = "llmjp33b_high"
        descriptions = {
            "diagnose_japanese_prose": (
                "原文と校正文脈を固定",
                "CPUで節・段落・文ID・原文ハッシュと前後段落を固定します。診断は次の修正生成と一体で行います。保護ブロックとインライン構造を自"
                "動変更の対象から除きます。",
            ),
            "redraft_japanese_paragraphs": (
                "節を読んで診断と局所修正を提案",
                "LLM-jp-4.1 33B BF16 highが、対象文そのものと前後段落を読み、"
                "一度だけ必要な局所修正を提案します。1文3候補"
                "・Luna検査・Sol順位付けは使いません。専門語や頻度だけを理由に書き換えず、中心語・接続・文体・意味を保ちます。",
            ),
            "review_japanese_sections": (
                "原文と候補を独立に読み採否を確認",
                "別のLLM-jp 33B BF16 high呼出しで原文・候補段落・置換IDを照合し、"
                "必要性・自然さ・意味・文体・前後関係を確認し"
                "ます。確認側では再改稿せず、問題のある案は棄却または人間確認へ回します。",
            ),
            "japanese_surface_quality_gate": (
                "原文と採否を照合して反映",
                "CPUが原文ハッシュ・一意な置換元・範囲・受け身と使役・文体・書名・数値・保護HTMLを再検査し、独立確認を通過した置換だけ反映し"
                "ます。未確認事項と保留は段落ID付きで人間確認へ送り、本文へ留保を追加しません。",
            ),
            "reconcile_candidate_context_after_proofreading": (
                "校正後の対応情報と出典述語を確認",
                "節・根拠・引用の対応をCPUで照合し、引用述語の帰属判定もLLM-jp 33B highで行います。"
                "未確定・不整合を通過扱いにせず、指定した校正区間の最後まで検査します。",
            ),
        }
        for node in data["nodes"]:
            if node["id"] in descriptions:
                purpose, summary = descriptions[node["id"]]
                data["review_policy"][node["id"]] = {
                    "purpose": purpose, "summary": summary,
                    "input": "原文HTML、文・段落ID、ハッシュ、節と隣接段落、保存済みの修正案・採否",
                    "output": "ID付き局所修正、独立した採否、原文対応の履歴と人間確認",
                }
                if node["resource_lane"] == "local_llm_gpu":
                    node["inference"] = {"model": "LLM-jp-4.1 33B BF16", "effort": "high"}
    if adoption:
        target = TARGET.with_name("manuscript-proofreading-adopt-data.js")
        data["proofreading_profile"] = profile
        purposes = {
            "diagnose_japanese_prose": (
                "保存本文の引用とIDを固定し、節ごとの本文と隣接段落だけを校正入力にする"),
            "redraft_japanese_paragraphs": (
                "各節1回のLLM-jpで不自然な連語・語法の診断、理由、局所修正案を同時に取得する。"
                "原稿全文の再送、未指摘段落の改稿、論証・構成・内容の追加をしない。"
                "HF自動起動が有効なら未取得の節がある時だけGPUを起動し、校正区間の終了・"
                "失敗・キャンセル時に停止する。取得済みの節はGPUなしで再利用し、"
                "応答不明の節は再生成しない。料金・時間・起動ID・送信記録はSQLiteに保存する"),
            "review_japanese_sections": (
                "保存済み案の原文対応、重複範囲、固定引用、ID、括弧をアプリが検査する。"
                "原文にない誤記の指摘は不適用として記録する。Sol mediumが内容変更・文の破損だけを"
                "実際の変更箇所の引用と理由付きで照合する。保留は原案とともに人間確認へ残す。"
                "LLM-jpへの再確認や別の言い換え探索をしない"),
            "japanese_surface_quality_gate": (
                "Lunaで生成時の既存表記規定だけを適用し、本文を旧稿へ戻さない"),
            "reconcile_candidate_context_after_proofreading": (
                "文ごとのevidence_usesから引用の実現先を復元する。節で利用可能な資料を"
                "引用不要の結論に割り当てず、採用本文・固定DOM・引用IDを維持する"),
            "redraft_body_prose": (
                "採用案を保持し、確認不足を述べる留保を既存のCodex抽出で人間確認へ移す。"
                "原文の完全一致削除だけを記録し、実質的な分析条件・引用・脚注・IDを保持する"),
        }
        for node in data["nodes"]:
            if node["id"] in purposes:
                data["review_policy"][node["id"]] = {
                    "purpose": purposes[node["id"]], "summary": purposes[node["id"]],
                    "input": "固定本文スロット・段落ID・引用支持関係・既存執筆規定",
                    "output": "採用本文、表記整形、構造と出典対応の保存記録",
                }
                scoped_io = {
                    "diagnose_japanese_prose": (
                        "保存本文・段落ID・節構造・保護要素。モデルへの送信なし",
                        "各節の本文と前後の段落、固定されたHTMLとID"),
                    "redraft_japanese_paragraphs": (
                        "その節の本文と段落ID、前後の段落だけ。各節1要求",
                        "問題箇所のbefore/after・理由・人間確認事項。本文全体は返さない"),
                    "review_japanese_sections": (
                        "機械照合済みの局所案と対象段落を一度だけまとめてSolへ送る",
                        "変更箇所と理由付きの採否、不適用記録、固定DOMの検査と採用本文"),
                }
                if node["id"] in scoped_io:
                    source, result = scoped_io[node["id"]]
                    data["review_policy"][node["id"]].update(input=source, output=result)
                if node["resource_lane"] == "local_llm_gpu":
                    node["inference"] = {"model": "LLM-jp-4.1 33B BF16", "effort": "high"}
                elif node["id"] == "japanese_surface_quality_gate":
                    node["inference"] = {"model": "gpt-6-luna", "effort": "low"}
                elif node["id"] in {"review_japanese_sections", "redraft_body_prose"}:
                    node["inference"] = {"model": "gpt-5.6-sol", "effort": "medium"}
    output = (
        "// Generated from the current executable manuscript workflow.\n"
        "// Do not edit by hand. Regenerate with scripts/generate_manuscript_graph.py.\n"
        f"window.MANUSCRIPT_GRAPH_V32 = {json.dumps(data, ensure_ascii=False, indent=2)};\n"
    )
    if "--check" in sys.argv:
        if target.read_text(encoding="utf-8") != output:
            raise SystemExit("V32 DAG data is stale")
    else:
        target.write_text(output, encoding="utf-8")
    print(
        f"{len(data['nodes'])} nodes, {len(data['edges'])} edges "
        f"(diagram V32, workflow V{data['version']})"
    )


if __name__ == "__main__":
    main()
