// Isolated V22 browser regression: no production API, proposal or Run is changed.
// Run with Playwright available through NODE_PATH.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");
const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixture = {
  id: "fixture-v22",
  base_revision_id: "base",
  status: "needs_human_review",
  proposed_sha256: "before-v22",
  proposed_html: '<article id="a" lang="ja"><p id="p">確認用本文。</p></article>',
  validation: { valid: true },
  edit_summary: {},
  context: {
    workflow_version: 22,
    completion_status: "conditional_success",
    conditional_success: {
      reason: "response_contract_incomplete",
      phase: "rewrite",
      unit_ids: ["p-2"],
    },
    conditional_conditions: [{
      reason: "response_contract_incomplete",
      phase: "rewrite",
      unit_ids: ["p-2"],
    }],
    reference_entries: [{
      reference_id: "r",
      title: "メタヒストリー",
      author: "ヘイドン・ホワイト",
      search_completeness: {
        status: "search_incomplete",
        display_label: "確認できた候補すべて（網羅性未確認）",
      },
      candidate_matches: [
        { candidate_id: "a", title: "メタヒストリー", publisher: "作品社", year: "2017", volume: "上巻", edition: "初版", url: "https://example.org/a" },
        { candidate_id: "b", title: "メタヒストリー", publisher: "作品社", year: "2017", volume: "下巻", edition: "改訳版", url: "https://example.org/b" },
      ],
    }],
    reference_metadata_confirmation_items: [{
      machine_reference_id: "machine-1",
      confirmation_kind: "machine_reference",
      title: "翻訳書の候補",
      record_type: "book",
      translation_status: "requires_human_confirmation",
      translators: [],
    }, {
      reference_id: "consulted-child-1",
      source_reference_id: "consulted-parent-1",
      candidate_id: "ndl:selected-edition",
      confirmation_kind: "consulted_manifestation",
      title: "実際に参照した翻訳研究書",
      record_type: "book",
      edition: "改訂版",
      volume: "下巻",
      year: "2024",
      publisher: "研究出版社",
      is_translation: true,
      translation_status: "confirmed_translation",
      translators: [],
      required_missing_fields: ["translators"],
      user_action_required: {
        code: "complete_consulted_manifestation_metadata",
        fields: ["translators"],
        message: "実際に参照したこの版の訳者を確認してください。",
      },
    }, {
      reference_id: "manual-consulted-1",
      confirmation_kind: "consulted_manifestation",
      manual_manifestation_entry: true,
      manual_consulted_manifestation_allowed: true,
      resolution_status: "specified_scope_unresolved",
      title: "検索候補と一致しない参照版",
      author: "研究者",
      record_type: "book",
      edition: "改訳版",
      volume: "下巻",
      year: "2025",
      publisher: "研究出版社",
      translation_status: "undetermined",
      translators: [],
      required_missing_fields: ["translation_status_confirmation"],
      user_action_required: {
        code: "enter_consulted_manifestation",
        fields: ["volume", "edition"],
        message: "実際に参照した版・巻を入力してください。",
      },
    }, {
      reference_id: "unresolved-1",
      confirmation_kind: "unresolved_reference",
      resolution_status: "not_found",
      title: "調査中の未刊論考",
      author: "著者本人",
      is_self_authored: false,
      is_unpublished: false,
      user_action_required: {
        code: "identify_work_or_correct_query",
        fields: [
          "document_type",
          "publication_year",
          "venue_or_publisher",
          "pages_if_applicable",
        ],
        message: "外部データベースでは文献を同定できませんでした。",
      },
    }],
  },
};
const posts = [];
const patches = [];
const patchTargets = [];
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
        if (options?.method === 'POST') return fetch('/capture-post', options).then(r => r.json());
        if (options?.method === 'PATCH') return fetch('/capture-patch', {
          ...options, headers: {...(options.headers || {}), 'x-fukamiru-target': url},
        }).then(r => r.json());
        return {targets:[]};
      }`);
    } else if (["/capture-post", "/capture-patch"].includes(url.pathname)) {
      let body = "";
      for await (const chunk of request) body += chunk;
      (url.pathname === "/capture-post" ? posts : patches).push(JSON.parse(body));
      if (url.pathname === "/capture-patch") {
        patchTargets.push(request.headers["x-fukamiru-target"] || "");
      }
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(fixture));
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
  for (const width of [1250, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 950 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    assert.equal(await page.locator("#manuscript-rewrite-confirm").isDisabled(), true);
    assert.match(
      await page.locator("#manuscript-reference-approval-warning").innerText(),
      /参考文献の確定が必要です/,
    );
    assert.match(
      await page.locator("#manuscript-response-contract-warning").innerText(),
      /必須推論.*未完了.*承認は停止/s,
    );
    await page.getByText("参考文献候補：版・巻次を選択", { exact: true }).click();

    const fieldset = page.locator('.reference-choice[data-selection-mode="multiple"]');
    assert.equal(await fieldset.count(), 1);
    assert.equal(await fieldset.locator('input[type="checkbox"]').count(), 2);
    assert.equal(await fieldset.locator('input[type="radio"]').count(), 0);
    assert.equal(await fieldset.locator(".reference-select").isDisabled(), true);
    assert.equal(await fieldset.locator(".reference-selection-summary").innerText(), "0件選択中");
    assert.match(
      await fieldset.locator(".reference-search-completeness").innerText(),
      /確認できた候補すべて（網羅性未確認）.*ページ送りなし/s,
    );

    await fieldset.locator('input[value="a"]').check();
    await fieldset.locator('input[value="b"]').check();
    assert.equal(await fieldset.locator(".reference-selection-summary").innerText(), "2件選択中");
    assert.equal(await fieldset.locator(".reference-select").isEnabled(), true);

    await page.locator("#manuscript-rewrite-proposed").fill("未保存の修正");
    await fieldset.locator(".reference-select").click();
    assert.match(await fieldset.locator(".reference-status").innerText(), /未保存内容は上書きしません/);
    assert.equal(posts.length, width === 1250 ? 0 : 1);
    await page.locator("#manuscript-rewrite-proposed").fill(fixture.proposed_html);

    const overflow = await fieldset.evaluate((element) => element.scrollWidth > element.clientWidth + 2);
    assert.equal(overflow, false);
    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-post")),
      fieldset.locator(".reference-select").click(),
    ]);
    assert.deepEqual(posts.at(-1).candidate_ids, ["a", "b"]);
    assert.equal(Object.hasOwn(posts.at(-1), "candidate_id"), false);
    assert.equal(posts.at(-1).expected_sha256, "before-v22");

    const metadata = page.locator('.reference-metadata-confirmation[data-reference-id="machine-1"]');
    assert.equal(await metadata.count(), 1);
    assert.equal(await metadata.getByText(/実際に参照した文献/).count(), 0);
    await metadata.locator('[name="is_translation"]').selectOption("true");
    await metadata.locator(".reference-metadata-confirm").click();
    assert.match(await metadata.locator(".reference-status").innerText(), /訳者を入力/);
    assert.equal(await metadata.locator('[name="translators"]').evaluate((input) => input === document.activeElement), true);
    assert.equal(patches.length, width === 1250 ? 0 : 4);
    await metadata.locator('[name="translators"]').fill("山田太郎／佐藤花子");
    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-patch")),
      metadata.locator(".reference-metadata-confirm").click(),
    ]);
    assert.equal(patches.at(-1).is_translation, true);
    assert.deepEqual(patches.at(-1).translators, ["山田太郎", "佐藤花子"]);
    assert.equal(patches.at(-1).expected_sha256, "before-v22");
    assert.equal(Object.hasOwn(patches.at(-1), "is_self_authored"), false);
    assert.equal(Object.hasOwn(patches.at(-1), "is_unpublished"), false);
    assert.match(patchTargets.at(-1), /\/machine-references\/machine-1$/);
    const metadataOverflow = await metadata.evaluate((element) => element.scrollWidth > element.clientWidth + 2);
    assert.equal(metadataOverflow, false);

    const consultedMetadata = page.locator('.reference-metadata-confirmation[data-reference-id="consulted-child-1"]');
    assert.equal(await consultedMetadata.count(), 1);
    assert.match(await consultedMetadata.locator("legend").innerText(), /実際に参照した版・巻.*改訂版・下巻・2024・研究出版社/);
    assert.equal(await consultedMetadata.locator(".reference-classification").count(), 0);
    assert.equal(await consultedMetadata.locator('[name="is_translation"]').inputValue(), "true");
    await consultedMetadata.locator('[name="translators"]').fill("参照版訳者");
    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-patch")),
      consultedMetadata.locator(".reference-metadata-confirm").click(),
    ]);
    assert.deepEqual(patches.at(-1).translators, ["参照版訳者"]);
    assert.equal(Object.hasOwn(patches.at(-1), "is_self_authored"), false);
    assert.equal(Object.hasOwn(patches.at(-1), "is_unpublished"), false);
    assert.match(patchTargets.at(-1), /\/machine-references\/consulted-child-1$/);
    const consultedOverflow = await consultedMetadata.evaluate((element) => element.scrollWidth > element.clientWidth + 2);
    assert.equal(consultedOverflow, false);

    const unresolvedMetadata = page.locator('.reference-metadata-confirmation[data-reference-id="unresolved-1"]');
    assert.match(
      await unresolvedMetadata.locator(".reference-missing-fields").innerText(),
      /資料種別（書籍・論文・章・未刊原稿）・発表年・掲載先または出版社・該当するページ/,
    );
    assert.equal(await unresolvedMetadata.locator('[name="is_self_authored"]').isChecked(), false);
    assert.equal(await unresolvedMetadata.locator('[name="is_unpublished"]').isChecked(), false);
    assert.equal(await unresolvedMetadata.locator(".reference-metadata-confirm").isDisabled(), true);
    await unresolvedMetadata.locator('[name="is_self_authored"]').check();
    await unresolvedMetadata.locator('[name="is_unpublished"]').check();
    assert.equal(await unresolvedMetadata.locator(".reference-metadata-confirm").isEnabled(), true);
    assert.match(await unresolvedMetadata.locator(".reference-classification-status").innerText(), /自著・未刊資料/);
    await unresolvedMetadata.locator('[name="is_translation"]').selectOption("false");
    await unresolvedMetadata.locator('[name="record_type"]').selectOption("unpublished");
    await unresolvedMetadata.locator('[name="year"]').fill("2026");
    await unresolvedMetadata.locator('[name="venue"]').fill("個人所蔵");
    await unresolvedMetadata.locator('[name="pages"]').fill("1-20");
    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-patch")),
      unresolvedMetadata.locator(".reference-metadata-confirm").click(),
    ]);
    assert.equal(patches.at(-1).is_self_authored, true);
    assert.equal(patches.at(-1).is_unpublished, true);
    assert.equal(Object.hasOwn(patches.at(-1), "author"), false);
    assert.equal(Object.hasOwn(patches.at(-1), "title"), false);
    assert.equal(patches.at(-1).record_type, "unpublished");
    assert.equal(patches.at(-1).year, "2026");
    assert.equal(patches.at(-1).venue, "個人所蔵");
    assert.equal(patches.at(-1).pages, "1-20");
    const unresolvedOverflow = await unresolvedMetadata.evaluate((element) => element.scrollWidth > element.clientWidth + 2);
    assert.equal(unresolvedOverflow, false);

    const manual = page.locator('.reference-metadata-confirmation[data-reference-id="manual-consulted-1"]');
    assert.equal(await manual.locator(".reference-classification").count(), 0);
    assert.match(await manual.locator("legend").innerText(), /実際に参照した版・巻.*改訳版・下巻/);
    assert.match(await manual.getByText(/検索候補と一致しなかった/).innerText(), /手入力で確定/);
    assert.equal(await manual.locator(".reference-metadata-confirm").innerText(), "実際に参照した版・巻を登録");
    await manual.locator('[name="is_translation"]').selectOption("true");
    await manual.locator('[name="translators"]').fill("改訳版訳者");
    await manual.locator('[name="original_title"]').fill("Original Work");
    await manual.locator('[name="translated_title"]').fill("検索候補と一致しない参照版");
    await Promise.all([
      page.waitForResponse((result) => result.url().endsWith("/capture-patch")),
      manual.locator(".reference-metadata-confirm").click(),
    ]);
    assert.equal(patches.at(-1).volume, "下巻");
    assert.equal(patches.at(-1).edition, "改訳版");
    assert.equal(patches.at(-1).original_title, "Original Work");
    assert.equal(patches.at(-1).translated_title, "検索候補と一致しない参照版");
    assert.deepEqual(patches.at(-1).translators, ["改訳版訳者"]);
    assert.equal(Object.hasOwn(patches.at(-1), "is_self_authored"), false);
    assert.equal(Object.hasOwn(patches.at(-1), "is_unpublished"), false);
    assert.match(patchTargets.at(-1), /\/machine-references\/manual-consulted-1$/);
    const manualOverflow = await manual.evaluate((element) => element.scrollWidth > element.clientWidth + 2);
    assert.equal(manualOverflow, false);
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log("V22 browser: multi-select and metadata confirmation passed on desktop/mobile");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
