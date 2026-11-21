import test from "node:test";
import assert from "node:assert/strict";
import { numericProgressSummary, runMeasuredProgress } from "../src/run-progress.js";
import { progressUnitLabel } from "../src/run-labels.js";

const start = "2026-11-17T02:24:09Z";
const item = { run: { id: "run", status: "running" },
  active_nodes: [{ node_id: "build_semantic_index", status: "running", started_at: start }] };
const point = (time, count, extra = {}) => ({ type: "corpus.index.progress", run_id: "run",
  created_at: `2026-11-17T02:${time}:00Z`, payload: {
    phase: "preflight_retrieval_groups", checked_groups: count, embedded_count: 0, total_count: 0,
  }, ...extra });

test("Run details show preflight actual count, rate and elapsed time with unknown total", () => {
  const value = runMeasuredProgress(item, null, [point("25", 100), point("27", 340)],
    Date.parse("2026-11-17T02:28:00Z"));
  const summary = numericProgressSummary(value);
  assert.equal(value.completed, 340);
  assert.equal(value.rate, 2);
  assert.equal(value.elapsed_seconds, 231);
  assert.equal(summary.headline, "意味索引の事前検査：340グループ確認済み");
  assert.equal(summary.percentage, null);
  assert.match(summary.details, /総数未確定.*120グループ\/分.*経過4分/);
});

test("index counters exclude another Run, previous attempt, and invalid observations", () => {
  const points = [point("20", 9999), point("26", 9999, { run_id: "other" }),
    point("25", 100), point("27", 340), point("28", -1)];
  assert.equal(runMeasuredProgress(item, null, points).completed, 340);
});

test("previous-attempt or another Run counters never replace current progress", () => {
  const fallback = { phase: "build_semantic_index" };
  assert.equal(runMeasuredProgress(item, fallback,
    [point("20", 9999), point("26", 9999, { run_id: "other" })]), fallback);
  assert.equal(runMeasuredProgress({ ...item, active_nodes: [{ ...item.active_nodes[0],
    node_id: "run_ocr_jobs" }] }, fallback, [point("25", 100)]), fallback);
});

test("embedding replaces preflight and shows exact completed/total groups", () => {
  const embedding = point("28", 0, { payload: { embedded_count: 40, total_count: 100,
    chunks_per_second: 2, eta_seconds: 30, index_type: "IVFPQ64x8" } });
  const value = runMeasuredProgress(item, null, [point("25", 10000), embedding]);
  const summary = numericProgressSummary(value);
  assert.equal(summary.headline, "意味索引の埋め込み：40 / 100グループ完了");
  assert.equal(summary.percentage, 40);
  assert.match(summary.details, /120グループ\/分.*残り目安1分/);
});

test("generic non-batch counts preserve units and show zero without inventing a percentage", () => {
  const progress = { completed: 0, total: null, unit: "pages" };
  assert.equal(progressUnitLabel(progress), "処理");
  assert.deepEqual(numericProgressSummary(progress), {
    title: "処理", headline: "処理：0ページ完了", percentage: null, details: "総数未確定",
  });
  assert.equal(numericProgressSummary({ completed: 1200, total: 2000, unit: "files" }).percentage, 60);
  assert.equal(numericProgressSummary({ completed: 0, total: 0 }).percentage, null);
  assert.equal(numericProgressSummary({ completed: NaN, total: 20 }), null);
  assert.equal(numericProgressSummary({ completed: -1, total: 20 }), null);
  assert.equal(numericProgressSummary({ completed: null, total: 20 }), null);
  assert.equal(numericProgressSummary({ completed: 10, total: Infinity }).percentage, null);
});

test("terminal Runs and nodes without a start time do not acquire live counters", () => {
  const fallback = { completed: null };
  assert.equal(runMeasuredProgress({ ...item, run: { ...item.run, status: "failed" } },
    fallback, [point("25", 100)]), fallback);
  assert.equal(runMeasuredProgress({ ...item, active_nodes: [{ ...item.active_nodes[0],
    started_at: null }] }, fallback, [point("25", 100)]), fallback);
});
