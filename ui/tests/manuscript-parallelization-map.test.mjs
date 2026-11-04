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
    this.classList = { add() {}, remove() {} };
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

function loadMap(file) {
  const html = readFileSync(file, "utf8");
  const data = JSON.parse(html.match(/<script id="workflow-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
  const script = html.match(/<script>\s*([\s\S]*?)<\/script>\s*<\/body>/)[1];
  const ids = ["workflow-data", "map", "stats", "node-modal", "modal-close", "detail-index",
    "detail-title", "detail-description", "detail-status", "filter-summary",
    "source-note", "modal-id", "modal-order", "modal-title", "modal-role", "modal-status",
    "modal-target", "modal-boundary", "modal-safety", "modal-lane", "modal-flow", "modal-flow-note"];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
  elements["workflow-data"].textContent = JSON.stringify(data);
  const filters = ["all", "parallel", "bounded", "conditional", "serial", "guard"].map((value) => {
    const filter = new Element("button");
    filter.dataset.filter = value;
    return filter;
  });
  const document = {
    body: new Element("body"),
    listeners: {},
    getElementById: (id) => elements[id],
    createElement: (tag) => new Element(tag),
    querySelectorAll: (selector) => selector === ".filter" ? filters : [],
    addEventListener(name, listener) { this.listeners[name] = listener; },
  };
  runInNewContext(script, { document, Map });
  const nodes = elements.map.children.flatMap((stage) => stage.children[1].children);
  return { html, data, document, elements, filters, nodes };
}

test("Workflow graph opens complete inner-process modal for every node", () => {
  const root = resolve(import.meta.dirname, "../..");
  for (const name of ["manuscript-rewrite.html"]) {
    const map = loadMap(resolve(root, "docs/diagrams", name));
    assert.equal(map.data.meta.title, "原稿書き換えワークフロー");
    assert.match(map.html, /主にローカル/);
    assert.doesNotMatch(map.html, /CPU処理|Python処理/);
    assert.equal(map.nodes.length, 29);
    for (const [index, card] of map.nodes.entries()) {
      const node = map.data.nodes[index];
      card.click();
      assert.equal(map.elements["node-modal"].hidden, false, node.id);
      assert.equal(map.elements["modal-title"].textContent, node.label, node.id);
      assert.equal(map.elements["modal-role"].textContent, node.role, node.id);
      assert.equal(map.elements["modal-target"].textContent, node.target, node.id);
      assert.equal(map.elements["modal-boundary"].textContent, node.boundary, node.id);
      assert.equal(map.elements["modal-safety"].textContent, node.safety, node.id);
      if (node.resource_lane === "ingestion_cpu") {
        assert.equal(map.elements["modal-lane"].textContent, "主にローカル", node.id);
      }
      assert.equal(map.elements["modal-flow"].children.length, 1, node.id);
      assert.equal(card.attributes["aria-haspopup"], "dialog");
      map.document.listeners.keydown({ key: "Escape", preventDefault() {} });
      assert.equal(map.elements["node-modal"].hidden, true, node.id);
      assert.equal(Element.focused, card, node.id);
    }
    map.nodes[0].click();
    map.elements["modal-close"].click();
    assert.equal(map.elements["node-modal"].hidden, true);
    map.nodes[0].click();
    map.elements["node-modal"].click();
    assert.equal(map.elements["node-modal"].hidden, true);
    const cpuParallel = map.data.nodes.find((node) =>
      node.resource_lane === "ingestion_cpu" && node.status === "parallel");
    assert.ok(cpuParallel);
    const parallelFilter = map.filters.find((filter) => filter.dataset.filter === "parallel");
    parallelFilter.click();
    assert.equal(map.nodes.find((card) => card.dataset.nodeId === cpuParallel.id).hidden, false);
    const serialFilter = map.filters.find((filter) => filter.dataset.filter === "serial");
    serialFilter.click();
    assert.equal(map.nodes.find((card) => card.dataset.nodeId === cpuParallel.id).hidden, true);
  }
});
