// Isolated V22 browser regression: no production API, proposal or Run is changed.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");
const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function initialFixture() {
  const condition = {
    reason: "semantic_resolution_required",
    phase: "argument_process:claim_evidence_links",
    unit_ids: ["link-a", "link-b"],
    block_ids: ["P-1"],
  };
  return {
    id: "fixture-v22-semantic",
    base_revision_id: "base",
    status: "needs_human_review",
    proposed_sha256: "b".repeat(64),
    semantic_review_context_sha256: "a".repeat(64),
    proposed_html: '<article id="a" lang="ja"><p id="P-1">確認用本文。</p></article>',
    validation: { valid: true },
    edit_summary: {},
    context: {
      workflow_version: 22,
      completion_status: "conditional_success",
      conditional_success: condition,
      conditional_conditions: [condition],
      semantic_review_items: ["link-a", "link-b"].map((unit, index) => ({
        review_id: `semantic-review-v22-${index + 1}`,
        source_type: "semantic_condition_unit",
        source_item_id: "condition-1",
        unit_id: unit,
        group_unit_ids: ["link-a", "link-b"],
        review_class: "human_semantic_judgement_required",
        phase: "argument_process:claim_evidence_links",
        kind: "claim_causality",
        block_ids: ["P-1"],
        explanation: `${unit} の因果関係を確認してください。`,
        status: "pending_human_judgement",
      })),
      reference_entries: [],
      machine_reference_entries: [],
      reference_metadata_confirmation_items: [],
      reference_quality_gate: { passed: true, issues: [] },
      reference_approval: { final_revision_blocked: false },
    },
  };
}

let fixture = initialFixture();
const patches = [];
const patchTargets = [];
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(`<html lang="ja"><head><meta charset="UTF-8"><link rel="stylesheet" href="/src/styles.css">
        <style>body{display:block;overflow:auto;margin:0;padding:16px}#manuscript-rewrite-result{max-width:900px;margin:auto}</style>
        </head><body><div id="manuscript-rewrite-result"></div><script type="module" src="/src/app.js"></script></body></html>`);
    } else if (url.pathname === "/src/app.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      const source = await fs.readFile(path.join(ui, "src/app.js"), "utf8");
      response.end(source.replace(/\nboot\(\);\s*$/, `\nrenderManuscriptRewriteProposal(${JSON.stringify(fixture)});`));
    } else if (url.pathname === "/src/api.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      response.end(`export async function api(url, options) {
        if (options?.method === 'PATCH' && url.includes('/semantic-reviews/')) {
          return fetch('/capture-semantic', {
            ...options, headers: {...(options.headers || {}), 'x-fukamiru-target': url},
          }).then(r => r.json());
        }
        return {targets:[]};
      }`);
    } else if (url.pathname === "/capture-semantic") {
      let body = "";
      for await (const chunk of request) body += chunk;
      const payload = JSON.parse(body);
      const target = String(request.headers["x-fukamiru-target"] || "");
      patches.push(payload);
      patchTargets.push(target);
      const reviewId = decodeURIComponent(target.split("/").at(-1));
      const next = structuredClone(fixture);
      next.context.semantic_review_items = next.context.semantic_review_items.filter(
        (item) => item.review_id !== reviewId,
      );
      next.semantic_review_context_sha256 = String(patches.length + 1).repeat(64);
      if (!next.context.semantic_review_items.length) {
        next.context.conditional_conditions = [];
        delete next.context.conditional_success;
        next.context.completion_status = "success";
      }
      fixture = next;
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(next));
    } else if (["/src/styles.css", "/src/run-labels.js", "/src/run-recovery.js", "/src/pane-layout.js", "/src/manuscript-policy.js"].includes(url.pathname)) {
      response.setHeader("Content-Type", url.pathname.endsWith("css") ? "text/css" : "text/javascript");
      response.end(await fs.readFile(path.join(ui, url.pathname.slice(1))));
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
  for (const width of [1200, 390]) {
    fixture = initialFixture();
    const startPatchCount = patches.length;
    const page = await browser.newPage({ viewport: { width, height: 950 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);

    assert.equal(await page.locator(".semantic-review-item").count(), 2);
    assert.equal(await page.locator("#manuscript-rewrite-confirm").isDisabled(), true);
    assert.match(
      await page.locator("#manuscript-semantic-resolution-warning").innerText(),
      /HTML手修正だけでは意味判定済みになりません/,
    );
    const first = page.locator(".semantic-review-item").first();
    await first.locator(".semantic-review-resolve").click();
    assert.match(await first.locator(".semantic-review-status").innerText(), /判断を選択/);
    await first.locator('[name="semantic_decision"]').selectOption("clarify_interpretation");
    await first.locator(".semantic-review-resolve").click();
    assert.match(await first.locator(".semantic-review-status").innerText(), /判断理由を入力/);

    await page.locator("#manuscript-rewrite-proposed").fill("未保存のHTML変更");
    await first.locator('[name="semantic_rationale"]').fill("因果関係を限定して確定する。");
    await first.locator(".semantic-review-resolve").click();
    assert.match(await first.locator(".semantic-review-status").innerText(), /保存だけでは意味判定は完了しません/);
    assert.equal(patches.length, startPatchCount);
    await page.locator("#manuscript-rewrite-proposed").fill(fixture.proposed_html);

    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-semantic")),
      first.locator(".semantic-review-resolve").click(),
    ]);
    assert.equal(patches.at(-1).expected_sha256, "b".repeat(64));
    assert.equal(patches.at(-1).expected_context_sha256, "a".repeat(64));
    assert.equal(patches.at(-1).decision, "clarify_interpretation");
    assert.equal(patches.at(-1).rationale, "因果関係を限定して確定する。");
    assert.match(patchTargets.at(-1), /semantic-reviews\/semantic-review-v22-1$/);
    assert.equal(await page.locator(".semantic-review-item").count(), 1);
    assert.equal(await page.locator("#manuscript-rewrite-confirm").isDisabled(), true);

    const second = page.locator(".semantic-review-item");
    await second.locator('[name="semantic_decision"]').selectOption("confirm_current_interpretation");
    await second.locator('[name="semantic_rationale"]').fill("第二の関係は現行稿の説明で成立する。");
    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-semantic")),
      second.locator(".semantic-review-resolve").click(),
    ]);
    assert.equal(await page.locator(".semantic-review-item").count(), 0);
    assert.equal(await page.locator("#manuscript-rewrite-confirm").isEnabled(), true);
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log("V22 browser: item-scoped human semantic adjudication passed on desktop/mobile");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
