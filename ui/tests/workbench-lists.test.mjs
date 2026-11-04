import test from "node:test";
import assert from "node:assert/strict";
import { approvedOutputReviews, translationSourceRuns } from "../src/workbench-lists.js";

test("translation sidebar contains only source-file runs", () => {
  const runs = [
    { run: { id: "manuscript", workflow_name: "manuscript_rewrite", input: { source_path: "wrong.html" } } },
    { run: { id: "translation", workflow_name: "translation", input: { source_path: "paper.pdf" } } },
    { run: { id: "translation-older", workflow_name: "translation", input: { source_path: "paper.pdf" } } },
    { run: { id: "other", workflow_name: "translation", input: {} } },
  ];
  assert.deepEqual(translationSourceRuns(runs).map((run) => run.id), ["translation"]);
});

test("approval list contains approved manuscript and translation outputs only", () => {
  const reviews = [
    { id: "coherence", object_type: "manuscript_coherence_resolution", workflow_name: "manuscript_rewrite" },
    { id: "manuscript", object_type: "manuscript_rewrite_proposal", workflow_name: "manuscript_rewrite" },
    { id: "authoring", object_type: "authoring_submission", workflow_name: "authoring" },
    { id: "generator", object_type: "authoring_submission", workflow_name: "manuscript_generator" },
    { id: "translation", object_type: "run_artifact", workflow_name: "translation" },
    { id: "other", object_type: "run_artifact", workflow_name: "ingestion" },
  ];
  assert.deepEqual(approvedOutputReviews(reviews).map((review) => review.id), ["manuscript", "authoring", "generator", "translation"]);
});
