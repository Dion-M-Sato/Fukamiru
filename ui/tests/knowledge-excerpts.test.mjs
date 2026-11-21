import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createExcerptReport, excerptDownloadDocument, EXCERPT_REPORT_FLOW } from "../src/knowledge-excerpts.js";

test("excerpt reports group original text by source, retain rank and position, and escape content", () => {
  const hits = [
    { chunk_id: "a", source_asset_id: "book", title: "書籍 <one>", path: "C:/book.txt", text: '<script>bad()</script>\n原文 & 続き', citation: { start_label: "十二", end_label: "十三" } },
    { chunk_id: "b", source_asset_id: "other", title: "別の資料", text: "原文B", citation: { start_page: 2 } },
    { chunk_id: "c", source_asset_id: "book", title: "別の章", path: "C:/book.txt", text: "原文C" },
  ];
  const original = structuredClone(hits);
  const report = createExcerptReport('検索語 "&<>', [...hits, hits[0], { text: " " }], "2026-11-15T04:00:00Z");
  assert.deepEqual(hits, original);
  assert.equal(report.source_count, 2);
  assert.equal(report.result_count, 3);
  assert.equal(report.groups[0].excerpts[1].rank, 3);
  assert.equal(report.groups[0].excerpts[0].text, hits[0].text);
  assert.equal(report.groups[0].excerpts[0].position, "p. 十二–十三");
  assert(report.canonical_html.includes('href="#source-1"'));
  assert(report.canonical_html.includes("&lt;script&gt;bad()&lt;/script&gt;"));
  assert(!report.canonical_html.includes("<script>"));
  assert(report.canonical_html.includes("AIによる要約は行っていません"));
  assert(excerptDownloadDocument(report).includes(report.canonical_html));
});

test("empty searches produce an honest report without fabricated prose", () => {
  const report = createExcerptReport("no hits", []);
  assert.equal(report.result_count, 0);
  assert.equal(report.source_count, 0);
  assert(report.canonical_html.includes("該当する検索結果がありません"));
  assert.throws(() => createExcerptReport(" ", []));
  assert.throws(() => createExcerptReport("query", {}));
});

test("bibliography is a source-linked snapshot, with registered fields escaped in the reader and download", () => {
  const bibliography = { id: "record-a", title: "正式書名 <&>", authors: ["著者A", "著者B <script>"],
    publication_title: "掲載誌", publisher: "出版社", publication_date: "2011-03-20",
    volume: "12", issue: "4", pages: "41–55", doi: "10.1234/source", isbn: "9780000000000",
    url: 'https://example.invalid/?x="<img>&y=2', metadata_source: "pdf_document_info", metadata_status: "complete" };
  const report = createExcerptReport("query", [
    { chunk_id: "a", source_asset_id: "one", title: "章題", text: "抜粋1" },
    { chunk_id: "b", source_asset_id: "two", title: "別資料", text: "抜粋2", bibliography: { id: "record-b", title: "別の正式書名", metadata_source: "filename", metadata_status: "partial" } },
    { chunk_id: "c", source_asset_id: "one", title: "別章", text: "抜粋3", bibliography },
  ]);
  assert.deepEqual(report.groups[0].bibliography, bibliography);
  bibliography.title = "changed"; bibliography.authors.push("changed");
  assert.equal(report.groups[0].bibliography.title, "正式書名 <&>");
  assert.equal(report.groups[0].bibliography.authors.length, 2);
  for (const value of ["正式書名 &lt;&amp;&gt;", "著者A / 著者B &lt;script&gt;", "掲載誌・収録先：掲載誌",
    "出版者：出版社", "刊行日：2011-03-20", "巻：12", "号：4", "掲載ページ：41–55",
    "DOI：10.1234/source", "ISBN：9780000000000", "書誌ID：record-a", "PDFの文書情報"]) {
    assert(report.canonical_html.includes(value), value);
    assert(excerptDownloadDocument(report).includes(value), value);
  }
  assert(!report.canonical_html.includes("<script>"));
  assert(!report.canonical_html.includes("<img>"));
  assert(!report.canonical_html.includes('href="https:'));
  assert.equal((report.canonical_html.match(/id="source-1-bibliography"/g) || []).length, 1);
  assert(report.canonical_html.includes('id="source-2-bibliography-title"><strong>登録タイトル：</strong><cite>別の正式書名</cite>'));
});

test("partial and missing bibliography stay explicit without treating filenames as verified citations", () => {
  const report = createExcerptReport("query", [
    { chunk_id: "a", source_asset_id: "one", title: "File_2011", text: "A", bibliography: {
      id: "partial", title: "File_2011", authors: [], publication_date: null, publisher: " ",
      metadata_source: "filename", metadata_status: "partial" } },
    { chunk_id: "b", source_asset_id: "two", title: "Unknown_1999", text: "B", bibliography: null },
  ]);
  assert(report.canonical_html.includes("登録情報（一部未登録） · ファイル名からの仮登録"));
  assert(report.canonical_html.includes("<strong>著者：</strong>未登録"));
  assert(report.canonical_html.includes("出版者：未登録 · 刊行日：未登録"));
  assert(report.canonical_html.includes("書誌情報が登録されていません。"));
  assert(report.canonical_html.includes("未登録の項目は推測で補っていません"));
  assert(!report.canonical_html.includes("刊行日：2011"));
  assert(!report.canonical_html.includes("刊行日：1999"));
  assert(!report.canonical_html.includes("[object Object]"));
});

test("the UI-linked diagram describes the exact frontend flow and no inference or database writes", () => {
  const data = JSON.parse(readFileSync(new URL("../../docs/diagrams/knowledge-ingestion.workflow.json", import.meta.url), "utf8"));
  assert.deepEqual(data.uiExcerptReport.steps.map((step) => step.id), EXCERPT_REPORT_FLOW);
  assert.equal(data.uiExcerptReport.search_api, "GET /api/search");
  assert.equal(data.uiExcerptReport.inference, false);
  assert.equal(data.uiExcerptReport.backend_restart, false);
  assert(data.uiExcerptReport.bibliography.includes("search_result.bibliography"));
});
