import { externalRunWaitState } from "./run-recovery.js";

const RUN_STATUS_LABELS = {
  queued: "開始待ち",
  running: "実行中",
  waiting_human: "人間レビュー待ち",
  completed: "完了",
  failed: "失敗",
  cancelled: "中止",
};

export const MANUSCRIPT_NODE_LABELS = Object.freeze({
  prepare_rewrite_context: "書換え準備",
  extract_manuscript_references: "文献抽出",
  review_manuscript_reasoning: "論理レビュー",
  plan_manuscript_structure: "構造計画",
  resolve_manuscript_evidence: "証拠解決",
  plan_research_queries: "検索課題と検索文の整理",
  validate_evidence_support: "主張の根拠・推論を検証",
  resolve_reference_metadata: "文献メタデータ",
  resolve_enumeration_context: "列挙文脈解決",
  plan_semantic_paragraphs: "意味段落計画",
  plan_term_introductions: "初出語・引用の談話計画",
  analyze_japanese_structure: "日本語の形態素・係り受け解析",
  validate_discourse_introductions: "初出語・引用目的の最終検証",
  draft_rewrite: "書換えドラフト",
  calibrate_manuscript_certainty: "根拠の確実度・断定表現の調整",
  resolve_manuscript_predicate_relations: "述語関係解決",
  validate_rewrite_coherence: "書換え一貫性",
  detect_japanese_polish_targets: "研磨対象検出",
  polish_manuscript_japanese: "日本語研磨",
  select_japanese_polish_candidates: "研磨候補選択",
  japanese_surface_quality_gate: "表層品質ゲート",
  redraft_body_prose: "脚注付き本文の話題設定と構文を再起草",
  build_collocation_frames: "コロケーションフレーム構築",
  generate_predicate_candidates: "述語候補生成",
  generate_collocation_probes: "自然文Probe生成",
  score_with_llmjp_base: "llm-jp Base採点",
  verify_semantics_and_decide: "意味保存ゲート",
  publish_collocation_suggestions: "候補評価を提示",
  extract_proofreading_sentences: "文抽出と作品名登録",
  generate_proofreading_candidates: "全文推敲候補生成",
  preprocess_proofreading_candidates: "機械保護と文字差分",
  screen_proofreading_with_luna: "Luna警告判定",
  select_proofreading_candidates: "推敲候補選択",
  publish_proofreading_shadow: "推敲プレビュー公開",
  validate_final_coherence: "最終一貫性",
  render_manuscript_references: "参考文献整形",
  validate_reference_quality: "文献品質検証",
  validate_rendered_structure: "描画後の正本構造検証",
  repair_final_coherence: "最終原稿の局所修復",
  render_evidence_footnotes: "根拠脚注を生成",
  validate_footnoted_references: "脚注文献の品質再検証",
  validate_footnoted_structure: "脚注後の正本構造検証",
  validate_footnoted_coherence: "脚注付き最終候補を再監査",
  freeze_final_candidate: "最終候補を固定",
  audit_final_findings: "重大指摘の最終再監査",
  persist_rewrite_proposal: "提案を保存",
  prepare_note: "原メモと空の対象原稿を登録",
  decompose_memo: "メモを命題へ分解",
  classify_propositions: "命題型を分類",
  formulate_research_question: "中心問題を固定",
  build_claim_tree: "主張階層を生成",
  build_argument_graph: "論証グラフを生成",
  generate_hypotheses: "仮説候補を生成",
  build_discourse_tree: "談話構造を設計",
  lock_section_contracts: "節契約を固定",
  draft_sections: "節本文を生成",
  audit_structure: "論証・談話構造を監査",
  repair_failed_sections: "失敗した節だけを修復",
  polish_prose: "局所校正",
  reconcile_candidate_context: "候補と構造計画を再接続",
  validate_draft_coherence: "新規草稿の一貫性を検証",
  finalize_authoring: "承認待ち原稿を保存",
});

const NODE_LABELS = {
  ensure_ingested: "資料準備",
  gaperon_source_inspection: "フランス語原文検査",
  research_trigger_detection: "研究参照の検出",
  academic_evidence_resolution: "学術的根拠と訳語の確定",
  prepare_context: "翻訳コンテキスト準備",
  draft_translation: "翻訳",
  translation_compliance_validation: "翻訳準拠検証",
  repair_translation: "翻訳の準拠修復",
  semantic_validation: "意味・モダリティ検証",
  japanese_diagnostic: "日本語推敲箇所の診断",
  japanese_context: "日本語RAGコンテキスト準備",
  llm_jp_post_edit: "llm-jp日本語推敲",
  post_edit_validation: "日本語表層品質ゲート",
  human_review: "翻訳の人間レビュー",
  finalize_translation: "翻訳HTML確定",
  ...MANUSCRIPT_NODE_LABELS,
  scan_sources: "資料走査",
  ingest_sources: "資料取り込み",
  locate_source: "資料特定",
  inspect_text_layer: "PDFテキスト層判定",
  run_ocr: "言語別OCR実行",
  register_asset: "資料登録",
  extract_text: "本文抽出",
  audit_ocr: "OCR品質監査",
  infer_structure: "構造推定",
  build_chunks: "検索チャンク構築",
  index_chunks: "全文検索索引更新",
  ingest_ready_sources: "OCR不要資料の取り込み",
  run_ocr_jobs: "言語別OCRジョブ実行",
  ingest_ocr_outputs: "OCR済み資料の取り込み",
  build_semantic_index: "意味検索索引構築",
};

const LANE_LABELS = {
  ingestion_cpu: "CPU処理枠",
  embedding_gpu: "埋め込みGPU処理枠",
  codex_remote: "Codex処理枠",
  local_llm_gpu: "ローカルLLM処理枠",
};

const LIVE_RUN_STATUSES = new Set(["queued", "running", "waiting_human"]);

const COHERENCE_ROUTE_LABELS = {
  human_review: "人間レビュー",
  bounded_rewrite: "限定範囲の再改稿",
  claim_limitation: "主張の限定",
  additional_research: "追加研究",
};

const ARGUMENT_PROCESS_LABELS = {
  block_roles: "ブロック役割判定",
  reference_classification: "作品・理論参照分類",
  claim_evidence_links: "主張―根拠リンク生成",
  integration_validation: "決定論的統合検証",
};

function activeNodeFor(item) {
  const nodes = item?.current_nodes || item?.active_nodes || [];
  return ["running", "waiting_human", "queued"]
    .map((status) => nodes.find((node) => node.status === status))
    .find(Boolean) || nodes[0] || null;
}

export function runNodeLabel(nodeId) {
  if (nodeId && typeof nodeId === "object") {
    return nodeId.function_name_ja
      || NODE_LABELS[nodeId.node_id]
      || nodeId.title
      || nodeId.node_id
      || "処理";
  }
  return NODE_LABELS[nodeId] || String(nodeId || "処理").replaceAll("_", " ");
}

export function runNodeTechnicalName(node) {
  return String(node?.node_id || "unknown_node");
}

export function runDisplayNodes(item) {
  return item?.current_nodes || item?.active_nodes || [];
}

export function argumentProcessLabel(processName) {
  return ARGUMENT_PROCESS_LABELS[processName]
    || String(processName || "argument処理").replaceAll("_", " ");
}

export function runHasLiveProgress(item) {
  const run = item?.run || item || {};
  return LIVE_RUN_STATUSES.has(run.status);
}

export function runExecutionState(item) {
  const run = item?.run || item || {};
  const externalWait = externalRunWaitState(item);
  if (externalWait) {
    return {
      statusLabel: externalWait.statusLabel,
      detail: externalWait.title,
      node: activeNodeFor(item),
      externalWait,
    };
  }
  const node = activeNodeFor(item);
  const fallback = RUN_STATUS_LABELS[run.status] || String(run.status || "不明");
  if (!node) {
    if (run.status === "completed" && run.output?.quality_status === "needs_human_review") {
      return {
        statusLabel: run.output?.completion_status === "conditional_success"
          ? "条件付き成功・要確認" : "処理完了・品質要確認",
        detail: "原稿を保存済み。未解決事項・候補選択をReviewで確認してください",
        node: null,
      };
    }
    if (
      run.status === "completed"
      && run.output?.completion_status === "conditional_success"
    ) {
      return {
        statusLabel: "条件付き成功",
        detail: "追加研究待ちの改稿案を保存済み",
        node: null,
      };
    }
    return { statusLabel: fallback, detail: fallback, node: null };
  }
  const nodeLabel = runNodeLabel(node);
  const laneLabel = LANE_LABELS[node.resource_lane]
    || String(node.resource_lane || "処理枠").replaceAll("_", " ");
  if (node.status === "queued") {
    return {
      statusLabel: `${laneLabel}待ち`,
      detail: `${laneLabel}待ち（次: ${nodeLabel}）`,
      node,
    };
  }
  if (node.status === "waiting_human") {
    const waitKind = node.output?.ocr_wait_kind || run.output?.ocr_wait_kind;
    if (waitKind === "ocr_language") {
      return { statusLabel: "OCR言語確認待ち", detail: `${nodeLabel}：ja / en / frを指定`, node };
    }
    return { statusLabel: "人間レビュー待ち", detail: `${nodeLabel}待ち`, node };
  }
  if (node.status === "running") {
    return { statusLabel: "実行中", detail: `${nodeLabel}中`, node };
  }
  return { statusLabel: fallback, detail: nodeLabel, node };
}

export function runNodeStatusLabel(node) {
  if (!node) return "不明";
  if (node.status === "queued") {
    const laneLabel = LANE_LABELS[node.resource_lane]
      || String(node.resource_lane || "処理枠").replaceAll("_", " ");
    return `${laneLabel}待ち`;
  }
  return RUN_STATUS_LABELS[node.status] || String(node.status || "不明");
}

export function progressUnitLabel(progress, activeNodes = []) {
  if (progress?.kind === "manuscript_query_plan") return "検索文の計画・バッチ";
  if (progress?.kind === "manuscript_evidence_support") return "主張の根拠検証・調査課題";
  if (progress?.kind === "manuscript_final_findings") return "最終稿の指摘再判定・バッチ";
  if (progress?.kind === "manuscript_certainty") return "確実度を確認した文";
  const activeNode = activeNodeFor({ active_nodes: activeNodes });
  const phase = activeNode?.node_id || progress?.phase;
  if (progress?.kind === "llm_jp_post_edit_chunks") return "llm-jp校正チャンク";
  if (progress?.kind === "manuscript_reasoning_batches") {
    return `${runNodeLabel("review_manuscript_reasoning")}・バッチ`;
  }
  if (progress?.kind === "manuscript_structure_plan_passes") {
    const stages = {
      skeleton: "節順序・中心命題",
      argument: "論証関係・具体例分類",
      temporal: "年代言及・段落時間範囲",
      discourse: "接続関係・段落統合",
      global_validation: "全文構造の決定論的検証",
    };
    const stage = stages[progress?.stage];
    return stage
      ? `${runNodeLabel("plan_manuscript_structure")}：${stage}${progress?.repair_mode ? "（差分修復）" : ""}`
      : `${runNodeLabel("plan_manuscript_structure")}・工程`;
  }
  if (progress?.kind === "manuscript_argument_processes") {
    const label = argumentProcessLabel(progress?.process_name);
    return `${runNodeLabel("plan_manuscript_structure")}：${label}${progress?.repair_mode ? "（未解決項目のみ再試行）" : ""}`;
  }
  if (progress?.kind === "manuscript_research_queries") {
    return `${runNodeLabel("resolve_manuscript_evidence")}・検索クエリ`;
  }
  if (progress?.kind === "manuscript_reference_entries") {
    return `${runNodeLabel("resolve_reference_metadata")}・項目`;
  }
  if (progress?.kind === "manuscript_enumeration_batches") {
    if (progress?.unit === "candidate") return "処理した列挙候補";
    return `${runNodeLabel("resolve_enumeration_context")}・バッチ`;
  }
  if (progress?.kind === "manuscript_paragraph_plan_passes") {
    return `${runNodeLabel("plan_semantic_paragraphs")}・パス`;
  }
  if (progress?.kind === "manuscript_rewrite_batches") {
    return `${runNodeLabel("draft_rewrite")}・バッチ`;
  }
  if (progress?.kind === "manuscript_predicate_candidates") {
    return `${runNodeLabel("resolve_manuscript_predicate_relations")}・候補バッチ`;
  }
  if (progress?.kind === "manuscript_coherence_passes") {
    const coherenceNode = phase === "repair_final_coherence"
      ? "repair_final_coherence"
      : phase === "validate_final_coherence"
      ? "validate_final_coherence"
      : "validate_rewrite_coherence";
    return `${runNodeLabel(coherenceNode)}・検証パス`;
  }
  if (progress?.kind === "manuscript_polish_chunks") {
    return `${runNodeLabel("polish_manuscript_japanese")}・チャンク`;
  }
  if (progress?.kind === "manuscript_polish_targets") {
    return `${runNodeLabel("detect_japanese_polish_targets")}・対象`;
  }
  if (progress?.kind === "manuscript_polish_candidates") {
    return `${runNodeLabel("polish_manuscript_japanese")}・候補生成`;
  }
  if (progress?.kind === "manuscript_polish_selection") {
    return `${runNodeLabel("select_japanese_polish_candidates")}・対象`;
  }
  if (progress?.kind === "translation_batches" && phase === "repair_translation") {
    return "翻訳準拠修復バッチ";
  }
  return "翻訳バッチ";
}

const PATCH_STAGE_LABELS = {
  sentences_extracted: "推敲対象文を抽出",
  candidates_generating: "推敲候補を生成中",
  candidates_generated: "推敲候補を生成",
  candidates_preprocessed: "候補を前処理",
  luna_screened: "Luna安全性判定",
  candidates_ranked: "候補を順位付け",
  candidates_selected: "採用候補を選択",
  shadow_published: "パッチプレビューを公開",
};

const PATCH_STATUS_LABELS = {
  clean: "安全",
  invalid: "無効",
  luna_suspicious: "要注意",
  luna_unavailable: "Luna未判定",
  policy_blocked: "規則で棄却",
  stale: "失効",
  generated: "生成済み",
  detected: "検出済み",
};

export function patchProcessingState(patch) {
  if (!patch) {
    return { status: "unavailable", label: "状態なし", detail: "パッチ状態を取得できません" };
  }
  if (patch.applicable === false || patch.state === "not_applicable") {
    return {
      status: "not_applicable",
      label: "パッチ非対象",
      detail: "このフローは推敲パッチを生成しません",
    };
  }
  const total = Number(patch.total || 0);
  const selected = Number(patch.selected || 0);
  const applied = Number(patch.applied || 0);
  const counts = Object.entries(patch.status_counts || {})
    .filter(([, count]) => Number(count) > 0)
    .map(([status, count]) => `${PATCH_STATUS_LABELS[status] || status} ${Number(count)}件`);
  const stage = PATCH_STAGE_LABELS[patch.stage] || "パッチ処理";
  const metrics = total
    ? [`候補 ${total}件`, `選択 ${selected}件`, `適用 ${applied}件`, ...counts].join(" · ")
    : patch.empty
      ? "対象となるパッチはありません"
      : "候補はまだ生成されていません";
  const labels = {
    pending: "未開始",
    running: "処理中",
    waiting: "次工程待ち",
    completed: patch.empty ? "完了・対象なし" : "完了",
    failed: "失敗",
  };
  return {
    status: patch.state || "unavailable",
    label: labels[patch.state] || "状態なし",
    detail: `${stage} · ${metrics}`,
  };
}

export function terminalRunProgressState(item, progress = item?.progress) {
  const run = item?.run || item || {};
  const conditionalSuccess = run.status === "completed"
    && run.output?.completion_status === "conditional_success";
  if (!["failed", "cancelled"].includes(run.status) && !conditionalSuccess) {
    return null;
  }

  const completed = progress?.completed;
  const total = progress?.total;
  const percentage = progress?.percentage;
  const hasStopPosition = completed != null || total != null;
  const progressLabel = progressUnitLabel(progress, item?.active_nodes || []);
  const ratio = total != null
    ? `${completed ?? 0} / ${total}`
    : completed != null
      ? `${completed}完了時点`
      : "";
  const percent = percentage != null ? `（${percentage}%）` : "";
  const stoppedAt = hasStopPosition
    ? `${progressLabel} ${ratio}${percent}${conditionalSuccess ? "で条件付き終了" : "で停止"}`
    : null;

  const routeCounts = progress?.blocking_route_counts || {};
  const routeDetails = Object.entries(routeCounts)
    .filter(([, count]) => Number(count) > 0)
    .map(([route, count]) => `${COHERENCE_ROUTE_LABELS[route] || route} ${count}件`);
  const blockingCount = Object.values(routeCounts)
    .reduce((sum, count) => sum + Math.max(0, Number(count) || 0), 0);
  const coherenceFailure = run.error?.type === "CoherenceResolutionRequired";
  let reason = null;
  if (coherenceFailure) {
    reason = blockingCount
      ? `未解決の整合性問題 ${blockingCount}件：${routeDetails.join(" · ")}`
      : "未解決の整合性問題が残ったため停止しました。";
  } else if (run.status === "cancelled") {
    reason = "このRunは中止されており、処理は進行していません。";
  } else if (conditionalSuccess) {
    reason = "追加研究が必要な状態の改稿案を保存しました。";
  }

  return {
    status: conditionalSuccess ? "conditional_success" : run.status,
    title: conditionalSuccess
      ? "条件付き成功として保存しました"
      : run.status === "failed"
        ? "Runは失敗しました"
        : "Runは中止されました",
    stoppedAt,
    reason,
  };
}
