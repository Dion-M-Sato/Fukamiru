// Isolated browser check: no production Project or Run is changed.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");
const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = await fs.mkdtemp(path.join(os.tmpdir(), "fukamiru-authoring-folder-"));
const preview = {
  folder_path: "notes",
  toc_path: "notes/目次.md",
  note_text: "# 目次\n\n- 序論\n- 分析\n\n# 本文\n\n## 序論\n\n最初の主張。\n\n## 分析\n\n次の根拠。\n",
  sections: [
    { title: "序論", source_path: "notes/序論.md" },
    { title: "分析", source_path: "notes/分析.md" },
  ],
  warnings: [],
};
const server = http.createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === "/") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(await fs.readFile(path.join(ui, "index.html")));
    } else if (pathname === "/src/app.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      const source = await fs.readFile(path.join(ui, "src/app.js"), "utf8");
      response.end(source.replace(
        /\nboot\(\);\s*$/,
        '\nstate.projects = [{ id: "project-1", name: "研究Project" }]; renderAuthoringEditor();',
      ));
    } else if (pathname === "/src/api.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      response.end(`export async function api(url) {
        if (url === "/api/projects/project-1/authoring-folders") return { folders: ["notes"] };
        if (url === "/api/projects/project-1/authoring-input-preview") return ${JSON.stringify(preview)};
        throw new Error("Unexpected API request: " + url);
      }`);
    } else if (pathname.startsWith("/src/")) {
      const filename = path.join(ui, pathname.slice(1));
      response.setHeader("Content-Type", filename.endsWith(".css") ? "text/css" : "text/javascript");
      response.end(await fs.readFile(filename));
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
  for (const width of [1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.locator("#authoring-folder option[value='notes']").waitFor({ state: "attached" });
    assert.equal(await page.locator("label[for='authoring-folder']").innerText(), "対象のフォルダを選択");
    await page.locator("#authoring-folder").selectOption("notes");
    await page.getByText("目次と本文の対応を確認しました").waitFor();
    assert.equal(await page.locator("#authoring-note").isDisabled(), true);
    assert.equal(await page.locator("#authoring-note-field").isHidden(), true);
    assert.equal(await page.locator("#authoring-folder-preview li").count(), 2);
    await page.locator("#authoring-folder-preview summary").click();
    assert.match(await page.locator("#authoring-folder-preview textarea").inputValue(), /次の根拠。/);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, `authoring-folder-${width}.png`), fullPage: true });
    await page.close();
  }
  console.log(`Authoring folder browser passed; screenshots: ${output}`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
