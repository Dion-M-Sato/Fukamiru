"""Authoritative display metadata for executable workflow nodes."""

from fukamiru.workflow.graph import WorkflowDefinition

NODE_FUNCTION_NAMES_JA = {
    "academic_evidence_resolution": "学術的根拠と訳語を確定",
    "apply_human_revision": "人間が修正した翻訳版を保存",
    "audit_final_findings": "重大指摘を最終再監査",
    "audit_ocr": "OCR品質とページ連続性を監査",
    "audit_structure": "主張と談話構造を監査",
    "classify_audit_findings": "監査指摘を修復・人間確認・停止へ分類",
    "bind_claims_to_propositions": "主張を原子命題へ対応付け",
    "build_argument_graph": "主張・根拠・論拠グラフを構築",
    "select_defense_obligations": "本文で処理すべき反論を選別",
    "bounded_research_retry": "不足した根拠と対抗資料を限定的に再探索",
    "build_chunks": "検索チャンクを構築",
    "build_claim_tree": "主張階層を構築",
    "build_discourse_tree": "修辞的談話構造を構築",
    "build_semantic_index": "BGE-M3意味検索索引を構築",
    "build_source_preservation_map": "原資料命題と主張の保存関係を作成",
    "calibrate_manuscript_certainty": "根拠の確実度に応じて断定表現を調整",
    "classify_propositions": "命題型を分類",
    "classify_verification_scopes": "原子命題の検証範囲を分類",
    "decide_evidence_realization": "断定・帰属・限定・省略を確定",
    "decompose_memo": "メモを原子命題へ分解",
    "derive_atomic_evidence_obligations": "原子命題ごとの根拠要件を導出",
    "draft_rewrite": "固定した計画から原稿を生成",
    "draft_translation": "翻訳ドラフトを生成",
    "ensure_ingested": "資料の取り込み状態を確認",
    "evaluate_human_revision_requirement": "人間による修正の要否を判定",
    "extract_manuscript_references": "原稿から文献情報を抽出",
    "extract_proofreading_sentences": "安定した日本語文と作品名を抽出",
    "extract_text": "本文テキストを抽出",
    "finalize_authoring": "凍結した原稿案を保存",
    "finalize_translation": "承認済み翻訳HTMLを確定",
    "formulate_research_question": "中心となる研究課題を固定",
    "ratify_research_problem": "根拠に基づき研究問題を確定",
    "freeze_final_candidate": "最終候補とハッシュを固定",
    "gaperon_source_inspection": "Gaperonでフランス語原文を検査",
    "generate_hypotheses": "範囲を限定した仮説を生成",
    "generate_proofreading_candidates": "全文推敲候補を生成",
    "human_approval": "必要な場合だけ人間の承認を依頼",
    "human_review": "翻訳を人間が確認",
    "index_chunks": "全文検索索引へチャンクを公開",
    "infer_structure": "文書構造と出典範囲を推定",
    "ingest_ocr_outputs": "残るOCR結果を取り込み",
    "ingest_ready_sources": "原文取り込みとOCRを並行処理",
    "inspect_text_layer": "PDFテキスト層とOCR要否を判定",
    "japanese_context": "日本語推敲用RAG文脈を準備",
    "japanese_diagnostic": "日本語推敲対象を診断",
    "japanese_surface_quality_gate": "適用後の日本語表層品質を検証",
    "join_draft_audits": "原資料保存監査と構造監査を統合",
    "join_submission_validations": "提出前の各検証ゲートを統合",
    "llm_jp_post_edit": "llm-jpで限定的な日本語推敲を実行",
    "locate_source": "資料パスを特定",
    "lock_section_contracts": "節ごとの生成契約を固定",
    "persist_rewrite_proposal": "改稿案を検証して保存",
    "plan_manuscript_structure": "原稿全体の構造を計画",
    "plan_research_queries": "資料別の調査クエリを計画",
    "plan_semantic_paragraphs": "意味段落の展開を計画",
    "plan_term_introductions": "重要語の初出方法を計画",
    "polish_prose": "局所的な文章修正を適用",
    "post_edit_validation": "日本語表層品質と推敲制約を検証",
    "prepare_context": "RAGと用語の翻訳文脈を準備",
    "prepare_note": "原メモと対象原稿を登録",
    "prepare_rewrite_context": "原稿・出典・レビュー文脈を準備",
    "preprocess_proofreading_candidates": "推敲候補を正規化・重複排除・差分化",
    "publish_proofreading_shadow": "安全性と推敲プレビューを公開",
    "rank_proofreading_candidates": "原文を含めて推敲候補を順位付け",
    "diagnose_japanese_prose": "日本語の話題設定と構文を診断",
    "redraft_japanese_paragraphs": "前後の文脈を踏まえて段落を再起草",
    "review_japanese_sections": "節全体の文体と主張・出典の保持を確認",
    "redraft_body_prose": "脚注付き本文を節ごと一度だけ改稿",
    "finalize_section_headings": "完成した本文に合わせて節見出しを一度だけ整える",
    "start_paragraph_formation": "節の主張を固定したまま段落形成を開始",
    "reconcile_candidate_context": "原稿候補と構造計画を再接続",
    "reconcile_candidate_context_after_proofreading": "推敲後の原稿候補を再接続",
    "register_asset": "不変の資料資産として登録",
    "render_evidence_footnotes": "根拠脚注と参考文献を描画",
    "render_manuscript_references": "指定書式で参考文献を描画",
    "repair_failed_sections": "失敗した節だけを修復",
    "repair_final_coherence": "最終候補の意味的一貫性を局所修復",
    "repair_translation": "翻訳の制約違反を修復",
    "research_trigger_detection": "研究参照が必要な語句を検出",
    "resolve_enumeration_context": "列挙と対比の文脈を解決",
    "resolve_manuscript_evidence": "ローカルRAGとオンライン学術根拠を解決",
    "resolve_reference_metadata": "文献メタデータを解決・重複排除",
    "review_manuscript_reasoning": "原稿の論理と調査要件を確認",
    "run_ocr": "MistralまたはNDLOCR-LiteでOCRを実行",
    "publish_research_index": "有用と判定した調査資料をRAG索引へ追加",
    "run_ocr_jobs": "残る言語別OCRジョブを実行",
    "scan_sources": "管理対象の資料を走査",
    "screen_proofreading_with_luna": "Lunaで推敲候補の危険性を判定",
    "select_proofreading_candidates": "安全性と採用候補を分離して選択",
    "semantic_validation": "意味とモダリティを検証",
    "submission_package": "原稿と監査マニフェストを書き出し",
    "translation_compliance_validation": "翻訳禁止事項・人名・判断を検証",
    "validate_argument_coverage": "論証上の役割の充足を検証",
    "validate_discourse_introductions": "初出語と引用目的の談話対応を検証",
    "validate_draft_coherence": "草稿の談話的一貫性を検証",
    "validate_evidence_ready": "原子命題の根拠要件を検証",
    "validate_evidence_support": "主張の根拠と明示的推論を検証",
    "validate_final_coherence": "日本語表層復元後の最終一貫性を検証",
    "validate_footnoted_coherence": "脚注付き最終候補を読み取り専用で再監査",
    "validate_footnoted_references": "脚注から再構成した参考文献を検証",
    "validate_footnoted_structure": "脚注描画後の正本構造を検証",
    "validate_reference_quality": "参考文献の完全性・来歴・冪等性を検証",
    "validate_rendered_structure": "描画後の正本構造を検証",
    "validate_rewrite_coherence": "節と全文の一貫性を検証・修復",
    "validate_source_preservation": "原資料の命題保存を検証",
}

V31_NODE_FUNCTION_NAMES_JA = {
    "resolve_enumeration_context": "列挙候補から段落の分析上の焦点を選ぶ",
    "plan_semantic_paragraphs": "段落ごとに論を進める役割を計画",
    "start_paragraph_formation": "主張と出典を保って段落形成を再開",
    "draft_rewrite": "対象の分析を起草しLLMの推論を記録",
    "polish_prose": "主張を保って停滞した論述を組み直す",
    "diagnose_japanese_prose": "日本語と段落の分析の進展を診断",
    "review_japanese_sections": "節の論述・日本語・出典対応を確認",
    "redraft_body_prose": "固定HTML内の文章を節ごと一度だけ整える",
    "finalize_section_headings": "本文に合わせて節見出しの日本語を一度だけ整える",
    "validate_footnoted_coherence": "改稿後のHTMLと固定要素を機械的に照合",
    "validate_discourse_introductions": "談話の追加監査を省き確認事項に記録",
    "audit_final_findings": "最終指摘の追加監査を省き確認事項に記録",
}


PROOFREADING_PATCH_NODE_IDS = frozenset(
    {
        "diagnose_japanese_prose",
        "redraft_japanese_paragraphs",
        "review_japanese_sections",
        "redraft_body_prose",
        "finalize_section_headings",
        "extract_proofreading_sentences",
        "generate_proofreading_candidates",
        "preprocess_proofreading_candidates",
        "screen_proofreading_with_luna",
        "rank_proofreading_candidates",
        "select_proofreading_candidates",
        "publish_proofreading_shadow",
        "japanese_surface_quality_gate",
        "reconcile_candidate_context_after_proofreading",
    }
)

V32_LOCAL_PROOFREADING_NAMES_JA = {
    "extract_proofreading_sentences": "校正する文IDと原文ハッシュを確定",
    "generate_proofreading_candidates": "文IDに対応する局所修正を生成",
    "preprocess_proofreading_candidates": "局所修正の原文対応と文体を検査",
    "rank_proofreading_candidates": "原文と局所修正候補を比較して選択",
    "publish_proofreading_shadow": "IDを保って検証済み局所修正を反映",
}


def workflow_node_metadata(
    definition: WorkflowDefinition | None, node_id: str
) -> dict[str, object]:
    """Return display metadata without inventing an executable node."""

    node = (
        next((item for item in definition.nodes if item.id == node_id), None)
        if definition
        else None
    )
    contextual_names = {
        "prepare": "原文IDと節の校正文脈を固定",
        "propose": "LLM-jp 33B highで診断と局所修正を提案",
        "review": "LLM-jp 33B highで原文と候補の採否を確認",
        "apply": "原文照合を通過した修正だけ反映",
        "reconcile": "LLM-jp 33B highで校正後の対応情報と出典述語を確認",
    }
    contextual = (
        node.handler.removeprefix("authoring.llmjp33b_")
        if node and node.handler.startswith("authoring.llmjp33b_")
        else ""
    )
    adopted_names = {
        "prepare_sections": "節ごとの校正入力とID・引用要素を固定",
        "correct_sections_once": "各節1回で日本語を校正し管理対象のHF Jobを停止",
        "validate_sections": "原文を機械照合しSolで内容変更・文の破損だけを確認",
        "diagnose": "全文を一度読み不自然な連語・語法を診断",
        "suggest": "指摘箇所だけの理由と局所修正案を提示",
        "verify": "指摘の解消を一度だけ確認し別案を探索しない",
        "prepare": "引用要素を保持して本文スロットを固定",
        "propose": "LLM-jp 33B highの語感で日本語案を作成",
        "review": "採用案の誤字・引用前後の破損を修復・再確認",
        "apply": "Lunaで既存表記規定を適用し本文案を保持",
        "reconcile": "実際の根拠利用から引用・脚注・IDの対応を維持",
        "finish": "採用本文を保持し確認不足の留保を人間確認へ移送",
    }
    adopted = (node.handler.removeprefix("authoring.llmjp_adopt_")
               if node and node.handler.startswith("authoring.llmjp_adopt_") else "")
    return {
        "node_id": node_id,
        "title": node.title if node else node_id.replace("_", " "),
        "function_name_ja": (
            adopted_names.get(adopted) or contextual_names.get(contextual)
        ) or (
            V32_LOCAL_PROOFREADING_NAMES_JA.get(node_id)
            if definition
            and definition.name == "manuscript_generator" and definition.version == "32"
            else None
        ) or (
            V31_NODE_FUNCTION_NAMES_JA.get(node_id)
            if definition and definition.version in {"31", "32"}
            else None
        ) or NODE_FUNCTION_NAMES_JA.get(
            node_id, node.title if node else "機能名未登録"
        ),
        "resource_lane": node.resource_lane if node else None,
        "known_in_workflow": node is not None,
    }


def workflow_supports_proofreading_patches(definition: WorkflowDefinition | None) -> bool:
    return bool(
        definition
        and any(node.id in PROOFREADING_PATCH_NODE_IDS for node in definition.nodes)
    )
