import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  argumentProcessLabel,
  patchProcessingState,
  progressUnitLabel,
  runDisplayNodes,
  runExecutionState,
  runHasLiveProgress,
  runNodeLabel,
  runNodeStatusLabel,
  terminalRunProgressState,
} from "../src/run-labels.js";
import {
  externalResumeRequestOptions,
  externalRunWaitState,
  externalWaitMarkup,
} from "../src/run-recovery.js";

test("V32 literature capacity waits expose pending-search recovery", () => {
  const item = {
    run: {
      workflow_name: "manuscript_generator", workflow_version: "32", status: "waiting_human",
      error: { type: "ExternalServiceUnavailable", reason: "literature_search_deferred" },
    },
    active_nodes: [{ node_id: "resolve_manuscript_evidence", status: "waiting_human" }],
  };
  const wait = externalRunWaitState(item);
  assert.equal(wait.statusLabel, "文献DB復旧待ち");
  assert.match(wait.detail, /未完了の検索だけ/);
  assert.deepEqual(externalResumeRequestOptions(wait), { method: "POST" });
  assert.equal(runExecutionState(item).statusLabel, "文献DB復旧待ち");
});

test("V22 external outage is labeled as a recovery wait and resumes without a fingerprint", () => {
  const item = {
    run: {
      id: "run-outage",
      workflow_name: "manuscript_rewrite",
      workflow_version: "22",
      status: "waiting_human",
      error: { type: "ExternalServiceUnavailable", reason: "external_service_unavailable" },
    },
    active_nodes: [{ node_id: "review_manuscript_reasoning", status: "waiting_human" }],
  };

  const wait = externalRunWaitState(item);
  assert.equal(wait.kind, "outage");
  assert.equal(runExecutionState(item).statusLabel, "外部Codex復旧待ち");
  assert.deepEqual(externalResumeRequestOptions(wait), { method: "POST" });
});

test("V22 engine authorization sends only the exact pending fingerprint", () => {
  const target = "b".repeat(64);
  const previous = "a".repeat(64);
  const item = {
    run: {
      workflow_name: "manuscript_rewrite",
      workflow_version: 22,
      status: "waiting_human",
      error: {
        type: "ExternalServiceUnavailable",
        reason: "inference_engine_authorization_required",
        previous_engine_fingerprint: previous,
        target_engine_fingerprint: target,
      },
    },
  };

  const wait = externalRunWaitState(item);
  assert.equal(wait.statusLabel, "推論エンジン変更の承認待ち");
  assert.equal(wait.previousFingerprint, previous);
  assert.equal(wait.targetFingerprint, target);
  assert.deepEqual(JSON.parse(externalResumeRequestOptions(wait).body), {
    authorized_engine_fingerprint: target,
  });
  const markup = externalWaitMarkup({ id: "run-engine" }, wait);
  assert.match(markup, /推論エンジンが変更されています/);
  assert.match(markup, new RegExp(target));
  assert.match(markup, /このfingerprintを承認して再開/);
  assert.match(markup, /class="danger-action run-external-cancel"/);
});

test("invalid engine fingerprints cannot produce an authorization request", () => {
  const wait = externalRunWaitState({
    run: {
      workflow_name: "manuscript_rewrite",
      workflow_version: 22,
      status: "waiting_human",
      error: {
        type: "ExternalServiceUnavailable",
        reason: "inference_engine_authorization_required",
        target_engine_fingerprint: "not-a-sha256",
      },
    },
  });

  assert.equal(wait.canResume, false);
  assert.equal(externalResumeRequestOptions(wait), null);
});

test("ordinary and pre-V22 human waits keep the human review label", () => {
  const item = {
    run: {
      workflow_name: "manuscript_rewrite",
      workflow_version: 21,
      status: "waiting_human",
      error: { reason: "external_service_unavailable" },
    },
    active_nodes: [{ node_id: "validate_rewrite_coherence", status: "waiting_human" }],
  };

  assert.equal(externalRunWaitState(item), null);
  assert.equal(runExecutionState(item).statusLabel, "人間レビュー待ち");
});

test("a spoofed external reason on an ordinary human wait exposes no recovery action", () => {
  const item = {
    run: {
      workflow_name: "manuscript_rewrite",
      workflow_version: 22,
      status: "waiting_human",
      error: {
        type: "CoherenceResolutionRequired",
        reason: "external_service_unavailable",
      },
    },
    active_nodes: [{
      node_id: "validate_rewrite_coherence",
      status: "waiting_human",
      error: { type: "CoherenceResolutionRequired" },
    }],
  };

  assert.equal(externalRunWaitState(item), null);
  assert.equal(runExecutionState(item).statusLabel, "人間レビュー待ち");
});

test("a snapshot with another active node or open review exposes no external action", () => {
  const base = {
    run: {
      workflow_name: "manuscript_rewrite",
      workflow_version: 22,
      status: "waiting_human",
      error: {
        type: "ExternalServiceUnavailable",
        reason: "external_service_unavailable",
      },
    },
    active_nodes: [{
      node_id: "review_manuscript_reasoning",
      status: "waiting_human",
      error: { type: "ExternalServiceUnavailable" },
    }],
  };

  assert.equal(externalRunWaitState({
    ...base,
    active_nodes: [
      ...base.active_nodes,
      { node_id: "human_review", status: "waiting_human", error: null },
    ],
  }), null);
  assert.equal(externalRunWaitState({
    ...base,
    reviews: [{ status: "open" }],
  }), null);
});

test("processing complete never implies manuscript quality passed", () => {
  const state = runExecutionState({ run: {
    status: "completed", output: { completion_status: "success", quality_status: "needs_human_review" },
  }});
  assert.equal(state.statusLabel, "処理完了・品質要確認");
  assert.match(state.detail, /Review/);
  assert.equal(progressUnitLabel({kind: "manuscript_evidence_support"}), "主張の根拠検証・調査課題");
});

test("a queued manuscript review reports the occupied Codex lane", () => {
  const item = {
    run: { status: "running" },
    active_nodes: [{
      node_id: "review_manuscript_reasoning",
      status: "queued",
      resource_lane: "codex_remote",
    }],
  };

  const execution = runExecutionState(item);

  assert.equal(execution.statusLabel, "Codex処理枠待ち");
  assert.equal(execution.detail, "Codex処理枠待ち（次: 論理レビュー）");
  assert.equal(runNodeStatusLabel(item.active_nodes[0]), "Codex処理枠待ち");
});

test("translation repair does not use the draft translation label", () => {
  const activeNodes = [{
    node_id: "repair_translation",
    status: "running",
    resource_lane: "codex_remote",
  }];

  assert.equal(
    runExecutionState({ run: { status: "running" }, active_nodes: activeNodes }).detail,
    "翻訳の準拠修復中",
  );
  assert.equal(
    progressUnitLabel(
      { kind: "translation_batches", phase: "draft_translation" },
      activeNodes,
    ),
    "翻訳準拠修復バッチ",
  );
});

test("ordinary translation keeps the draft batch label", () => {
  assert.equal(
    progressUnitLabel(
      { kind: "translation_batches", phase: "draft_translation" },
      [{ node_id: "draft_translation", status: "running" }],
    ),
    "翻訳バッチ",
  );
});

test("the manuscript pre-save gate uses the parallelization-map label", () => {
  const execution = runExecutionState({
    run: { status: "running" },
    active_nodes: [{
      node_id: "japanese_surface_quality_gate",
      status: "running",
      resource_lane: "ingestion_cpu",
    }],
  });

  assert.equal(execution.detail, "表層品質ゲート中");
});

test("a conditionally completed manuscript run is not labeled as a full success", () => {
  const execution = runExecutionState({
    run: {
      status: "completed",
      output: { completion_status: "conditional_success" },
    },
    active_nodes: [],
  });

  assert.equal(execution.statusLabel, "条件付き成功");
  assert.equal(execution.detail, "追加研究待ちの改稿案を保存済み");
});

test("semantic paragraph and predicate phases expose distinct progress labels", () => {
  assert.equal(
    runExecutionState({
      run: { status: "running" },
      active_nodes: [{
        node_id: "plan_semantic_paragraphs",
        status: "running",
        resource_lane: "codex_remote",
      }],
    }).detail,
    "意味段落計画中",
  );
  assert.equal(
    progressUnitLabel({ kind: "manuscript_paragraph_plan_passes" }),
    "意味段落計画・パス",
  );
  assert.equal(
    progressUnitLabel({ kind: "manuscript_predicate_candidates" }),
    "述語関係解決・候補バッチ",
  );
});

test("structure planning exposes its current isolated repair stage", () => {
  assert.equal(
    progressUnitLabel({
      kind: "manuscript_structure_plan_passes",
      stage: "temporal",
      repair_mode: true,
    }),
    "構造計画：年代言及・段落時間範囲（差分修復）",
  );
});

test("argument planning exposes each checkpointed process", () => {
  assert.equal(argumentProcessLabel("block_roles"), "ブロック役割判定");
  assert.equal(
    progressUnitLabel({
      kind: "manuscript_argument_processes",
      process_name: "claim_evidence_links",
      repair_mode: true,
    }),
    "構造計画：主張―根拠リンク生成（未解決項目のみ再試行）",
  );
});

test("ふかみる uses every parallelization-map node name verbatim", () => {
  const mapLabels = {
    prepare_rewrite_context: "書換え準備",
    extract_manuscript_references: "文献抽出",
    review_manuscript_reasoning: "論理レビュー",
    plan_manuscript_structure: "構造計画",
    resolve_manuscript_evidence: "証拠解決",
    resolve_reference_metadata: "文献メタデータ",
    resolve_enumeration_context: "列挙文脈解決",
    plan_semantic_paragraphs: "意味段落計画",
    draft_rewrite: "書換えドラフト",
    resolve_manuscript_predicate_relations: "述語関係解決",
    validate_rewrite_coherence: "書換え一貫性",
    detect_japanese_polish_targets: "研磨対象検出",
    polish_manuscript_japanese: "日本語研磨",
    select_japanese_polish_candidates: "研磨候補選択",
    japanese_surface_quality_gate: "表層品質ゲート",
    validate_final_coherence: "最終一貫性",
    render_manuscript_references: "参考文献整形",
    validate_reference_quality: "文献品質検証",
    persist_rewrite_proposal: "提案を保存",
  };

  Object.entries(mapLabels).forEach(([nodeId, label]) => {
    assert.equal(runNodeLabel(nodeId), label, nodeId);
  });
});

test("server workflow metadata overrides the legacy label and current_nodes drives failures", () => {
  const item = {
    active_nodes: [],
    current_nodes: [{
      node_id: "new_runtime_node",
      function_name_ja: "新しい実行機能",
      status: "failed",
    }],
  };
  assert.equal(runDisplayNodes(item)[0].node_id, "new_runtime_node");
  assert.equal(runNodeLabel(runDisplayNodes(item)[0]), "新しい実行機能");
  assert.equal(runExecutionState({ run: { status: "failed" }, ...item }).detail, "新しい実行機能");
});

test("patch processing labels cover missing, non-applicable, running, empty, and failed states", () => {
  assert.equal(patchProcessingState(null).label, "状態なし");
  assert.equal(
    patchProcessingState({ applicable: false, state: "not_applicable" }).label,
    "パッチ非対象",
  );
  const running = patchProcessingState({
    applicable: true,
    state: "running",
    stage: "candidates_preprocessed",
    total: 6,
    selected: 1,
    applied: 0,
    status_counts: { clean: 4, invalid: 2 },
  });
  assert.equal(running.label, "処理中");
  assert.match(running.detail, /候補を前処理/);
  assert.match(running.detail, /安全 4件/);
  assert.equal(
    patchProcessingState({ applicable: true, state: "completed", empty: true }).label,
    "完了・対象なし",
  );
  assert.equal(
    patchProcessingState({ applicable: true, state: "failed" }).label,
    "失敗",
  );
});

test("V29 map labels match the Run display for every node", () => {
  const source = new URL("../../docs/diagrams/manuscript-rewrite.workflow.json", import.meta.url);
  const diagram = JSON.parse(readFileSync(source, "utf8"));
  assert.equal(diagram.meta.workflowVersion, "29");
  assert.equal(diagram.nodes.length, 33);
  for (const node of diagram.nodes) {
    assert.equal(runNodeLabel(node.id), node.label, node.id);
  }
});

test("manuscript progress units retain their parallelization-map node names", () => {
  assert.equal(
    progressUnitLabel({ kind: "manuscript_enumeration_batches" }),
    "列挙文脈解決・バッチ",
  );
  assert.equal(
    progressUnitLabel({ kind: "manuscript_polish_targets" }),
    "研磨対象検出・対象",
  );
  assert.equal(
    progressUnitLabel({ kind: "manuscript_polish_candidates" }),
    "日本語研磨・候補生成",
  );
  assert.equal(
    progressUnitLabel({ kind: "manuscript_polish_selection" }),
    "研磨候補選択・対象",
  );
});

test("a failed Run does not expose its last percentage as live progress", () => {
  const item = {
    run: { status: "failed" },
    progress: { percentage: 33.3 },
  };

  assert.equal(runHasLiveProgress(item), false);
  assert.equal(runHasLiveProgress({ run: { status: "running" } }), true);
});

test("a coherence failure explains its stop position and blocking routes", () => {
  const item = {
    run: {
      status: "failed",
      error: { type: "CoherenceResolutionRequired" },
    },
    active_nodes: [],
  };
  const terminal = terminalRunProgressState(item, {
    kind: "manuscript_coherence_passes",
    completed: 1,
    total: 3,
    percentage: 33.3,
    blocking_route_counts: {
      human_review: 1,
      bounded_rewrite: 1,
      claim_limitation: 2,
    },
  });

  assert.equal(terminal.title, "Runは失敗しました");
  assert.equal(terminal.stoppedAt, "書換え一貫性・検証パス 1 / 3（33.3%）で停止");
  assert.equal(
    terminal.reason,
    "未解決の整合性問題 4件：人間レビュー 1件 · 限定範囲の再改稿 1件 · 主張の限定 2件",
  );
});

test("a conditional success is not presented as an active partial Run", () => {
  const terminal = terminalRunProgressState({
    run: {
      status: "completed",
      output: { completion_status: "conditional_success" },
    },
  }, {
    kind: "manuscript_coherence_passes",
    completed: 1,
    total: 3,
    percentage: 33.3,
  });

  assert.equal(terminal.status, "conditional_success");
  assert.equal(terminal.title, "条件付き成功として保存しました");
  assert.equal(
    terminal.stoppedAt,
    "書換え一貫性・検証パス 1 / 3（33.3%）で条件付き終了",
  );
});

test("OCR language waits are distinguished from manuscript review", () => {
  const execution = runExecutionState({
    run: { status: "waiting_human" },
    active_nodes: [{
      node_id: "run_ocr",
      status: "waiting_human",
      output: { ocr_wait_kind: "ocr_language" },
    }],
  });

  assert.equal(execution.statusLabel, "OCR言語確認待ち");
  assert.equal(execution.detail, "言語別OCR実行：ja / en / frを指定");
  assert.equal(runNodeLabel("ingest_ocr_outputs"), "OCR済み資料の取り込み");
});
