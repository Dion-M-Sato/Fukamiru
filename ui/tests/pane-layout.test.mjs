import test from "node:test";
import assert from "node:assert/strict";

import {
  clampPaneSize,
  normalizePaneLayout,
  paneSizeFromKey,
} from "../src/pane-layout.js";

test("pane layout defaults to three visible panes without forced pixel sizes", () => {
  assert.deepEqual(normalizePaneLayout(), {
    visible: { left: true, right: true, bottom: true },
    sizes: { left: null, right: null, bottom: null },
  });
});

test("saved visibility and finite pane sizes are normalized", () => {
  assert.deepEqual(normalizePaneLayout({
    visible: { left: false, bottom: false },
    sizes: { left: 263.6, right: "360", bottom: -4 },
  }), {
    visible: { left: false, right: true, bottom: false },
    sizes: { left: 264, right: 360, bottom: null },
  });
});

test("pane sizes respect both product limits and available workspace", () => {
  assert.equal(clampPaneSize("left", 900), 420);
  assert.equal(clampPaneSize("right", 500, 360), 360);
  assert.equal(clampPaneSize("bottom", 12), 96);
});

test("separator arrow keys follow the visual direction of each pane", () => {
  assert.equal(paneSizeFromKey("left", 240, "ArrowRight"), 256);
  assert.equal(paneSizeFromKey("right", 320, "ArrowLeft"), 336);
  assert.equal(paneSizeFromKey("bottom", 140, "ArrowUp", { shiftKey: true }), 188);
  assert.equal(paneSizeFromKey("bottom", 140, "ArrowLeft"), null);
});
