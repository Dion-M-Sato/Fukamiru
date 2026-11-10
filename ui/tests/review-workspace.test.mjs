import assert from "node:assert/strict";
import test from "node:test";
import { ReviewDrafts } from "../src/review-workspace.js";

test("per-review answers survive switching and an edit during an in-flight save", async () => {
  const drafts = new ReviewDrafts();
  const make = (id) => ({ review_id: id, revision: 0, answers: [], overall_comment: "" });
  drafts.load(make("a")); drafts.load(make("b"));
  drafts.edit("a", (packet) => { packet.answers.push({ question_id: "q", answer: "", llm_overreach: true }); });
  let release;
  const calls = [];
  const saving = drafts.save("a", async (body) => {
    calls.push(body);
    if (calls.length === 1) await new Promise((resolve) => { release = resolve; });
    return { revision: calls.length, submitted_revision: null };
  });
  drafts.edit("a", (packet) => { packet.overall_comment = "追記"; });
  drafts.edit("b", (packet) => { packet.overall_comment = "別の原稿"; });
  release(); await saving;
  assert.equal(calls.length, 2);
  assert.equal(calls[1].overall_comment, "追記");
  assert.equal(calls[1].revision, 1);
  assert.equal(calls[0].answers[0].llm_overreach, true);
  assert.equal(drafts.entries.get("b").packet.overall_comment, "別の原稿");
  assert.equal(drafts.load(make("a")).packet.overall_comment, "追記");
});

test("a rejected save retains local input for retry", async () => {
  const drafts = new ReviewDrafts();
  drafts.load({ review_id: "a", revision: 0, answers: [], overall_comment: "記録" });
  await assert.rejects(drafts.save("a", async () => { throw new Error("offline"); }));
  assert.equal(drafts.entries.get("a").packet.overall_comment, "記録");
  assert.equal(drafts.entries.get("a").packet.revision, 0);
});
