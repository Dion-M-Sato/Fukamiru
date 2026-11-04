import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import test from "node:test";

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.hidden = false;
    this.textContent = "";
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  focus() { Element.focused = this; }
  click() { this.listeners.click?.({ target: this }); }
}

function loadMap() {
  const root = resolve(import.meta.dirname, "../..");
  const html = readFileSync(resolve(root, "docs/diagrams/knowledge-ingestion.html"), "utf8");
  const source = JSON.parse(readFileSync(resolve(root, "docs/diagrams/knowledge-ingestion.workflow.json"), "utf8"));
  const data = JSON.parse(html.match(/<script id="workflow-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
  assert.deepEqual(data.workflows.corpus.nodes, source.nodes);
  assert.deepEqual(data.workflows.single.nodes, source.singleWorkflow.nodes);
  const script = html.match(/<script>\s*([\s\S]*?)<\/script>\s*<\/body>/)[1];
  const ids = ["workflow-data", "map", "stats", "node-modal", "modal-close", "detail-index",
    "detail-title", "detail-description", "detail-status", "filter-summary", "source-note",
    "modal-id", "modal-order", "modal-title", "modal-role", "modal-status", "modal-target",
    "modal-boundary", "modal-safety", "modal-lane", "modal-flow", "modal-flow-note",
    "active-workflow-title", "active-workflow-note", "active-outcome-title", "active-outcome-path"];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
  elements["workflow-data"].textContent = JSON.stringify(data);
  const filters = ["all", "parallel", "bounded", "conditional", "serial", "guard"].map((value) => {
    const filter = new Element("button");
    filter.dataset.filter = value;
    return filter;
  });
  const tabs = ["corpus", "single"].map((value) => {
    const tab = new Element("button");
    tab.dataset.workflow = value;
    return tab;
  });
  const document = {
    body: new Element("body"),
    listeners: {},
    getElementById: (id) => elements[id],
    createElement: (tag) => new Element(tag),
    querySelectorAll: (selector) => selector === ".filter" ? filters
      : selector === ".workflow-tab" ? tabs : [],
    addEventListener(name, listener) { this.listeners[name] = listener; },
  };
  document.body.classList = { add() {}, remove() {} };
  runInNewContext(script, { document, Map });
  const nodes = () => elements.map.children.flatMap((stage) => stage.children[1].children);
  return { html, data, document, elements, filters, tabs, nodes };
}

function verifyVisibleWorkflow(map, workflow) {
  const nodes = map.nodes();
  assert.equal(nodes.length, workflow.nodes.length);
  for (const [index, card] of nodes.entries()) {
    const node = workflow.nodes[index];
    card.click();
    assert.equal(map.elements["node-modal"].hidden, false, node.id);
    assert.equal(map.elements["modal-title"].textContent, node.label, node.id);
    assert.equal(map.elements["modal-role"].textContent, node.role, node.id);
    assert.equal(map.elements["modal-target"].textContent, node.target, node.id);
    assert.equal(map.elements["modal-boundary"].textContent, node.boundary, node.id);
    assert.equal(map.elements["modal-safety"].textContent, node.safety, node.id);
    assert.equal(map.elements["modal-flow"].children.length, 1, node.id);
    map.document.listeners.keydown({ key: "Escape", preventDefault() {} });
    assert.equal(map.elements["node-modal"].hidden, true, node.id);
    assert.equal(Element.focused, card, node.id);
  }
}

test("both RAG workflows retain graph filters and complete node modals", () => {
  const map = loadMap();
  const corpus = map.data.workflows.corpus;
  const single = map.data.workflows.single;
  assert.equal(single.researchSourceHandoff.name, "research_source_ingestion");
  assert.deepEqual(single.researchSourceHandoff.nodes.filter((node) =>
    node.id !== "run_ocr").map((node) => node.id), [
    "register_asset", "extract_text", "audit_ocr", "infer_structure", "build_chunks",
    "index_chunks", "publish_research_index",
  ]);
  assert.match(single.meta.note, /原稿生成はこのRunを待たず/);
  assert.equal(corpus.meta.title, "RAG作成ワークフロー V2グラフ");
  assert.equal(single.meta.title, "単一資料取り込み V2グラフ");
  assert.equal(map.nodes().length, 16);
  assert.match(map.html, /Mistral \/ NDLOCR-Lite → SourceSpan \+ 書誌情報/);
  for (const workflow of [corpus, single]) {
    const structure = workflow.nodes.find((node) =>
      ["infer_structure", "rebuild_document_structure"].includes(node.id));
    assert.equal(structure.label, "文書構造・書誌を保存");
    assert.match(structure.boundary, /書誌リンクを同じトランザクションで確定/);
  }
  assert.deepEqual(single.nodes.map((node) => node.id), [
    "locate_source", "inspect_text_layer", "run_ocr",
    "register_asset", "extract_text", "audit_ocr", "infer_structure",
    "build_chunks", "index_chunks",
  ]);
  verifyVisibleWorkflow(map, corpus);
  const bounded = map.filters.find((filter) => filter.dataset.filter === "bounded");
  bounded.click();
  assert.equal(map.nodes().filter((card) => !card.hidden).length, 2);
  assert.deepEqual(corpus.nodes.filter((node) => node.status === "bounded").map((node) => node.id), [
    "extract_and_normalize_source", "build_bge_m3_embeddings",
  ]);
  const chunks = corpus.nodes.find((node) => node.id === "build_retrieval_chunks");
  assert.match(chunks.boundary, /計算中はこの工程の書き込みロックを取得せず/);
  assert.match(chunks.boundary, /SAVEPOINT/);
  assert.match(chunks.safety, /挿入失敗時は旧Chunkを復元/);
  map.filters.find((filter) => filter.dataset.filter === "all").click();
  map.tabs.find((tab) => tab.dataset.workflow === "single").click();
  assert.equal(map.nodes().length, 9);
  assert.equal(map.elements["active-workflow-title"].textContent, single.meta.title);
  verifyVisibleWorkflow(map, single);
  const parallel = map.filters.find((filter) => filter.dataset.filter === "parallel");
  parallel.click();
  assert.equal(map.nodes().filter((card) => !card.hidden).length, 0);
});
