import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const source = resolve(root, "docs/diagrams/manuscript-node-descriptions.json");
const target = resolve(root, "docs/diagrams/manuscript-generator-node-copy.js");
const data = JSON.parse(readFileSync(source, "utf8"));
const lanes = new Set(data.lanes.map(({ id }) => id));
const nodes = new Set(data.nodes.map(({ id }) => id));
if (lanes.size !== data.lanes.length || nodes.size !== data.nodes.length ||
    data.nodes.some(({ id, lane, purpose, summary }) => !id || !lanes.has(lane) || !purpose || !summary)) {
  throw new Error("Node descriptions require unique IDs, known lanes and descriptive text");
}
const output = `// Generated node descriptions. Do not edit by hand.\nwindow.MANUSCRIPT_NODE_COPY = ${JSON.stringify(data, null, 2)};\n`;
if (process.argv.includes("--check")) {
  if (readFileSync(target, "utf8") !== output) throw new Error("Node description data is stale");
} else {
  writeFileSync(target, output, "utf8");
}
