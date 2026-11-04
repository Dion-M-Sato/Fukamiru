export function corpusPreflightProgress(events, node, estimate, runId, now = Date.now()) {
  const start = Date.parse(node?.started_at || "");
  const points = events.filter((event) =>
    (event.type || event.event_type) === "corpus.index.progress"
    && event.payload?.phase === "preflight_retrieval_groups"
    && (!Number.isFinite(start) || Date.parse(event.created_at) >= start)
  ).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  if (!points.length) return null;
  const latest = points.at(-1);
  const completed = Number(latest.payload.checked_groups || 0);
  const end = Date.parse(latest.created_at);
  const first = points.find((point) => end - Date.parse(point.created_at) <= 600000) || points[0];
  const seconds = (end - Date.parse(first.created_at)) / 1000;
  const delta = completed - Number(first.payload.checked_groups || 0);
  const rate = seconds >= 30 && delta >= 0 ? delta / seconds : null;
  const validEstimate = estimate?.run_id === runId && estimate.estimated_groups > completed;
  const total = validEstimate ? estimate.estimated_groups : null;
  const low = validEstimate ? estimate.estimated_groups_low : null;
  const high = validEstimate ? estimate.estimated_groups_high : null;
  return {
    completed, rate, measuredSeconds: seconds,
    elapsedSeconds: Number.isFinite(start) ? Math.max(0, (now - start) / 1000) : null,
    total, low, high,
    percent: total ? Math.min(99.9, 100 * completed / total) : null,
    etaLow: rate > 0 && low ? Math.max(0, (low - completed) / rate) : null,
    etaHigh: rate > 0 && high ? Math.max(0, (high - completed) / rate) : null,
    sampleDocuments: validEstimate ? estimate.sample_documents : 0,
  };
}

export function corpusDuration(seconds) {
  if (!Number.isFinite(seconds)) return "計測中";
  const minutes = Math.ceil(Math.max(0, seconds) / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}時間${minutes % 60}分` : `${minutes}分`;
}
