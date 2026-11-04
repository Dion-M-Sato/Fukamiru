// Isolated V22 browser regression: fixtures only; no production Run is changed.
// Run with Playwright available through NODE_PATH.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");
const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const targetFingerprint = "b".repeat(64);
const previousFingerprint = "a".repeat(64);
const fixtures = {
  outage: {
    run: {
      id: "run-outage",
      workflow_name: "manuscript_rewrite",
      workflow_version: "22",
      status: "waiting_human",
      input: { document_id: "DOC-OUTAGE" },
      error: { type: "ExternalServiceUnavailable", reason: "external_service_unavailable" },
    },
    active_nodes: [{ node_id: "review_manuscript_reasoning", status: "waiting_human" }],
    artifacts: [],
  },
  engine: {
    run: {
      id: "run-engine",
      workflow_name: "manuscript_rewrite",
      workflow_version: 22,
      status: "waiting_human",
      input: { document_id: "DOC-ENGINE" },
      error: {
        type: "ExternalServiceUnavailable",
        reason: "inference_engine_authorization_required",
        previous_engine_fingerprint: previousFingerprint,
        target_engine_fingerprint: targetFingerprint,
      },
    },
    active_nodes: [{ node_id: "plan_manuscript_structure", status: "waiting_human" }],
    artifacts: [],
  },
};
const requests = [];

async function waitForRecordedRequest(predicate, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const match = requests.find(predicate);
    if (match) return match;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`Timed out waiting for request; received ${JSON.stringify(requests)}`);
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://localhost");
    const kind = url.searchParams.get("kind") || "outage";
    const fixture = fixtures[kind] || fixtures.outage;
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(`<html lang="ja"><head><meta charset="UTF-8"><link rel="stylesheet" href="/src/styles.css"></head>
        <body><main class="workbench"><section id="bottom-pane"><div class="panel-tabs">
        <button class="panel-tab" data-panel="problems"></button><button class="panel-tab active" data-panel="run"></button></div>
        <div id="panel-content"></div></section><div id="editor-content"></div>
        <div id="run-status"></div><span id="status-run-summary"></span><span class="status-run-dot"></span>
        <span id="connection-status"></span><span id="problem-count">0</span></main>
        <script type="module" src="/src/app.js?kind=${kind}"></script></body></html>`);
    } else if (url.pathname === "/src/app.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      const source = await fs.readFile(path.join(ui, "src/app.js"), "utf8");
      response.end(source.replace(
        /\nboot\(\);\s*$/,
        `\nstate.panel = "run"; state.runs = [${JSON.stringify(fixture)}]; state.run = ${JSON.stringify(fixture)}; state.selectedRunId = ${JSON.stringify(fixture.run.id)}; window.__researchState = state; renderPanel();`,
      ));
    } else if (url.pathname === "/src/api.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      response.end(`export async function api(url, options = {}) {
        if (options.method === "POST") {
          const result = await fetch(url, options);
          return result.json();
        }
        if (url.includes("/progress")) return ${JSON.stringify(fixture)};
        if (url.startsWith("/api/runs?")) return { runs: [${JSON.stringify(fixture)}] };
        return {};
      }`);
    } else if (request.method === "POST" && url.pathname.startsWith("/api/runs/")) {
      let body = "";
      for await (const chunk of request) body += chunk;
      requests.push({ path: url.pathname, body });
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ status: url.pathname.endsWith("/cancel") ? "cancelled" : "queued" }));
    } else if (url.pathname.endsWith("/events")) {
      response.writeHead(204);
      response.end();
    } else if ([
      "/src/styles.css",
      "/src/run-labels.js",
      "/src/run-recovery.js",
      "/src/pane-layout.js",
      "/src/manuscript-policy.js",
    ].includes(url.pathname)) {
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
  const origin = `http://127.0.0.1:${server.address().port}`;

  const enginePage = await browser.newPage({ viewport: { width: 390, height: 850 } });
  const browserErrors = [];
  enginePage.on("pageerror", (error) => browserErrors.push(error.message));
  await enginePage.goto(`${origin}/?kind=engine`);
  assert.match(await enginePage.locator(".run-summary-heading").innerText(), /推論エンジン変更の承認待ち/);
  assert.equal(await enginePage.locator(".run-engine-fingerprints code").last().innerText(), targetFingerprint);
  await enginePage.getByRole("button", { name: "このfingerprintを承認して再開" }).evaluate((button) => button.click());
  const engineRequest = await waitForRecordedRequest(
    (item) => item.path.endsWith("run-engine/resume-external"),
  );
  assert.deepEqual(JSON.parse(engineRequest.body), { authorized_engine_fingerprint: targetFingerprint });
  await enginePage.close();

  const outagePage = await browser.newPage({ viewport: { width: 390, height: 850 } });
  outagePage.on("pageerror", (error) => browserErrors.push(error.message));
  await outagePage.goto(`${origin}/?kind=outage`);
  assert.match(await outagePage.locator(".run-summary-heading").innerText(), /外部Codex復旧待ち/);
  assert.equal(await outagePage.locator(".run-engine-fingerprints").count(), 0);
  assert.equal(await outagePage.getByRole("button", { name: "外部Codexを再試行" }).isEnabled(), true);
  await outagePage.getByRole("button", { name: "外部Codexを再試行" }).evaluate((button) => button.click());
  let outageRequest;
  try {
    outageRequest = await waitForRecordedRequest(
      (item) => item.path.endsWith("run-outage/resume-external"),
    );
  } catch (error) {
    const diagnostics = await outagePage.evaluate(() => ({
      run: window.__researchState?.run,
      problems: window.__researchState?.problems,
      panel: window.__researchState?.panel,
      body: document.body.innerText,
    }));
    throw new Error(`${error.message}; diagnostics=${JSON.stringify(diagnostics)}; pageErrors=${JSON.stringify(browserErrors)}`);
  }
  assert.equal(outageRequest.body, "");
  await outagePage.close();

  const cancelPage = await browser.newPage({ viewport: { width: 390, height: 850 } });
  cancelPage.on("pageerror", (error) => browserErrors.push(error.message));
  cancelPage.on("dialog", (dialog) => dialog.accept());
  await cancelPage.goto(`${origin}/?kind=outage`);
  await cancelPage.getByRole("button", { name: "このRunを中止" }).evaluate((button) => button.click());
  await waitForRecordedRequest((item) => item.path.endsWith("run-outage/cancel"));
  assert.deepEqual(browserErrors, []);
  await cancelPage.close();

  console.log("V22 browser: external outage, exact engine authorization, and cancel controls passed");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
