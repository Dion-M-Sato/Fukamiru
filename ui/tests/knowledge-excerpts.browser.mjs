// Exercises the optional UI against only the existing read-only search API.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { chromium } from "playwright";

const sourceRoot = path.resolve(import.meta.dirname, "..");
const fixtureRoot = process.env.FUKAMIRU_UI_FIXTURE_ROOT || sourceRoot;
const output = path.resolve(sourceRoot, process.env.FUKAMIRU_UI_VERIFICATION_DIR || "../output/verification-excerpts");
await fs.mkdir(output, { recursive: true });
const hits = [
  { chunk_id: "a", source_asset_id: "one", title: "観測ノートと参加者", path: "C:/fixture/book.txt", text: '原文A\n<script>window.parent.bad=true</script><img src="https://example.invalid/track">', citation: { start_page: 12, end_page: 13 } },
  { chunk_id: "b", source_asset_id: "two", title: "別の資料", text: "原文B", citation: { start_label: "三" } },
  { chunk_id: "c", source_asset_id: "one", title: "参加者の観測記録", path: "C:/fixture/book.txt", text: "原文C" },
];
hits[0].bibliography = { id: "record-one", title: "資料の正式タイトル", authors: ["著者A", "著者B"],
  publication_title: "研究誌", publisher: "架空研究出版", publication_date: "2011-03-20",
  volume: "12", issue: "4", pages: "41–55", doi: "10.1234/source", isbn: "9780000000000",
  url: "https://example.invalid/book", metadata_source: "pdf_document_info", metadata_status: "complete" };
hits[1].bibliography = { id: "record-two", title: "別の資料_1999", authors: [],
  metadata_source: "filename", metadata_status: "partial" };
const errors = [];
const writes = [];
const searches = [];
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");
  try {
    if (request.method !== "GET") { writes.push(request.url); response.statusCode = 405; response.end(); return; }
    if (url.pathname === "/favicon.ico") { response.statusCode = 204; response.end(); return; }
    if (url.pathname.startsWith("/api/")) {
      response.setHeader("Content-Type", "application/json");
      let result = {};
      if (url.pathname === "/api/search") {
        searches.push(url.searchParams.get("q"));
        await new Promise((resolve) => setTimeout(resolve, 120));
        if (url.searchParams.get("q") === "失敗") { response.statusCode = 500; result = { detail: "検索を一時的に利用できません。" }; }
        else result = { results: url.searchParams.get("q") === "空" ? [] : [...hits, hits[0]] };
      } else if (url.pathname === "/api/corpus/latest") result = { batch: null };
      else if (url.pathname === "/api/corpus/directories") result = { directories: [] };
      else if (url.pathname === "/api/search-reports") result = [];
      else throw new Error(`Unexpected API: ${url.pathname}`);
      response.end(JSON.stringify(result)); return;
    }
    const filename = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    assert(!filename.includes(".."));
    const root = filename.startsWith("src/knowledge-excerpts.") ? sourceRoot : fixtureRoot;
    let content = await fs.readFile(path.join(root, filename));
    if (filename === "index.html") {
      let html = content.toString();
      if (!html.includes("data-fukamiru-knowledge-excerpts")) html = html.replace("</head>", '<link rel="stylesheet" href="/src/knowledge-excerpts.css"></head>')
        .replace("</body>", '<script type="module" data-fukamiru-knowledge-excerpts src="/src/knowledge-excerpts.js"></script></body>');
      content = Buffer.from(html);
    }
    if (filename === "src/app.js") content = Buffer.from(content.toString().replace(/\nboot\(\);\s*$/,
      '\nstate.activity="rag"; bindPaneLayout(); bindDraftViewer(); bindNavigation(); renderRagEditor();'));
    response.setHeader("Content-Type", filename.endsWith(".js") ? "text/javascript; charset=utf-8"
      : filename.endsWith(".css") ? "text/css" : "text/html; charset=utf-8");
    response.end(content);
  } catch (error) { errors.push(String(error)); response.statusCode = 500; response.end(String(error)); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true, args: ["--disable-gpu"],
  channel: process.env.PLAYWRIGHT_CHANNEL || "msedge" });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const external = [];
  page.on("request", (request) => { if (/^https?:/.test(request.url()) && new URL(request.url()).hostname !== "127.0.0.1") external.push(request.url()); });
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const query = page.locator("#knowledge-excerpts-query");
  await query.fill("参加者の参加");
  await page.locator("#knowledge-excerpts-submit").click();
  assert(await page.locator("#knowledge-excerpts-submit").isDisabled());
  await page.locator("#knowledge-excerpts-form").evaluate((form) => form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true })));
  await page.locator(".knowledge-excerpts-choice").waitFor();
  assert.deepEqual(searches, ["参加者の参加"]);
  assert.equal(await page.locator("#knowledge-excerpts-inspector").isVisible(), false);
  assert((await page.locator(".knowledge-excerpts-choice").textContent()).includes("2資料 · 3箇所"));
  await page.locator('.pane-toggle[data-pane="right"]').click();
  await page.locator(".knowledge-excerpts-choice").click();
  const reader = page.frameLocator("#knowledge-excerpts-frame");
  await reader.locator("#excerpt-1-text").waitFor();
  assert((await page.locator("#knowledge-excerpts-frame").boundingBox()).height > 400);
  assert.equal(await reader.locator("#excerpt-1-text").textContent(), hits[0].text);
  assert.equal(await reader.locator("#excerpt-3-text").textContent(), "原文C");
  assert((await reader.locator("#excerpt-1-title").textContent()).includes("p. 12–13"));
  assert((await reader.locator("#report-scope").textContent()).includes("AIによる要約は行っていません"));
  assert.equal(await reader.locator("#source-1-bibliography-authors").textContent(), "著者：著者A / 著者B");
  assert.equal(await reader.locator("#source-1-bibliography-title").textContent(), "登録タイトル：資料の正式タイトル");
  assert.equal(await reader.locator("#source-1-bibliography-publication").textContent(), "出版者：架空研究出版 · 刊行日：2011-03-20");
  assert.equal(await reader.locator("#source-1-bibliography-details").textContent(), "掲載誌・収録先：研究誌 · 巻：12 · 号：4 · 掲載ページ：41–55");
  assert((await reader.locator("#source-1-bibliography-identifiers").textContent()).includes("DOI：10.1234/source · ISBN：9780000000000"));
  assert((await reader.locator("#source-2-bibliography-status").textContent()).includes("ファイル名からの仮登録"));
  assert.equal(await reader.locator("#source-2-bibliography-publication").textContent(), "出版者：未登録 · 刊行日：未登録");
  assert.equal(await reader.locator("script,img").count(), 0);
  assert.equal(await page.evaluate(() => window.bad), undefined);
  await reader.locator('a[href="about:srcdoc#source-2"]').click();
  assert.equal(await reader.locator("#source-2-title").textContent(), "別の資料");
  const downloadWait = page.waitForEvent("download");
  await page.locator("#knowledge-excerpts-download").click();
  const download = await downloadWait;
  await download.saveAs(path.join(output, "fixture-report.html"));
  const exported = await fs.readFile(path.join(output, "fixture-report.html"), "utf8");
  assert(exported.includes("&lt;script&gt;window.parent.bad=true&lt;/script&gt;"));
  assert(!exported.includes("<script>"));
  assert(exported.includes("登録タイトル：</strong><cite>資料の正式タイトル</cite>"));
  assert(exported.includes("DOI：10.1234/source"));
  assert(exported.includes("ファイル名からの仮登録"));
  await reader.locator("#source-1-bibliography").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, "desktop.png"), animations: "disabled" });
  await query.fill("失敗"); await page.locator("#knowledge-excerpts-submit").click();
  await page.waitForFunction(() => document.querySelector("#knowledge-excerpts-status").textContent.includes("作成できません"));
  assert.equal(await page.locator(".knowledge-excerpts-choice").count(), 1);
  await query.fill("空"); await page.locator("#knowledge-excerpts-submit").click();
  await page.waitForFunction(() => document.querySelectorAll(".knowledge-excerpts-choice").length === 2);
  await page.locator(".knowledge-excerpts-choice").first().click();
  await reader.locator("#report-empty").waitFor();
  // The overlay closes when the existing app takes the inspector for another viewer.
  await page.locator("#review-inspector").evaluate((element) => { element.hidden = false; });
  await page.waitForFunction(() => document.querySelector("#knowledge-excerpts-inspector").hidden);
  assert(await page.locator("#review-inspector").isVisible());
  await page.locator("#review-inspector").evaluate((element) => { element.hidden = true; });
  await page.locator('[data-activity="projects"]').click();
  await page.locator('[data-activity="rag"]').click();
  await page.locator("#knowledge-excerpts-form").waitFor();
  assert.equal(await page.locator(".knowledge-excerpts-choice").count(), 2);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".knowledge-excerpts-choice").nth(1).click();
  await reader.locator("#excerpt-1-text").waitFor();
  await reader.locator("#source-1-bibliography").scrollIntoViewIfNeeded();
  const rect = await page.locator("#inspector-pane").boundingBox();
  assert(rect.width > 250 && rect.x + rect.width <= 390);
  await page.screenshot({ path: path.join(output, "mobile.png"), animations: "disabled" });
  await page.locator("#knowledge-excerpts-close").click();
  assert(await query.evaluate((element) => document.activeElement === element));
  await page.reload();
  await page.locator("#knowledge-excerpts-form").waitFor();
  assert.equal(await page.locator(".knowledge-excerpts-choice").count(), 0);
  assert.deepEqual(writes, []);
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  console.log("Excerpt UI passed: GET-only search, grouping/exact text, escaping, zero hits, failure retention, right pane, download, navigation, mobile and tab-only lifetime.");
} finally {
  await browser.close(); server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
