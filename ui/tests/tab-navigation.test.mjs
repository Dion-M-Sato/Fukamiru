import test from "node:test";
import assert from "node:assert/strict";

import { readSelectedTabs, urlForSelectedTabs } from "../src/tab-navigation.js";

test("reload restores the selected activity and lower panel from the URL", () => {
  assert.deepEqual(
    readSelectedTabs("?run_id=run-1&activity=review&panel=events"),
    { activity: "review", panel: "events" },
  );
  assert.deepEqual(readSelectedTabs("?activity=unknown&panel=unknown"), {
    activity: "projects", panel: "problems",
  });
});

test("tab changes preserve the run link and hash without building history entries", () => {
  const url = urlForSelectedTabs(
    "http://localhost:8765/?run_id=run-1#output",
    { activity: "authoring", panel: "run" },
  );
  assert.equal(url, "/?run_id=run-1&activity=authoring&panel=run#output");
  assert.equal(
    urlForSelectedTabs(`http://localhost:8765${url}`, {
      activity: "projects", panel: "problems",
    }),
    "/?run_id=run-1#output",
  );
});
