// Isolated browser regression: no production API, proposal or Run is changed.
// Run with Playwright available through NODE_PATH.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const { chromium } = createRequire(import.meta.url)("playwright");
const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.resolve(ui, "../output/verification-v21");
await fs.mkdir(output, { recursive: true });
const fixture = {
  id: "fixture", base_revision_id: "base", status: "needs_human_review", proposed_sha256: "before",
  proposed_html: '<article id="a" lang="ja"><p id="p">確認用本文。</p></article>',
  validation: { valid: true }, context: { workflow_version: 21, reference_entries: [{
    reference_id: "r", title: "メタヒストリー", author: "ヘイドン・ホワイト",
    candidate_display: "候補A〔ヘイドン・ホワイト・メタヒストリー・訳者A・作品社・2017・上巻・出典A〕／候補B〔同著者・メタヒストリー・訳者B・作品社・2017・下巻・出典B〕",
    candidate_matches: [
      { candidate_id: "a", title: "メタヒストリー", publisher: "作品社", year: "2017", volume: "上巻", url: "https://example.org/a" },
      { candidate_id: "b", title: "メタヒストリー", publisher: "作品社", year: "2017", volume: "下巻", url: "https://example.org/b" },
    ],
  }] },
};
let posts = [];
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(`<html lang="ja"><head><meta charset="UTF-8"><link rel="stylesheet" href="/src/styles.css">
        <style>body{display:block;overflow:auto;margin:0;padding:16px}#manuscript-rewrite-result{max-width:1050px;margin:auto}</style>
        </head><body><div id="manuscript-rewrite-result"></div><script type="module" src="/src/app.js"></script></body></html>`);
    } else if (url.pathname === "/src/app.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      const source = await fs.readFile(path.join(ui, "src/app.js"), "utf8");
      response.end(source.replace(/\nboot\(\);\s*$/, `\nrenderManuscriptRewriteProposal(${JSON.stringify(fixture)});`));
    } else if (url.pathname === "/src/api.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      response.end(`export async function api(url, options) {
        if (options?.method === 'POST') return fetch('/capture', options).then(r => r.json());
        return {targets:[]};
      }`);
    } else if (url.pathname === "/capture") {
      let body = "";
      for await (const chunk of request) body += chunk;
      posts.push(JSON.parse(body));
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(fixture));
    } else if (["/src/styles.css", "/src/run-labels.js", "/src/pane-layout.js", "/src/manuscript-policy.js"].includes(url.pathname)) {
      response.setHeader("Content-Type", url.pathname.endsWith("css") ? "text/css" : "text/javascript");
      response.end(await fs.readFile(path.join(ui, url.pathname.slice(1))));
    } else { response.writeHead(404); response.end(); }
  } catch (error) { response.writeHead(500); response.end(String(error)); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true, channel: "msedge" });
try {
  for (const width of [1250, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 950 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByText("参考文献候補：版・巻次を選択", { exact: true }).click();
    assert.equal(await page.locator('.reference-choice[data-selection-mode="single"]').count(), 1);
    assert.equal(await page.locator('.reference-choice input[type="radio"]').count(), 2);
    assert.equal(await page.locator('.reference-choice input[type="checkbox"]').count(), 0);
    assert.equal(await page.locator('.reference-choice input:checked').count(), 0);
    await page.locator(".reference-select").click();
    assert.match(await page.locator(".reference-status").innerText(), /候補を選択/);
    await page.locator('.reference-choice input[value="b"]').check();
    await page.locator("#manuscript-rewrite-proposed").fill("未保存の修正");
    await page.locator(".reference-select").click();
    assert.match(await page.locator(".reference-status").innerText(), /未保存内容は上書きしません/);
    assert.equal(posts.length, width === 1250 ? 0 : 1);
    await page.locator("#manuscript-rewrite-proposed").fill(fixture.proposed_html);
    await page.screenshot({ path: path.join(output, `reference-choice-${width}.png`), fullPage: true });
    const overflow = await page.locator(".reference-choice").evaluate(e => e.scrollWidth > e.clientWidth + 2);
    assert.equal(overflow, false);
    await Promise.all([
      page.waitForResponse(r => r.url().endsWith("/capture")),
      page.locator(".reference-select").click(),
    ]);
    assert.equal(posts.at(-1).candidate_id, "b");
    assert.equal(posts.at(-1).expected_sha256, "before");
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log("V21 browser: desktop/mobile choice, unsaved protection, CAS payload, no overflow passed");
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
