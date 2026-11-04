const CATEGORY_LABELS = {
  reader_brief_marginal_inference: "推論として採用した表現",
  reader_brief_marginal_decision: "意味に疑義がある採用表現",
  reader_brief_audit_disagreement: "監査が食い違った採用表現",
  reader_editorial_exclusion: "本文から分離した編集判断",
};

export function humanReviewCategoryLabel(category) {
  return CATEGORY_LABELS[category] || category || "確認項目";
}

export function humanReviewItemLabel(item) {
  return item.sentence_id || item.claim_id || item.item_id || item.section_id || "原稿全体";
}

export function groupHumanReviewItems(items) {
  const sections = new Map();
  for (const item of Array.isArray(items) ? items : []) {
    const sectionId = item.section_id || "原稿全体";
    if (!sections.has(sectionId)) sections.set(sectionId, []);
    sections.get(sectionId).push(item);
  }
  return [...sections].map(([sectionId, sectionItems]) => ({ sectionId, items: sectionItems }));
}

function markdownText(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function markdownQuote(value) {
  return markdownText(value).split(/\r?\n/).map((line) => `> ${line}`).join("\n");
}

export function humanReviewMarkdown({ title, runId, items }) {
  const rows = Array.isArray(items) ? items : [];
  const lines = [
    "# 人間確認リスト", "",
    `原稿: ${markdownText(title || "名称未記録")}`,
    `Run: ${markdownText(runId || "未記録")}`,
    `項目数: ${rows.length}件`, "",
  ];
  for (const group of groupHumanReviewItems(rows)) {
    lines.push(`## ${markdownText(group.sectionId)}`, "");
    group.items.forEach((item, index) => {
      lines.push(`### ${index + 1}. ${markdownText(humanReviewItemLabel(item))} — ${markdownText(humanReviewCategoryLabel(item.category))}`, "");
      lines.push(`- 状態: ${markdownText(item.status || "open")}`);
      lines.push(`- 確認項目ID: ${markdownText(item.review_item_id || item.id || "未記録")}`);
      lines.push(`- 関連する出典ID: ${markdownText((item.source_ids || []).join("、") || "記録なし")}`, "");
      lines.push("**確認理由**", "", markdownQuote(item.reason || "確認理由未記録"), "");
      lines.push("**推奨確認**", "", markdownQuote(item.recommended_check || "原典と本文の対応を確認"), "");
    });
  }
  return lines.join("\n").trimEnd() + "\n";
}
