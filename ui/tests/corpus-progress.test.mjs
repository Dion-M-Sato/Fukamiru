import test from "node:test";
import assert from "node:assert/strict";
import { corpusPreflightProgress } from "../src/corpus-progress.js";

const node = { started_at: "2026-10-26T08:00:00Z" };
const event = (minute, count, phase = "preflight_retrieval_groups") => ({
  type: "corpus.index.progress", created_at: `2026-10-26T08:${minute}:00Z`,
  payload: { phase, checked_groups: count, embedded_count: 0, total_count: 0 },
});

test("preflight measures groups and speed without inventing embedding completion", () => {
  const value = corpusPreflightProgress([event("01", 100), event("03", 340)], node, null, "run");
  assert.equal(value.completed, 340);
  assert.equal(value.rate, 2);
  assert.equal(value.percent, null);
  assert.equal(value.etaHigh, null);
});

test("sample totals produce explicitly estimated percent and stage ETA range", () => {
  const estimate = { run_id: "run", estimated_groups: 1000, estimated_groups_low: 800,
    estimated_groups_high: 1200, sample_documents: 12 };
  const value = corpusPreflightProgress([event("01", 100), event("03", 340)], node, estimate, "run");
  assert.equal(value.percent, 34);
  assert.equal(value.etaLow, 230);
  assert.equal(value.etaHigh, 430);
  assert.equal(value.sampleDocuments, 12);
});

test("another Run's estimate and prior-attempt points cannot contaminate progress", () => {
  const value = corpusPreflightProgress([
    { ...event("01", 9999), created_at: "2026-10-25T08:00:00Z" },
    event("01", 100), event("03", 340),
  ], node, { run_id: "other", estimated_groups: 1000 }, "run");
  assert.equal(value.rate, 2);
  assert.equal(value.total, null);
});

test("exceeding a sample estimate never displays premature 100 percent", () => {
  const value = corpusPreflightProgress([event("01", 100), event("03", 340)], node,
    { run_id: "run", estimated_groups: 300 }, "run");
  assert.equal(value.percent, null);
  assert.equal(value.etaHigh, null);
});
