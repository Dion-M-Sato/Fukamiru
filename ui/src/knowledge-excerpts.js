// Optional UI-only Knowledge Search. Uses the existing read-only search API.
import { api } from "./api.js?v=source";
import { reviewPreviewDocument } from "./review-workspace.js?v=source";

export const EXCERPT_REPORT_FLOW = ["search", "group_by_source", "render_bibliography", "render_excerpts", "open_in_right_pane", "download_html"];
const escapeHTML = (value) => String(value ?? "")
  .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/g, "�")
  .replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);

function position(result) {
  const citation = result.citation || {};
  const start = citation.start_label || citation.start_page;
  const end = citation.end_label || citation.end_page;
  return start == null ? "位置情報なし" : `p. ${start}${end != null && end !== start ? `–${end}` : ""}`;
}

function bibliographyHTML(group) {
  const e = escapeHTML;
  const id = `${group.id}-bibliography`;
  const record = group.bibliography;
  if (!record) return `<section id="${id}"><h4 id="${id}-heading">書誌情報</h4>
    <p id="${id}-status">書誌情報が登録されていません。</p></section>`;
  const value = (key) => String(record[key] ?? "").trim();
  const authors = Array.isArray(record.authors) ? record.authors.map((author) => String(author).trim()).filter(Boolean) : [];
  const missing = [!authors.length && "著者", !value("title") && "タイトル",
    !value("publisher") && "出版者", !value("publication_date") && "刊行日"].filter(Boolean);
  const origin = { filename: "ファイル名からの仮登録", pdf_document_info: "PDFの文書情報",
    epub_opf: "EPUBの書誌情報", html_meta: "HTMLのメタデータ", markdown_front_matter: "Markdownの書誌情報" }[record.metadata_source]
    || (value("metadata_source") ? `取得元：${value("metadata_source")}` : "取得元未登録");
  const status = record.metadata_source === "filename" || record.metadata_status === "partial" || missing.length
    ? "登録情報（一部未登録）" : "登録情報";
  const details = [["掲載誌・収録先", "publication_title"], ["巻", "volume"], ["号", "issue"], ["掲載ページ", "pages"]]
    .filter(([, key]) => value(key)).map(([label, key]) => `${label}：${e(value(key))}`);
  const identifiers = [["DOI", "doi"], ["ISBN", "isbn"], ["URL", "url"]]
    .filter(([, key]) => value(key)).map(([label, key]) => `${label}：${e(value(key))}`);
  return `<section id="${id}"><h4 id="${id}-heading">書誌情報</h4>
    <p id="${id}-status">${status} · ${e(origin)}</p>
    <p id="${id}-authors"><strong>著者：</strong>${authors.length ? e(authors.join(" / ")) : "未登録"}</p>
    <p id="${id}-title"><strong>登録タイトル：</strong>${value("title") ? `<cite>${e(value("title"))}</cite>` : "未登録"}</p>
    <p id="${id}-publication">出版者：${e(value("publisher") || "未登録")} · 刊行日：${e(value("publication_date") || "未登録")}</p>
    ${details.length ? `<p id="${id}-details">${details.join(" · ")}</p>` : ""}
    ${identifiers.length ? `<p id="${id}-identifiers">${identifiers.join(" · ")}</p>` : ""}
    ${value("id") ? `<p id="${id}-record">書誌ID：${e(value("id"))}</p>` : ""}
  </section>`;
}

export function createExcerptReport(query, results, createdAt = new Date().toISOString()) {
  if (!String(query).trim() || !Array.isArray(results)) throw new Error("検索語と検索結果を確認してください。");
  const groups = new Map();
  const seen = new Set();
  let count = 0;
  results.forEach((result, index) => {
    const text = String(result.text ?? "");
    if (!text.trim()) return;
    const sourceKey = result.source_asset_id || result.path || result.document_unit_id || result.chunk_id || `unknown-${index}`;
    const key = result.chunk_id || `${sourceKey}\0${text}`;
    if (seen.has(key)) return;
    seen.add(key);
    if (!groups.has(sourceKey)) groups.set(sourceKey, {
      id: `source-${groups.size + 1}`, title: String(result.title || "無題の資料"),
      path: String(result.path || "資料パス未記録"), bibliography: null, excerpts: [],
    });
    const group = groups.get(sourceKey);
    if (!group.bibliography && result.bibliography) group.bibliography = structuredClone(result.bibliography);
    group.excerpts.push({
      id: `excerpt-${++count}`, text, position: position(result), rank: index + 1,
      title: String(result.title || "無題の資料"),
    });
  });
  const report = { id: crypto.randomUUID(), query: String(query).trim(), created_at: createdAt,
    groups: [...groups.values()], result_count: count, source_count: groups.size,
    retrieved_count: results.length };
  report.canonical_html = excerptReportHTML(report);
  if (typeof DOMParser !== "undefined") {
    const parsed = new DOMParser().parseFromString(report.canonical_html, "application/xml");
    const ids = [...parsed.querySelectorAll("[id]")].map((element) => element.getAttribute("id"));
    if (parsed.querySelector("parsererror") || parsed.documentElement.localName !== "article"
        || ids.length !== new Set(ids).size
        || [...parsed.querySelectorAll("a")].some((link) => !ids.includes(link.getAttribute("href").slice(1)))) {
      throw new Error("抜粋レポートのHTMLを検証できませんでした。");
    }
  }
  return report;
}

export function excerptReportHTML(report) {
  const e = escapeHTML;
  const groups = report.groups.map((group) => `
    <section id="${group.id}"><h3 id="${group.id}-title">${e(group.title)}</h3>
      ${bibliographyHTML(group)}
      <p id="${group.id}-path">${e(group.path)}</p>
      ${group.excerpts.map((excerpt) => `<section id="${excerpt.id}">
        <h4 id="${excerpt.id}-title">検索順位 ${excerpt.rank} · ${e(excerpt.position)}</h4>
        ${excerpt.title !== group.title ? `<p id="${excerpt.id}-unit">${e(excerpt.title)}</p>` : ""}
        <blockquote id="${excerpt.id}-quote"><p id="${excerpt.id}-text">${e(excerpt.text)}</p></blockquote>
      </section>`).join("")}
    </section>`).join("");
  return `<article id="excerpt-report" lang="ja">
    <section id="report-header"><h2 id="report-title">検索抜粋レポート：${e(report.query)}</h2>
      <p id="report-date">${e(new Date(report.created_at).toLocaleString("ja-JP"))}</p>
      <p id="report-scope">検索時点で取り込み済みの資料から、検索結果の原文を資料ごとに整理しています。AIによる要約は行っていません。</p>
      <p id="report-bibliography-scope">書誌情報はcorpusの登録値です。未登録の項目は推測で補っていません。</p>
    </section>
    <section id="report-overview"><h2 id="report-overview-title">検索結果の概要</h2>
      <p id="report-counts">検索語：${e(report.query)} · ${report.source_count}資料 · ${report.result_count}箇所</p>
      ${report.groups.length ? `<ol id="report-contents">${report.groups.map((group, index) =>
        `<li id="report-contents-${index + 1}"><a href="#${group.id}">${e(group.title)}</a> · ${group.excerpts.length}箇所</li>`).join("")}</ol>`
        : '<p id="report-empty">該当する検索結果がありません。検索語を変えて検索してください。</p>'}
      ${report.retrieved_count !== report.result_count ? '<p id="report-deduplication">重複した検索結果と空の本文は除いています。</p>' : ""}
    </section>
    <section id="report-sources"><h2 id="report-sources-title">資料別の抜粋</h2>${groups}</section>
  </article>`;
}

export function excerptDownloadDocument(report) {
  return `<!doctype html><html lang="ja"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
    <title>検索抜粋レポート：${escapeHTML(report.query)}</title>
    <style>body{font:16px/1.8 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 24px;color:#1b211a;background:white;overflow-wrap:anywhere}section{margin:24px 0}h2{font-size:1.4rem}h3{font-size:1.15rem}h4{font-size:1rem}section[id$="-bibliography"]{padding:12px 16px;border-left:2px solid #687866}section[id$="-bibliography"] p{margin:.4em 0}p[id$="-record"]{font-size:.8rem}blockquote{margin:16px 0;padding-left:16px;border-left:3px solid #687866;white-space:pre-wrap}a{color:#245524}</style>
    </head><body>${report.canonical_html}</body></html>`;
}

export function installKnowledgeExcerpts(doc = document) {
  const editor = doc.getElementById("editor-content");
  if (!editor || editor.dataset.excerptInstaller) return;
  editor.dataset.excerptInstaller = "installed";
  const workbench = doc.querySelector(".workbench");
  const reports = [];
  let selectedId = null;
  let busy = false;
  let message = "";
  let failed = false;
  let downloadURL = null;
  let previousPane = null;
  const byId = (id) => doc.getElementById(id);
  const pane = doc.createElement("section");
  pane.id = "knowledge-excerpts-inspector";
  pane.hidden = true;
  pane.setAttribute("aria-label", "選択した検索抜粋レポート");
  pane.innerHTML = `<header class="knowledge-excerpts-toolbar"><strong id="knowledge-excerpts-name"></strong>
    <div class="viewer-actions"><a id="knowledge-excerpts-download" class="icon-action">HTML保存</a>
      <button id="knowledge-excerpts-close" class="icon-action" type="button" aria-label="抜粋レポートを閉じて一覧へ戻る">×</button></div></header>
    <iframe id="knowledge-excerpts-frame" sandbox title="検索抜粋レポートの本文"></iframe>`;
  doc.querySelector(".inspector-content").append(pane);
  const otherPanes = ["inspector-default", "review-inspector", "draft-inspector", "search-report-inspector"];
  const releaseDownload = () => { if (downloadURL) URL.revokeObjectURL(downloadURL); downloadURL = null; };

  function close({ restore = true, focus = false } = {}) {
    if (pane.hidden) return;
    pane.hidden = true;
    workbench.classList.remove("knowledge-excerpts-open");
    byId("knowledge-excerpts-frame").srcdoc = "";
    releaseDownload();
    if (restore && previousPane) {
      otherPanes.forEach((id) => { if (byId(id) && id in previousPane.hidden) byId(id).hidden = previousPane.hidden[id]; });
      byId("inspector-title").textContent = previousPane.title;
    }
    previousPane = null;
    selectedId = null;
    render();
    if (focus) byId("knowledge-excerpts-query")?.focus();
  }

  function open(report) {
    if (pane.hidden) previousPane = { title: byId("inspector-title").textContent,
      hidden: Object.fromEntries(otherPanes.filter((id) => byId(id)).map((id) => [id, byId(id).hidden])) };
    const inspector = byId("inspector-pane");
    if (inspector.hidden || workbench.classList.contains("pane-right-hidden")) doc.querySelector('.pane-toggle[data-pane="right"]')?.click();
    otherPanes.forEach((id) => { if (byId(id)) byId(id).hidden = true; });
    workbench.classList.add("knowledge-excerpts-open");
    pane.hidden = false;
    selectedId = report.id;
    byId("inspector-title").textContent = "検索抜粋レポート";
    byId("knowledge-excerpts-name").textContent = report.query;
    byId("knowledge-excerpts-frame").srcdoc = reviewPreviewDocument(report.canonical_html)
      .replaceAll('href="#', 'href="about:srcdoc#')
      .replace("</head>", '<style>blockquote p{white-space:pre-wrap}h4{font-size:1rem;line-height:1.6}section[id$="-bibliography"]{padding:8px 16px;border-left:2px solid #3E433D}section[id$="-bibliography"] p{margin:.4em 0}p[id$="-record"]{font-size:.8rem;color:#A3AAA1}</style></head>');
    releaseDownload();
    downloadURL = URL.createObjectURL(new Blob([excerptDownloadDocument(report)], { type: "text/html;charset=utf-8" }));
    byId("knowledge-excerpts-download").href = downloadURL;
    byId("knowledge-excerpts-download").download = `knowledge-excerpts-${report.id}.html`;
    render();
    byId("knowledge-excerpts-close").focus({ preventScroll: true });
  }

  function render() {
    const target = byId("knowledge-excerpts-list");
    if (!target) return;
    byId("knowledge-excerpts-status").textContent = message;
    byId("knowledge-excerpts-status").classList.toggle("error-text", failed);
    byId("knowledge-excerpts-submit").disabled = busy;
    byId("knowledge-excerpts-submit").textContent = busy ? "抜粋を整理中…" : "検索して抜粋を整理";
    target.replaceChildren();
    if (!reports.length) target.innerHTML = '<p class="empty-state">検索語を入力して抜粋レポートを作成してください。</p>';
    reports.forEach((report) => {
      const button = doc.createElement("button");
      button.type = "button";
      button.className = "knowledge-excerpts-choice";
      button.classList.toggle("selected", selectedId === report.id);
      button.setAttribute("aria-pressed", String(selectedId === report.id));
      button.setAttribute("aria-controls", pane.id);
      const title = doc.createElement("strong"); title.textContent = report.query;
      const meta = doc.createElement("span");
      meta.textContent = `${new Date(report.created_at).toLocaleString("ja-JP")} · ${report.source_count}資料 · ${report.result_count}箇所 · 右ペインで読む`;
      button.append(title, meta);
      button.addEventListener("click", () => open(report));
      target.append(button);
    });
  }

  function mount() {
    const oldForm = byId("search-form");
    const article = oldForm?.closest("article");
    const heading = [...(article?.querySelectorAll("h2") || [])].find((element) => element.textContent.trim() === "Knowledge Search");
    if (!heading) return;
    while (heading.nextSibling) heading.nextSibling.remove();
    const section = doc.createElement("section");
    section.id = "knowledge-excerpts-workspace";
    section.innerHTML = `<p class="muted">検索結果の原文を資料ごとに整理し、登録済みの書誌情報・出典位置と合わせてレポートにします。</p>
      <form id="knowledge-excerpts-form" class="knowledge-excerpts-search">
        <label for="knowledge-excerpts-query">検索語<input id="knowledge-excerpts-query" name="q" required maxlength="1000" placeholder="資料内を検索"></label>
        <button id="knowledge-excerpts-submit" class="primary-action" type="submit">検索して抜粋を整理</button>
      </form><p id="knowledge-excerpts-status" role="status" class="muted"></p>
      <h3>抜粋レポート</h3><p class="muted">選択すると右ペインで読めます。このタブで直近10件を保持します。再読み込みで消えるため、残したいレポートはHTMLで保存してください。</p>
      <div id="knowledge-excerpts-list"></div>`;
    heading.after(section);
    byId("knowledge-excerpts-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      const query = String(new FormData(event.currentTarget).get("q") || "").trim();
      if (!query) return;
      busy = true; failed = false; message = "検索結果の抜粋を整理しています…"; render();
      try {
        const payload = await api(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
        const report = createExcerptReport(query, payload.results);
        reports.unshift(report); reports.splice(10);
        message = report.result_count ? "抜粋レポートを作成しました。一覧から開けます。" : "該当する検索結果はありません。検索語を変えて検索してください。";
      } catch (error) { failed = true; message = `抜粋レポートを作成できませんでした。${error.message}`; }
      finally { busy = false; render(); }
    });
    render();
  }

  byId("knowledge-excerpts-close").addEventListener("click", () => close({ focus: true }));
  doc.addEventListener("click", (event) => {
    const activity = event.target.closest?.(".activity-button[data-activity]");
    if (activity && activity.dataset.activity !== "rag") close();
  }, true);
  const viewerObserver = new MutationObserver(() => {
    if (!pane.hidden && otherPanes.some((id) => byId(id) && !byId(id).hidden)) close({ restore: false });
  });
  otherPanes.forEach((id) => { if (byId(id)) viewerObserver.observe(byId(id), { attributes: true, attributeFilter: ["hidden"] }); });
  const editorObserver = new MutationObserver(mount);
  editorObserver.observe(editor, { childList: true });
  mount();
  window.addEventListener("pagehide", () => { releaseDownload(); viewerObserver.disconnect(); editorObserver.disconnect(); }, { once: true });
}

if (typeof document !== "undefined" && document.querySelector("script[data-fukamiru-knowledge-excerpts]")) installKnowledgeExcerpts();
