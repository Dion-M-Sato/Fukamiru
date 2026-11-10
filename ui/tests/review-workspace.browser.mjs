// Isolated UI regression. No production Run, document or author decision is changed.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const { chromium } = createRequire(import.meta.url)("playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.resolve(root, "../output/verification-review-ui");
await fs.mkdir(output, { recursive: true });
const reviews = ["a", "b"].map((id, index) => ({ id, run_id: `run-${id}`,
  object_id: `artifact-${id}`, object_type: "authoring_submission", status: "open",
  question: "原稿を確認できますか？", source_title: index ? "別の原稿" : "仮想展示の構成（UI検証）" }));
const packets = new Map(reviews.map((review) => [review.id, {
  review_id: review.id, run_id: review.run_id, status: "open", revision: 0,
  questions: [{ question_id: review.id === "b" ? 'q-b" onfocus="window.bad=true' : "q-a", target: "仮想展示", source_ids: [],
    question: "展示の範囲について、補足できる資料や情報はありますか？", reason: "対象の範囲を確認する", excerpt: "仮想展示の配置を比較する。".repeat(review.id === "a" ? 30 : 1),
    context_note: "展示の範囲を確認したいという指摘です。", excerpt_scope: review.id === "b" ? "planning" : "paragraph" }],
  answers: [], overall_comment: "", submitted_revision: null,
  preview_html: '<article id="paper"><h1>仮想展示</h1><p>仮想展示の配置を比較する。</p><script>window.parent.bad=true</script><img src="https://example.invalid/track"></article>',
}]));
const submissions = [];
const errors = [];
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");
  try {
    if (url.pathname === "/favicon.ico") { response.statusCode = 204; response.end(); return; }
    if (url.pathname.startsWith("/api/")) {
      response.setHeader("Content-Type", "application/json");
      let result;
      if (url.pathname === "/api/reviews") result = reviews.filter((review) => review.status === url.searchParams.get("status"));
      else if (url.pathname === "/api/review-feedback-history") result = reviews.filter((review) => packets.get(review.id).submitted_revision != null);
      else {
        const [, , , id, action] = url.pathname.split("/");
        const packet = packets.get(id);
        if (action === "feedback") {
          if (request.method === "PUT") {
            let data = ""; for await (const chunk of request) data += chunk;
            const body = JSON.parse(data);
            if (body.revision !== packet.revision) {
              response.statusCode = 409;
              response.end(JSON.stringify({ detail: "別の画面で回答が更新されました。" }));
              return;
            }
            packet.answers = body.answers; packet.overall_comment = body.overall_comment;
            packet.revision += 1;
            if (body.submit) { packet.submitted_revision = packet.revision; packet.submitted_at = "2026-11-09"; submissions.push(body); }
          }
          result = packet;
        } else if (action === "resolve") {
          const review = reviews.find((item) => item.id === id);
          review.status = "approved"; packet.status = "approved";
          result = { run_id: review.run_id, status: "completed" };
        } else result = {};
      }
      response.end(JSON.stringify(result)); return;
    }
    let filename = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    assert(!filename.includes(".."));
    let content = await fs.readFile(path.join(root, filename));
    if (filename === "src/app.js") content = Buffer.from(content.toString().replace(/\nboot\(\);\s*$/, '\nstate.activity="review"; bindPaneLayout(); renderReviewEditor();'));
    response.setHeader("Content-Type", filename.endsWith(".js") ? "text/javascript; charset=utf-8" : filename.endsWith(".css") ? "text/css" : "text/html; charset=utf-8");
    response.end(content);
  } catch (error) { errors.push(String(error)); response.statusCode = 500; response.end(String(error)); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || "msedge" });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const externalRequests = [];
  page.on("request", (request) => {
    if (/^https?:/.test(request.url()) && new URL(request.url()).hostname !== "127.0.0.1") externalRequests.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.locator("[data-question-overreach='q-a']").check();
  assert.equal(await page.locator(".review-question-context").textContent(), "展示の範囲を確認したいという指摘です。");
  assert.equal(await page.locator(".review-excerpt-label").textContent(), "対象の段落");
  assert.equal(await page.locator(".review-excerpt-detail").getAttribute("open"), null);
  await page.locator(".review-excerpt-detail summary").click();
  assert.notEqual(await page.locator(".review-excerpt-detail").getAttribute("open"), null);
  await page.locator(".review-excerpt-detail summary").click();
  await page.locator("#review-overall-comment").fill("UI検証の全体コメント");
  await page.locator("[data-review-select='b']").click();
  await page.waitForFunction(() => document.querySelector("#review-inspector [data-question-answer]")?.dataset.questionAnswer === 'q-b" onfocus="window.bad=true');
  assert.equal(await page.locator(".review-excerpt-label").textContent(), "検討対象の内容");
  await page.locator("#review-inspector [data-question-answer]").fill("別原稿の回答");
  assert.equal(await page.locator("#review-inspector [data-question-answer]").getAttribute("data-question-answer"), 'q-b" onfocus="window.bad=true');
  await page.locator("[data-review-select='a']").click();
  assert(await page.locator("[data-question-overreach='q-a']").isChecked());
  assert.equal(await page.locator("#review-overall-comment").inputValue(), "UI検証の全体コメント");
  await page.locator("#review-submit").click();
  await page.waitForFunction(() => document.getElementById("review-save-status").textContent.includes("送信しました"));
  assert(submissions.at(-1).answers[0].llm_overreach);
  assert.equal(submissions.at(-1).answers[0].answer, "");
  await page.screenshot({ path: path.join(output, "questions-desktop.png") });
  await page.reload();
  assert(await page.locator("[data-question-overreach='q-a']").isChecked());
  await page.locator("#review-overall-comment").fill("保存前の再読込でも保持するコメント");
  await page.reload();
  assert.equal(await page.locator("#review-overall-comment").inputValue(), "保存前の再読込でも保持するコメント");
  await page.locator("#review-inspector [data-question-answer]").fill("競合前の回答");
  packets.get("a").revision += 1;
  packets.get("a").overall_comment = "別の画面から保存されたコメント";
  await page.reload();
  await page.locator("#review-inspector .review-recovery").waitFor();
  assert.equal(await page.locator("#review-selection .review-recovery").count(), 0);
  assert.equal(await page.locator("#review-overall-comment").inputValue(), "別の画面から保存されたコメント");
  await page.locator(".review-recovery summary").click();
  await page.locator("#review-recover-local").click();
  assert.equal(await page.locator("#review-inspector [data-question-answer]").inputValue(), "競合前の回答");
  await page.locator("#review-html-tab").click();
  assert.equal(await page.locator("#review-html-frame").getAttribute("sandbox"), "");
  const preview = page.frameLocator("#review-html-frame");
  await preview.locator("h1").waitFor();
  assert.equal(await preview.locator("script,img").count(), 0);
  assert.equal(await page.evaluate(() => window.bad), undefined);
  assert.deepEqual(externalRequests, []);
  await page.screenshot({ path: path.join(output, "html-desktop.png") });
  await page.locator("#review-html-expand").click();
  assert(await page.locator("#review-html-modal").isVisible());
  await page.keyboard.press("Escape");
  await page.locator("#review-questions-tab").click();
  await page.locator("#review-questions-tab").press("ArrowRight");
  assert.equal(await page.locator("#review-html-tab").getAttribute("aria-selected"), "true");
  await page.locator("#review-html-tab").press("Home");
  assert.equal(await page.locator("#review-questions-tab").getAttribute("aria-selected"), "true");
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.locator("#review-inspector").isVisible());
  await page.screenshot({ path: path.join(output, "questions-mobile.png") });
  await page.locator("#review-pane-back").click();
  assert(!await page.locator("#inspector-pane").isVisible());
  assert(await page.locator("#review-overall-comment").isVisible());
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator("[data-review-select='b']").click();
  await page.waitForFunction(() => document.querySelector("#review-inspector [data-question-answer]")?.dataset.questionAnswer === 'q-b" onfocus="window.bad=true');
  assert.equal(await page.locator("#review-inspector [data-question-answer]").inputValue(), "別原稿の回答");
  assert.equal(errors.length, 0, errors.join("\n"));
  console.log("Review UI: switching, autosave, checkbox-only submission, reload, HTML isolation and mobile layout passed.");
} finally { await browser.close(); await new Promise((resolve) => server.close(resolve)); }
