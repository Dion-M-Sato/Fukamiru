const ENGINE_FINGERPRINT_PATTERN = /^[0-9a-f]{64}$/;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[character]);
}

function runRecord(item) {
  return item?.run || item || {};
}

function workflowVersion(run) {
  const parsed = Number.parseInt(String(run.workflow_version || ""), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * Classify only the durable V22 external-service waits that the operator can
 * resolve through the dedicated resume endpoint. Ordinary human reviews stay
 * outside this state machine.
 */
export function externalRunWaitState(item) {
  const run = runRecord(item);
  if (
    run.status !== "waiting_human"
    || !((run.workflow_name === "manuscript_rewrite" && workflowVersion(run) >= 22)
      || (["manuscript_generator", "authoring"].includes(run.workflow_name)
        && workflowVersion(run) >= 31))
  ) {
    return null;
  }

  const error = run.error && typeof run.error === "object" ? run.error : {};
  if (error.type !== "ExternalServiceUnavailable") return null;
  const activeNodes = Array.isArray(item?.active_nodes) ? item.active_nodes : [];
  const snapshotHasNodeErrors = activeNodes.some((node) =>
    Object.hasOwn(node, "error") || Object.hasOwn(node, "error_json"));
  if (snapshotHasNodeErrors) {
    const externallyBlocked = activeNodes.filter((node) => {
      const nodeError = node.error && typeof node.error === "object" ? node.error : {};
      return node.status === "waiting_human"
        && nodeError.type === "ExternalServiceUnavailable";
    });
    if (activeNodes.length !== 1 || externallyBlocked.length !== 1) return null;
    if (Array.isArray(item?.reviews) && item.reviews.some((review) => review.status === "open")) {
      return null;
    }
  }
  const reason = String(error.reason || "");
  if (reason === "literature_search_deferred" && workflowVersion(run) >= 32) {
    return {
      kind: "outage", reason,
      statusLabel: "文献DB復旧待ち",
      title: "文献DBの検索を完了できません",
      detail: "完了済みの検索結果は保存されています。DBの利用制限や復旧を確認して再開すると、未完了の検索だけを実行します。",
      targetFingerprint: null, previousFingerprint: null, canResume: true,
    };
  }
  if (reason === "external_service_unavailable") {
    return {
      kind: "outage",
      reason,
      statusLabel: "外部Codex復旧待ち",
      title: "外部Codexへ接続できません",
      detail: "復旧を確認して手動で再開するか、このRunを中止してください。自動的に別の推論エンジンへは切り替えません。",
      targetFingerprint: null,
      previousFingerprint: null,
      canResume: true,
    };
  }
  if (reason !== "inference_engine_authorization_required") return null;

  const targetFingerprint = String(error.target_engine_fingerprint || "");
  const previousFingerprint = String(error.previous_engine_fingerprint || "");
  const validTarget = ENGINE_FINGERPRINT_PATTERN.test(targetFingerprint);
  return {
    kind: "engine_authorization",
    reason,
    statusLabel: "推論エンジン変更の承認待ち",
    title: "推論エンジンが変更されています",
    detail: validTarget
      ? "下記の新しいfingerprintを確認してください。承認した場合だけ、その完全一致値を送ってRunを再開します。"
      : "承認対象のfingerprintを確認できないため再開できません。Eventsで待機記録を確認するか、このRunを中止してください。",
    targetFingerprint: validTarget ? targetFingerprint : null,
    previousFingerprint: ENGINE_FINGERPRINT_PATTERN.test(previousFingerprint)
      ? previousFingerprint
      : null,
    canResume: validTarget,
  };
}

/** Build the exact request for the wait type. Outage resumes have no body. */
export function externalResumeRequestOptions(waitState) {
  if (!waitState?.canResume) return null;
  if (waitState.kind === "outage") return { method: "POST" };
  if (
    waitState.kind === "engine_authorization"
    && ENGINE_FINGERPRINT_PATTERN.test(String(waitState.targetFingerprint || ""))
  ) {
    return {
      method: "POST",
      body: JSON.stringify({
        authorized_engine_fingerprint: waitState.targetFingerprint,
      }),
    };
  }
  return null;
}

export function externalWaitMarkup(run, waitState) {
  const target = waitState.targetFingerprint
    ? `<div><dt>承認対象</dt><dd><code>${escapeHtml(waitState.targetFingerprint)}</code></dd></div>`
    : "";
  const previous = waitState.previousFingerprint
    ? `<div><dt>直前のエンジン</dt><dd><code>${escapeHtml(waitState.previousFingerprint)}</code></dd></div>`
    : "";
  const resumeLabel = waitState.kind === "engine_authorization"
    ? "このfingerprintを承認して再開"
    : "外部Codexを再試行";
  return `<section class="run-external-wait" data-wait-kind="${escapeHtml(waitState.kind)}" aria-label="${escapeHtml(waitState.statusLabel)}">
    <div class="run-external-wait-heading"><span aria-hidden="true">!</span><strong>${escapeHtml(waitState.title)}</strong></div>
    <p>${escapeHtml(waitState.detail)}</p>
    ${target || previous ? `<dl class="run-engine-fingerprints">${previous}${target}</dl>` : ""}
    <div class="run-external-actions">
      <button type="button" class="secondary-action run-external-resume"
        data-run-id="${escapeHtml(run.id)}"
        ${waitState.targetFingerprint ? `data-engine-fingerprint="${escapeHtml(waitState.targetFingerprint)}"` : ""}
        ${waitState.canResume ? "" : "disabled"}>${escapeHtml(resumeLabel)}</button>
      <button type="button" class="danger-action run-external-cancel" data-run-id="${escapeHtml(run.id)}">このRunを中止</button>
    </div>
    <p class="run-external-action-status" role="status" aria-live="polite"></p>
  </section>`;
}
