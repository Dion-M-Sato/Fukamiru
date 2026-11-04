import assert from "node:assert/strict";
import test from "node:test";

import {
  groupHumanReviewItems,
  humanReviewCategoryLabel,
  humanReviewMarkdown,
} from "../src/human-review.js";

const items = [
  {
    review_item_id: "HR-1", section_id: "s04", claim_id: "c0301",
    category: "reader_brief_marginal_decision", status: "open",
    reason: "原文より対象が狭い。\n男性向けに限定している。",
    recommended_check: "原稿と出典を照合する。", source_ids: ["src-1"],
  },
  {
    review_item_id: "HR-2", section_id: "s05", category: "other",
    reason: "引用を確認する。", source_ids: [],
  },
  {
    review_item_id: "HR-3", section_id: "s04", category: "reader_brief_audit_disagreement",
    reason: "監査の判定が違う。", source_ids: [],
  },
];

test("human review groups items by section without dropping original order", () => {
  const groups = groupHumanReviewItems(items);
  assert.deepEqual(groups.map((group) => [group.sectionId, group.items.length]), [
    ["s04", 2], ["s05", 1],
  ]);
  assert.deepEqual(groups[0].items.map((item) => item.review_item_id), ["HR-1", "HR-3"]);
  assert.equal(humanReviewCategoryLabel("reader_brief_marginal_decision"), "意味に疑義がある採用表現");
});

test("markdown export includes every item, reason, source, and recommended check", () => {
  const markdown = humanReviewMarkdown({ title: "試行原稿", runId: "run-1", items });
  assert.match(markdown, /^# 人間確認リスト\n/);
  assert.match(markdown, /原稿: 試行原稿/);
  assert.match(markdown, /項目数: 3件/);
  assert.match(markdown, /## s04/);
  assert.match(markdown, /HR-1/);
  assert.match(markdown, /src-1/);
  assert.match(markdown, /> 原文より対象が狭い。\n> 男性向けに限定している。/);
  assert.match(markdown, /原稿と出典を照合する。/);
  assert.match(markdown, /HR-2/);
  assert.match(markdown, /HR-3/);
});
