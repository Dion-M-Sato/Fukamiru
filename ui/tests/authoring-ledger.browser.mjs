// Isolated visual review: no production API, proposal, or Run is changed.
// Run with Playwright available through NODE_PATH.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");
const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = await fs.mkdtemp(path.join(os.tmpdir(), "fukamiru-authoring-ledger-"));
const ledger = {
  claims: [{
    claim_id: "c1",
    claim: "地域資料は限定的な変化を示す",
    ai_decision: {
      disposition: "narrow",
      reason: "対象地域に限って支持される",
      counterinterpretation: "広域の変化とは限らない",
    },
    counterinterpretations: [],
    warrants: [{ text: "記録の年代と地点を照合した" }],
    source_passages: [{
      source_id: "s1", title: "地域史料集", authors: ["山田研究会"],
      source_url: "https://example.org/source", locator: "第2章 p. 18",
      bibliographic_verification_status: "verified",
      source_verification_status: "verified",
      excerpt: "調査地点で変化が記録された。",
    }],
  }],
};
const review = {
  id: "review-1", run_id: "run-1", object_id: "artifact-1",
  object_type: "authoring_submission", status: "open", question: "提出原稿を確認",
  source_title: "地域史研究", project_name: "研究プロジェクト",
};
const evidenceReview = { review_items: [
  {
    review_item_id: "HR-s04", section_id: "s04", claim_id: "c0301",
    category: "reader_brief_marginal_decision", status: "open",
    reason: "労働表現を男性向けに限定した判断を確認する。",
    recommended_check: "原文の範囲と本文を照合する。", source_ids: ["source-1"],
  },
  {
    review_item_id: "HR-s05", section_id: "s05", category: "reader_editorial_exclusion",
    reason: "編集判断を本文から除外した。", recommended_check: "本文に残っていないか確認する。",
  },
] };
const auditReview = {
  id: "review-audit", run_id: "run-1", object_id: "plan-1",
  object_type: "authoring_plan", status: "open", question: "構造監査を確認",
  source_title: "地域史研究", project_name: "研究プロジェクト", audit_attempt: 3,
  audit_findings: [{
    severity: "major", code: "missing_evidence", object_id: "sec-03",
    section_title: "資料の範囲", message: "第3節の根拠資料を確認する",
  }],
};
const posts = [];
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(await fs.readFile(path.join(ui, "index.html")));
    } else if (url.pathname === "/src/app.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      const source = await fs.readFile(path.join(ui, "src/app.js"), "utf8");
      response.end(source.replace(/\nboot\(\);\s*$/, '\nstate.activity = "review"; renderReviewEditor();'));
    } else if (url.pathname === "/src/api.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      response.end(`export async function api(url, options) {
        globalThis.reviewRequests ??= [];
        globalThis.reviewRequests.push(url);
        if (options?.method === "POST") return fetch("/capture", options).then(r => r.json());
        if (url === "/api/reviews?status=open") return ${JSON.stringify([auditReview, review])};
        if (url === "/api/reviews?status=approved" || url === "/api/reviews?status=rejected") {
          globalThis.reviewHistoryRequests ??= [];
          globalThis.reviewHistoryRequests.push(url);
          return url.endsWith("rejected") ? [{ id: "old-plan", run_id: "old-run", object_id: "old-plan", object_type: "authoring_plan", status: "rejected", question: "以前の構造監査" }] : [];
        }
        if (url === "/api/defects") return [];
        if (url.endsWith("/argument-ledger")) return ${JSON.stringify(ledger)};
        if (url.endsWith("/evidence-review")) return ${JSON.stringify(evidenceReview)};
        throw new Error("Unexpected API request: " + url);
      }`);
    } else if (url.pathname === "/capture") {
      let body = "";
      for await (const chunk of request) body += chunk;
      posts.push(JSON.parse(body));
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ status: "failed" }));
    } else if (url.pathname.startsWith("/src/")) {
      const file = path.join(ui, url.pathname.slice(1));
      response.setHeader("Content-Type", file.endsWith(".css") ? "text/css" : "text/javascript");
      response.end(await fs.readFile(file));
    } else {
      response.writeHead(404);
      response.end();
    }
  } catch (error) {
    response.writeHead(500);
    response.end(String(error));
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true, channel: "msedge" });
try {
  for (const width of [1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 950 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.querySelector("#sidebar-title")?.textContent === "レビュー待ち一覧");
    assert.equal(await page.locator('[data-activity="review"]').getAttribute("aria-label"), "レビュー待ち一覧");
    assert.equal(await page.locator("#tab-title").innerText(), "レビュー待ち一覧");
    assert.equal(await page.locator("#sidebar-title").innerText(), "レビュー待ち一覧");
    assert.equal(await page.locator("#editor-content h1").innerText(), "レビュー待ち一覧");
    assert.equal(await page.locator('[data-review-history="approved"]').getAttribute("open"), null);
    assert.equal(await page.locator('[data-review-history="rejected"]').getAttribute("open"), null);
    assert.deepEqual(await page.evaluate(() => globalThis.reviewHistoryRequests ?? []), []);
    assert.equal(await page.locator('[data-review-defects]').getAttribute("open"), null);
    assert.equal(await page.evaluate(() => globalThis.reviewRequests.includes("/api/defects")), false);
    await page.locator('[data-review-history="rejected"] > summary').click();
    assert.match(await page.locator('[data-review-history="rejected"] .review-history-entry').innerText(), /以前の構造監査/);
    assert.deepEqual(await page.evaluate(() => globalThis.reviewHistoryRequests), ["/api/reviews?status=rejected"]);
    await page.locator('[data-review-defects] > summary').click();
    assert.match(await page.locator('[data-review-defects] .empty-state').innerText(), /未解決のDefectはありません/);
    assert.equal(await page.evaluate(() => globalThis.reviewRequests.filter((url) => url === "/api/defects").length), 1);
    assert.match(await page.locator(".review-audit-target").innerText(), /対象原稿: 地域史研究/);
    assert.match(await page.locator(".review-audit-details").innerText(), /資料の範囲/);
    assert.match(await page.locator(".review-audit-details").innerText(), /第3節の根拠資料を確認する/);
    assert.equal(await page.locator(".human-review-entry").count(), 1);
    assert.equal(await page.locator(".review-card").getByText("労働表現を男性向けに限定した判断を確認する。").count(), 0);
    await page.locator(".human-review-open").click();
    assert.equal(await page.locator("#human-review-back").innerText(), "レビュー待ち一覧へ戻る");
    assert.equal(await page.locator(".human-review-row").count(), 2);
    assert.match(await page.locator(".human-review-group").first().innerText(), /s04/);
    await page.locator(".human-review-row").first().click();
    assert.equal(await page.locator("#human-review-modal").getAttribute("open"), "");
    assert.match(await page.locator("#human-review-modal-reason").innerText(), /男性向けに限定/);
    assert.match(await page.locator("#human-review-modal-check").innerText(), /原文の範囲/);
    await page.screenshot({ path: path.join(output, `human-review-modal-${width}.png`), fullPage: true });
    await page.locator("#human-review-modal-close").click();
    assert.equal(await page.locator("#human-review-modal").getAttribute("open"), null);
    const downloadPromise = page.waitForEvent("download");
    await page.locator("#human-review-download").click();
    const download = await downloadPromise;
    assert.equal(download.suggestedFilename(), "human-review-run-1.md");
    const markdownFile = path.join(output, `human-review-${width}.md`);
    await download.saveAs(markdownFile);
    const markdown = await fs.readFile(markdownFile, "utf8");
    assert.match(markdown, /HR-s04/);
    assert.match(markdown, /労働表現を男性向けに限定した判断を確認する/);
    assert.match(markdown, /HR-s05/);
    await page.screenshot({ path: path.join(output, `human-review-${width}.png`), fullPage: true });
    await page.locator("#human-review-back").click();
    assert.equal(await page.locator("#editor-content h1").innerText(), "レビュー待ち一覧");
    await page.screenshot({ path: path.join(output, `audit-${width}.png`), fullPage: true });
    await page.locator(".argument-ledger > summary").click();
    await page.locator(".argument-ledger-claim > summary").click();
    assert.match(await page.locator(".argument-ledger").innerText(), /地域史料集/);
    assert.match(await page.locator(".argument-ledger").innerText(), /広域の変化とは限らない/);
    assert.equal(await page.locator(".argument-ledger-source a").getAttribute("href"), "https://example.org/source");
    assert.equal(await page.locator(".argument-ledger").evaluate(
      (element) => element.scrollWidth > element.clientWidth + 2,
    ), false);
    await page.screenshot({ path: path.join(output, `review-${width}.png`), fullPage: true });
    await page.locator('[data-review-claim="review-1"]').selectOption("c1");
    await page.locator('[data-review-feedback="review-1"]').fill("この資料では支持できない");
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/capture")),
      page.locator('[data-review="review-1"][data-decision="reject"]').click(),
    ]);
    assert.deepEqual(posts.at(-1), {
      decision: "reject", feedback: "この資料では支持できない", claim_id: "c1",
      corrected_text: null,
    });
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log(`Authoring ledger browser passed; screenshots: ${output}`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
