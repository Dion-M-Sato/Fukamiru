// Isolated interaction regression: no production databases or model calls.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { chromium } from "playwright";

const root = path.resolve(import.meta.dirname, "..");
const output = path.resolve(root, "../output/verification-search-reports");
await fs.mkdir(output, { recursive: true });
const reports = [];
const errors = [];
let posts = 0;
const makeReport = (query, id) => ({ id, query, title: `調査レポート：${query}`,
  created_at: "2020-01-01T00:00:00Z", result_count: 2, source_count: 1,
  canonical_html: `<article><section><h2>${query}</h2><p>観測記録を通じた参加者の参加を確認した。<a href="#evidence-E1">[E1]</a></p><p>観測記録欄は参加者の意見を編集者に伝え、相互の交流を促した。</p></section><section id="bibliography-R1"><h2>使用文献</h2><p>架空太郎『観測ノートと参加者』東京：架空研究出版、2011年。</p><p>原資料の書誌ページを照合・再検査済み。</p></section><section id="evidence-E1"><h2>報告の根拠</h2><p>観測ノートと参加者 · PDF p. 12–13</p><p><a href="#bibliography-R1">[R1]</a></p><blockquote>観測ノートには参加者からの観測記録が掲載されている。</blockquote></section></article><script>window.parent.bad=true</script><img src="https://example.invalid/track">`,
});
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");
  try {
    if (url.pathname === "/favicon.ico") { response.statusCode = 204; response.end(); return; }
    if (url.pathname.startsWith("/api/")) {
      response.setHeader("Content-Type", "application/json");
      let result = {};
      if (url.pathname === "/api/corpus/latest") result = { batch: null };
      else if (url.pathname === "/api/corpus/directories") result = { directories: [] };
      else if (url.pathname === "/api/search-reports") {
        if (request.method === "POST") {
          posts += 1;
          let body = ""; for await (const chunk of request) body += chunk;
          const { query, limit } = JSON.parse(body);
          assert.equal(limit, 100);
          await new Promise((resolve) => setTimeout(resolve, 150));
          if (query === "失敗") {
            response.statusCode = 502;
            response.end(JSON.stringify({ detail: "AI要約が一時的に利用できません。" })); return;
          }
          result = makeReport(query, `report-${posts}`);
          reports.unshift(result);
          response.statusCode = 201;
        } else result = reports;
      } else if (url.pathname.startsWith("/api/search-reports/")) {
        const id = url.pathname.split("/")[3];
        if (url.pathname.endsWith("/export.html")) {
          response.setHeader("Content-Type", "text/html");
          response.setHeader("Content-Disposition", 'attachment; filename="search-report.html"');
          response.end(reports.find((item) => item.id === id).canonical_html); return;
        }
        await new Promise((resolve) => setTimeout(resolve, id === "report-1" ? 200 : 10));
        result = reports.find((item) => item.id === id);
      }
      response.end(JSON.stringify(result)); return;
    }
    const filename = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    assert(!filename.includes(".."));
    let content = await fs.readFile(path.join(root, filename));
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
  const externalRequests = [];
  page.on("request", (request) => {
    if (/^https?:/.test(request.url()) && new URL(request.url()).hostname !== "127.0.0.1") externalRequests.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  assert.equal(await page.locator(".search-report-choice").count(), 0);
  await page.locator("#search-form input").fill("参加者の参加");
  await page.locator("#search-form button").click();
  assert(await page.locator("#search-form button").isDisabled());
  await page.locator(".search-report-choice").waitFor();
  assert.equal(posts, 1);
  assert.equal(await page.locator("#search-results").textContent().then((text) => text.includes("観測記録を通じた")), false);
  assert.equal(await page.locator("#search-report-inspector").isVisible(), false);
  await page.locator('[data-pane="right"].pane-toggle').click();
  await page.locator(".search-report-choice").click();
  const reader = page.frameLocator("#search-report-frame");
  await reader.locator("a[href='about:srcdoc#evidence-E1']").waitFor();
  assert(await page.locator("#inspector-pane").isVisible());
  assert.equal(await page.locator("#search-report-frame").getAttribute("sandbox"), "");
  assert.equal(await reader.locator("script,img").count(), 0);
  assert.equal(await page.evaluate(() => window.bad), undefined);
  await reader.locator("a[href='about:srcdoc#evidence-E1']").click();
  assert.equal(await reader.locator("#evidence-E1").textContent().then((text) => text.includes("p. 12–13")), true);
  await reader.locator("a[href='about:srcdoc#bibliography-R1']").click();
  assert((await reader.locator("#bibliography-R1").textContent()).includes("架空研究出版、2011年"));
  assert.equal(await reader.locator('[id^="bibliography-"]').count(), 1);
  const downloadWait = page.waitForEvent("download");
  await page.locator("#search-report-export").click();
  const download = await downloadWait;
  assert.equal(download.suggestedFilename(), "search-report.html");
  await page.screenshot({ path: path.join(output, "desktop.png"), animations: "disabled" });
  await page.locator("#search-form input").fill("失敗");
  await page.locator("#search-form button").click();
  await page.waitForFunction(() => document.querySelector("#search-report-status").textContent.includes("作成できません"));
  assert.equal(await page.locator(".search-report-choice").count(), 1);
  assert(await page.locator("#search-report-inspector").isVisible());
  await page.locator("#search-form input").fill("参加者の観測記録");
  await page.locator("#search-form button").click();
  await page.waitForFunction(() => document.querySelectorAll(".search-report-choice").length === 2);
  // A slow old selection must not overwrite a newer click.
  await page.locator(".search-report-choice").nth(1).click();
  await page.locator(".search-report-choice").nth(0).click();
  await page.waitForFunction(() => document.querySelector("#search-report-name").textContent.includes("参加者の観測記録"));
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(await page.locator("#search-report-name").textContent(), "調査レポート：参加者の観測記録");
  await page.reload();
  await page.locator(".search-report-choice").nth(1).waitFor();
  assert.equal(posts, 3);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".search-report-choice").first().click();
  await reader.locator("a[href='about:srcdoc#evidence-E1']").waitFor();
  assert(await page.locator("#search-report-inspector").isVisible());
  const rect = await page.locator("#inspector-pane").boundingBox();
  assert(rect.width > 250 && rect.x >= 0 && rect.x + rect.width <= 390);
  await page.screenshot({ path: path.join(output, "mobile.png"), animations: "disabled" });
  await page.locator("#search-report-close").click();
  assert.equal(await page.locator("#search-report-inspector").isVisible(), false);
  assert(await page.locator("#search-form input").evaluate((element) => document.activeElement === element));
  assert.deepEqual(externalRequests, []);
  assert.deepEqual(errors, []);
  console.log("Knowledge Search browser checks passed: creation, busy/error states, saved history, evidence links, HTML download, selection race, desktop/mobile reader.");
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
