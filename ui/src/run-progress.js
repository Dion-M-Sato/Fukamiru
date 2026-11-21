import { corpusDuration, corpusPreflightProgress } from "./corpus-progress.js?v=source";

const count = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
const units = { pages: "ページ", page: "ページ", chunks: "チャンク", groups: "グループ",
  source_spans: "Span", files: "ファイル", documents: "資料", items: "件", candidate: "候補" };

// The polled snapshot reads persisted SQLite events. Do not infer Run completion
// from an SSE notification, or reuse a counter from a different node attempt.
export function runMeasuredProgress(item, fallback = item?.progress, events = item?.events || [], now = Date.now()) {
  const run = item?.run || item;
  if (!["running", "waiting_human"].includes(run?.status)) return fallback;
  const node = (item.current_nodes || item.active_nodes || []).find((candidate) =>
    candidate.node_id === "build_semantic_index" && candidate.status === "running");
  const start = Date.parse(node?.started_at || "");
  if (!node || !Number.isFinite(start)) return fallback;
  const points = events.filter((event) =>
    (event.type || event.event_type) === "corpus.index.progress"
    && (!event.run_id || event.run_id === run.id)
    && Date.parse(event.created_at) >= start
    && (event.payload?.phase === "preflight_retrieval_groups"
      ? count(event.payload.checked_groups) : count(event.payload?.embedded_count))
  ).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const latest = points.at(-1);
  if (!latest) return fallback;
  const payload = latest.payload || {};
  const elapsed = Math.max(0, (now - start) / 1000);
  if (payload.phase === "preflight_retrieval_groups") {
    if (!count(payload.checked_groups)) return fallback;
    const preflight = corpusPreflightProgress(points.filter((event) =>
      count(event.payload?.checked_groups)), node, null, run.id, now);
    return { kind: "semantic_index_activity", phase: payload.phase,
      label: "意味索引の事前検査", completed: preflight.completed, total: null,
      unit: "グループ", completed_label: "確認済み", rate: preflight.rate,
      elapsed_seconds: elapsed, updated_at: latest.created_at };
  }
  if (!count(payload.embedded_count)) return fallback;
  return { kind: "semantic_index_activity", phase: "embedding", label: "意味索引の埋め込み",
    completed: payload.embedded_count, total: count(payload.total_count) && payload.total_count > 0
      ? payload.total_count : null,
    unit: payload.index_type ? "グループ" : "チャンク", rate: payload.chunks_per_second,
    eta_seconds: payload.eta_seconds, elapsed_seconds: elapsed, updated_at: latest.created_at };
}

export function numericProgressSummary(progress, label = "処理") {
  if (!count(progress?.completed)) return null;
  const completed = progress.completed;
  const total = count(progress.total) ? progress.total : null;
  const unit = units[progress.unit] || progress.unit || "件";
  const title = progress.label || label;
  const amount = total === null ? completed.toLocaleString("ja-JP")
    : `${completed.toLocaleString("ja-JP")} / ${total.toLocaleString("ja-JP")}`;
  const details = [];
  if (total === null) details.push("総数未確定");
  if (count(progress.rate) && progress.rate > 0) {
    details.push(`${Math.round(progress.rate * 60).toLocaleString("ja-JP")}${unit}/分`);
  }
  if (count(progress.elapsed_seconds)) details.push(`経過${corpusDuration(progress.elapsed_seconds)}`);
  if (total > 0 && count(progress.eta_seconds)) details.push(`残り目安${corpusDuration(progress.eta_seconds)}`);
  if (count(progress.completed_characters)) {
    details.push(`翻訳済み ${progress.completed_characters.toLocaleString("ja-JP")}字`);
  }
  return { title, headline: `${title}：${amount}${unit}${progress.completed_label || "完了"}`,
    percentage: total > 0 ? Math.min(100, Math.max(0, Math.round(completed / total * 1000) / 10)) : null,
    details: details.join(" · ") };
}
