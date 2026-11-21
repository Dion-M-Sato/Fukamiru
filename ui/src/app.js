import { ReviewDrafts, reviewPreviewDocument } from "./review-workspace.js?v=0.33.0";
import { api } from "./api.js";
import { corpusDuration, corpusPreflightProgress } from "./corpus-progress.js";
import { numericProgressSummary, runMeasuredProgress } from "./run-progress.js";
import {
  argumentProcessLabel,
  patchProcessingState,
  progressUnitLabel,
  runDisplayNodes,
  runExecutionState,
  runHasLiveProgress,
  runNodeLabel,
  runNodeStatusLabel,
  runNodeTechnicalName,
  terminalRunProgressState,
} from "./run-labels.js";
import {
  externalResumeRequestOptions,
  externalRunWaitState,
  externalWaitMarkup,
} from "./run-recovery.js";
import {
  PANE_LIMITS,
  PANE_NAMES,
  clampPaneSize,
  normalizePaneLayout,
  paneSizeFromKey,
} from "./pane-layout.js";
import {
  manuscriptFinalApprovalAllowed,
  manuscriptReferenceApprovalRequired,
  manuscriptResponseContractIncomplete,
  manuscriptRewriteWorkflowVersion,
  manuscriptSemanticResolutionRequired,
} from "./manuscript-policy.js";
import { approvedOutputReviews, translationSourceRuns } from "./workbench-lists.js";
import { readSelectedTabs, urlForSelectedTabs } from "./tab-navigation.js";
import {
  groupHumanReviewItems,
  humanReviewCategoryLabel,
  humanReviewItemLabel,
  humanReviewMarkdown,
} from "./human-review.js";

const state = {
  activity: "projects",
  panel: "problems",
  health: null,
  workflows: [],
  projects: [],
  archivedProjects: [],
  run: null,
  runs: [],
  translationRuns: [],
  selectedRunId: null,
  events: [],
  problems: [],
  searchReports: [],
  selectedSearchReportId: null,
  searchReportSelection: 0,
  searchReportBusy: false,
  searchReportStatus: "",
  corpus: null,
  corpusIndexProgress: null,
  corpusIndexEvents: [],
  corpusIndexNode: null,
  corpusIndexEstimate: null,
  corpusIndexEstimateLoaded: false,
  corpusRefreshInFlight: false,
  corpusDirectories: [],
  reviews: [],
  defects: [],
  eventSource: null,
  runProgress: null,
  runPollTimer: null,
  runPollInFlight: false,
  runListPollInFlight: false,
  manuscriptRewriteTimer: null,
  draftPreview: null,
  authoringFolderPreview: null,
  paneLayout: normalizePaneLayout(),
};

const byId = (id) => document.getElementById(id);

const PANE_LAYOUT_STORAGE_KEY = "fukamiru:pane-layout:v1";
const PANE_ELEMENTS = {
  left: "sidebar-pane",
  right: "inspector-pane",
  bottom: "bottom-pane",
};
const PANE_RESIZERS = {
  left: "left-pane-resizer",
  right: "right-pane-resizer",
  bottom: "bottom-pane-resizer",
};
const PANE_CSS_PROPERTIES = {
  left: "--sidebar-track",
  right: "--inspector-track",
  bottom: "--panel-track",
};
const PANE_LABELS = {
  left: "左",
  right: "右",
  bottom: "下",
};

function escapeText(value) {
  const span = document.createElement("span");
  span.textContent = String(value ?? "");
  return span.innerHTML;
}

function reviewAttribute(value) {
  return escapeText(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function loadPaneLayout() {
  try {
    return normalizePaneLayout(JSON.parse(localStorage.getItem(PANE_LAYOUT_STORAGE_KEY) || "{}"));
  } catch {
    return normalizePaneLayout();
  }
}

function savePaneLayout() {
  try {
    localStorage.setItem(PANE_LAYOUT_STORAGE_KEY, JSON.stringify(state.paneLayout));
  } catch {
    // The workspace remains usable when storage is unavailable.
  }
}

function paneElement(pane) {
  return byId(PANE_ELEMENTS[pane]);
}

function paneResizer(pane) {
  return byId(PANE_RESIZERS[pane]);
}

function paneIsRendered(pane) {
  const element = paneElement(pane);
  return Boolean(element && !element.hidden && getComputedStyle(element).display !== "none");
}

function currentPaneSize(pane) {
  const element = paneElement(pane);
  const rect = element?.getBoundingClientRect();
  const measured = pane === "bottom" ? rect?.height : rect?.width;
  if (measured > 0) return measured;
  return state.paneLayout.sizes[pane] || PANE_LIMITS[pane].min;
}

function paneDynamicMaximum(pane) {
  const workbench = document.querySelector(".workbench");
  if (!workbench) return PANE_LIMITS[pane].max;
  if (pane === "bottom") {
    const statusHeight = byId("version-status")?.closest(".status-bar")?.getBoundingClientRect().height || 32;
    return workbench.clientHeight - statusHeight - 160;
  }

  const activityWidth = document.querySelector(".activity-bar")?.getBoundingClientRect().width || 64;
  const otherPane = pane === "left" ? "right" : "left";
  const otherWidth = paneIsRendered(otherPane) ? currentPaneSize(otherPane) : 0;
  return workbench.clientWidth - activityWidth - otherWidth - 260;
}

function updatePaneResizer(pane, currentSize = null) {
  const resizer = paneResizer(pane);
  if (!resizer || !paneIsRendered(pane)) return;
  const maximum = paneDynamicMaximum(pane);
  resizer.setAttribute("aria-valuemin", String(PANE_LIMITS[pane].min));
  resizer.setAttribute("aria-valuemax", String(clampPaneSize(pane, maximum, maximum)));
  resizer.setAttribute("aria-valuenow", String(Math.round(currentSize ?? currentPaneSize(pane))));
}

function updatePaneResizers() {
  PANE_NAMES.forEach((pane) => updatePaneResizer(pane));
}

function applyPaneSize(pane, requestedSize, { persist = false } = {}) {
  const size = clampPaneSize(pane, requestedSize, paneDynamicMaximum(pane));
  state.paneLayout.sizes[pane] = size;
  document.querySelector(".workbench")?.style.setProperty(PANE_CSS_PROPERTIES[pane], `${size}px`);
  updatePaneResizer(pane, size);
  if (persist) savePaneLayout();
  return size;
}

function resetPaneSize(pane) {
  state.paneLayout.sizes[pane] = null;
  document.querySelector(".workbench")?.style.removeProperty(PANE_CSS_PROPERTIES[pane]);
  requestAnimationFrame(updatePaneResizers);
  savePaneLayout();
}

function applyPaneVisibility() {
  const workbench = document.querySelector(".workbench");
  if (!workbench) return;
  PANE_NAMES.forEach((pane) => {
    const visible = state.paneLayout.visible[pane];
    workbench.classList.toggle(`pane-${pane}-hidden`, !visible);
    const element = paneElement(pane);
    if (element) element.hidden = !visible;
    const resizer = paneResizer(pane);
    if (resizer) resizer.hidden = !visible;
    const toggle = document.querySelector(`.pane-toggle[data-pane="${pane}"]`);
    if (toggle) {
      const action = visible ? "非表示" : "表示";
      toggle.setAttribute("aria-pressed", String(visible));
      toggle.setAttribute("aria-label", `${PANE_LABELS[pane]}ペインを${action}`);
      toggle.title = `${PANE_LABELS[pane]}ペインを${action}`;
    }
  });
  requestAnimationFrame(updatePaneResizers);
}

function setPaneVisibility(pane, visible, { persist = true } = {}) {
  if (!PANE_NAMES.includes(pane)) return;
  state.paneLayout.visible[pane] = Boolean(visible);
  applyPaneVisibility();
  if (visible && paneIsRendered(pane) && state.paneLayout.sizes[pane] != null) {
    requestAnimationFrame(() => applyPaneSize(pane, state.paneLayout.sizes[pane]));
  }
  if (persist) savePaneLayout();
}

function finishPaneResize(pane, resizer, pointerId, move, finish) {
  window.removeEventListener("pointermove", move);
  window.removeEventListener("pointerup", finish, true);
  window.removeEventListener("pointercancel", finish, true);
  resizer.removeEventListener("lostpointercapture", finish);
  if (resizer.hasPointerCapture(pointerId)) resizer.releasePointerCapture(pointerId);
  resizer.classList.remove("is-dragging");
  document.body.classList.remove("pane-resizing", "pane-resizing-horizontal", "pane-resizing-vertical");
  updatePaneResizer(pane);
  savePaneLayout();
}

function beginPaneResize(event) {
  if (event.button !== 0) return;
  const resizer = event.currentTarget;
  const pane = resizer.dataset.pane;
  if (!state.paneLayout.visible[pane]) return;
  const pointerId = event.pointerId;
  const startSize = currentPaneSize(pane);
  const startPoint = pane === "bottom" ? event.clientY : event.clientX;
  const axisClass = pane === "bottom" ? "pane-resizing-horizontal" : "pane-resizing-vertical";

  const move = (moveEvent) => {
    const point = pane === "bottom" ? moveEvent.clientY : moveEvent.clientX;
    const delta = point - startPoint;
    const nextSize = pane === "left"
      ? startSize + delta
      : startSize - delta;
    applyPaneSize(pane, nextSize);
  };
  const finish = () => finishPaneResize(pane, resizer, pointerId, move, finish);

  event.preventDefault();
  resizer.focus({ preventScroll: true });
  resizer.setPointerCapture(pointerId);
  resizer.classList.add("is-dragging");
  document.body.classList.add("pane-resizing", axisClass);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", finish, true);
  window.addEventListener("pointercancel", finish, true);
  resizer.addEventListener("lostpointercapture", finish);
}

function resizePaneFromKeyboard(event) {
  const pane = event.currentTarget.dataset.pane;
  const maximum = paneDynamicMaximum(pane);
  const nextSize = paneSizeFromKey(pane, currentPaneSize(pane), event.key, {
    shiftKey: event.shiftKey,
    maximum,
  });
  if (nextSize == null) return;
  event.preventDefault();
  applyPaneSize(pane, nextSize, { persist: true });
}

function bindPaneLayout() {
  state.paneLayout = loadPaneLayout();
  applyPaneVisibility();
  PANE_NAMES.forEach((pane) => {
    if (paneIsRendered(pane) && state.paneLayout.sizes[pane] != null) {
      applyPaneSize(pane, state.paneLayout.sizes[pane]);
    }
  });

  document.querySelectorAll(".pane-toggle").forEach((button) => {
    button.addEventListener("click", () => {
      const pane = button.dataset.pane;
      setPaneVisibility(pane, !state.paneLayout.visible[pane]);
    });
  });
  PANE_NAMES.forEach((pane) => {
    const resizer = paneResizer(pane);
    resizer?.addEventListener("pointerdown", beginPaneResize);
    resizer?.addEventListener("keydown", resizePaneFromKeyboard);
    resizer?.addEventListener("dblclick", () => resetPaneSize(pane));
  });

  if ("ResizeObserver" in window) {
    const paneResizeObserver = new ResizeObserver((entries) => {
      entries.forEach((entry) => {
        const pane = PANE_NAMES.find((name) => paneElement(name) === entry.target);
        if (!pane || !state.paneLayout.visible[pane]) return;
        const rect = entry.target.getBoundingClientRect();
        const size = pane === "bottom" ? rect.height : rect.width;
        updatePaneResizer(pane, size);
      });
    });
    PANE_NAMES.forEach((pane) => {
      const element = paneElement(pane);
      if (element) paneResizeObserver.observe(element);
    });
  }

  let resizeFrame = null;
  window.addEventListener("resize", () => {
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      PANE_NAMES.forEach((pane) => {
        if (paneIsRendered(pane) && state.paneLayout.sizes[pane] != null) {
          applyPaneSize(pane, state.paneLayout.sizes[pane]);
        }
      });
      updatePaneResizers();
      savePaneLayout();
      resizeFrame = null;
    });
  });
}

function renderSystemStatus() {
  const health = state.health;
  const list = byId("system-status");
  if (!health) {
    list.innerHTML = "<div><dt>Status</dt><dd>Unavailable</dd></div>";
    return;
  }
  list.innerHTML = `
    <div><dt>Status</dt><dd>${escapeText(health.status)}</dd></div>
    <div><dt>State DB</dt><dd>${escapeText(health.state_db)}</dd></div>
    <div><dt>Knowledge DB</dt><dd>${escapeText(health.knowledge_db)}</dd></div>
    <div><dt>Retrieval</dt><dd>${escapeText(health.retrieval_backend)}</dd></div>
    <div><dt>Lexical RAG</dt><dd>${escapeText(health.lexical_retrieval)}</dd></div>
    <div><dt>Semantic RAG</dt><dd>${escapeText(health.semantic_retrieval.index)}</dd></div>
    <div><dt>Embedding</dt><dd>${escapeText(health.semantic_retrieval.dependencies)}</dd></div>
    <div><dt>Embedding device</dt><dd>${escapeText(health.actual_embedding_device)}</dd></div>
    <div><dt>Configured device</dt><dd>${escapeText(health.configured_device)}</dd></div>
    <div><dt>CUDA</dt><dd>${health.cuda_available ? escapeText(health.gpu_name) : "unavailable"}</dd></div>
    <div><dt>GPU Worker</dt><dd>${escapeText(health.gpu_worker?.status || "unknown")}</dd></div>
    <div><dt>Ingestion CPU</dt><dd>${escapeText(health.workers?.lanes?.ingestion_cpu?.status || "unknown")}</dd></div>
    <div><dt>Embedding GPU</dt><dd>${escapeText(health.workers?.lanes?.embedding_gpu?.status || "unknown")}</dd></div>
    <div><dt>Codex Remote</dt><dd>${escapeText(health.workers?.lanes?.codex_remote?.status || "unknown")}</dd></div>
    <div><dt>Local LLM GPU</dt><dd>${escapeText(health.workers?.lanes?.local_llm_gpu?.status || "unknown")}</dd></div>
    <div><dt>GPU Mutex</dt><dd>${escapeText(health.gpu_worker?.mutex?.owner || "free")}</dd></div>
    <div><dt>Worker</dt><dd>${escapeText(health.worker)}</dd></div>
    <div><dt>Codex</dt><dd>${health.codex.available ? "available" : "not found"}</dd></div>
    <div><dt>llm-jp</dt><dd>${escapeText(health.local_models?.llm_jp?.status || (health.models.llm_jp ? "configured" : "not found"))}</dd></div>
    <div><dt>Gaperon</dt><dd>${escapeText(health.local_models?.gaperon?.status || (health.models.gaperon ? "configured" : "not configured"))}</dd></div>
    <div><dt>Document review</dt><dd>${health.features?.html_preview ? "HTML · ふかみる共通スタイル" : "unavailable"}</dd></div>
  `;
  byId("tab-status-dot").className = `status-dot ${health.status === "ready" ? "ready" : "error"}`;
  byId("connection-status").textContent = health.status.toUpperCase();
  byId("version-status").textContent = `ふかみる ${health.version}`;
}

function formatElapsed(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds || 0)));
  if (value < 60) return `${value}秒`;
  const minutes = Math.floor(value / 60);
  const remainder = value % 60;
  return remainder ? `${minutes}分${remainder}秒` : `${minutes}分`;
}

function ingestionActivity(progress, activeNodes = [], events = state.events) {
  if (progress?.kind === "ingestion_activity") {
    const activity = {
      ...progress,
      details: { ...(progress.details || {}) },
    };
    const observed = Date.parse(progress.updated_at || "");
    const running = activeNodes.some((item) => item.status === "running");
    if (
      running
      && activity.details.elapsed_seconds != null
      && Number.isFinite(observed)
    ) {
      activity.details.elapsed_seconds = Number(activity.details.elapsed_seconds)
        + Math.max(0, (Date.now() - observed) / 1000);
    }
    return activity;
  }
  const node = activeNodes.find((item) => item.node_id === "ensure_ingested");
  const activityEvent = [...events]
    .reverse()
    .find((item) => item.type === "ingestion.progress");
  if (activityEvent?.payload) {
    const activity = {
      ...activityEvent.payload,
      details: { ...(activityEvent.payload.details || {}) },
    };
    const observed = Date.parse(activityEvent.created_at || "");
    if (
      node
      && activity.details.elapsed_seconds != null
      && Number.isFinite(observed)
    ) {
      activity.details.elapsed_seconds = Number(activity.details.elapsed_seconds)
        + Math.max(0, (Date.now() - observed) / 1000);
    }
    return activity;
  }
  if (!node) return null;
  const started = Date.parse(node.started_at || "");
  const elapsed = Number.isFinite(started) ? (Date.now() - started) / 1000 : 0;
  return {
    kind: "ingestion_activity",
    phase: "ensure_ingested",
    label: "資料の取り込み・既存構造の置換を実行中",
    indeterminate: true,
    details: { elapsed_seconds: elapsed },
  };
}

function ingestionActivityDetails(activity) {
  const details = activity?.details || {};
  const values = [];
  if (activity?.stage && activity?.stage_total) {
    values.push(`段階 ${activity.stage}/${activity.stage_total}`);
  }
  if (activity?.completed != null && activity?.total != null) {
    values.push(`${activity.completed}/${activity.total} ${activity.unit || "items"}`);
  }
  if (details.existing_chunks != null) {
    values.push(`置換対象 ${Number(details.existing_chunks).toLocaleString()} chunks`);
  }
  if (details.corpus_chunks != null) {
    values.push(`Corpus全体 ${Number(details.corpus_chunks).toLocaleString()} chunks`);
  }
  if (details.activity_count != null) {
    values.push(`活動 ${Number(details.activity_count).toLocaleString()}`);
  }
  if (details.elapsed_seconds != null) {
    values.push(`経過 ${formatElapsed(details.elapsed_seconds)}`);
  }
  return values.join(" · ");
}

function renderRunStatus() {
  const list = byId("run-status");
  const statusSummary = byId("status-run-summary");
  const statusDot = document.querySelector(".status-run-dot");
  const trackedRuns = state.runs.length ? state.runs : state.run ? [state.run] : [];
  if (!trackedRuns.length) {
    list.innerHTML = "<div><dt>Runs</dt><dd>None</dd></div>";
    if (statusSummary) statusSummary.textContent = "No active Runs";
    statusDot?.classList.remove("active");
    return;
  }
  const activeCount = trackedRuns.filter((item) =>
    ["queued", "running", "waiting_human"].includes((item.run || item).status),
  ).length;
  if (statusSummary) {
    statusSummary.textContent = activeCount
      ? `${activeCount} active ${activeCount === 1 ? "Run" : "Runs"}`
      : `All idle · ${trackedRuns.length} recent Runs`;
  }
  statusDot?.classList.toggle("active", activeCount > 0);
  list.innerHTML = `
    <div><dt>Runs</dt><dd>${activeCount} active · ${trackedRuns.length} displayed</dd></div>
    ${trackedRuns.slice(0, 8).map((item) => {
      const run = item.run || item;
      const progress = item.progress;
      const source = runSourceLabel(run);
      const execution = runExecutionState(item);
      const liveProgress = runHasLiveProgress(item) ? progress : null;
      const progressText = liveProgress?.percentage != null
        ? ` · ${progress.percentage}%`
        : liveProgress?.completed != null
          ? ` · ${progress.completed} completed`
          : "";
      const selected = run.id === state.selectedRunId ? " · selected" : "";
      return `<div><dt>${escapeText(execution.statusLabel)}</dt><dd title="${escapeText(run.id)}">${escapeText(source)} · ${escapeText(execution.detail)}${escapeText(progressText)}${escapeText(selected)}</dd></div>`;
    }).join("")}
  `;
}

function runSourceLabel(run) {
  const input = run?.input || {};
  const sourcePath = String(input.source_path || "").replace(/^["']|["']$/g, "");
  if (sourcePath) return sourcePath.split(/[\\/]/).pop() || sourcePath;
  const project = state.projects.find((item) => item.id === run?.project_id);
  if (project?.name) return project.name;
  if (input.document_id) return String(input.document_id);
  if (input.root_path) return String(input.root_path).split(/[\\/]/).pop() || input.root_path;
  return workflowDisplayName(run?.workflow_name) || run?.id || "Run";
}

function workflowDisplayName(name) {
  return name === "authoring" || name === "manuscript_generator"
    ? "Manuscript Generator"
    : name || "";
}

function sidebarTemplate(activity) {
  const pendingReviewCount = state.reviews.filter((item) => item.status === "open").length;
  const definitions = {
    projects: [
      "Library",
      "Register project",
      ["Projects", state.projects[0]?.name || "No registered project"],
    ],
    translation: ["Translation", "New translation", ["翻訳ファイル", "翻訳したファイルはありません"]],
    authoring: ["Manuscript Generator", "New manuscript", ["Active run", ["authoring", "manuscript_generator"].includes(state.run?.run?.workflow_name) ? state.run.run.id : "Ready"]],
    literature: ["Literature", "Start discovery", ["Collections", "No collection selected"]],
    rag: ["Knowledge Base", "Sync corpus sources", ["Indexes", state.health?.semantic_retrieval?.index || "Unavailable"]],
    review: [
      "レビュー待ち一覧",
      "レビュー待ち一覧を開く",
      ["Pending", pendingReviewCount ? `${pendingReviewCount}件` : "No pending review"],
    ],
    settings: ["Settings", "Open local settings", ["Profile", "local.toml"]],
  };
  return definitions[activity];
}

function renderSidebar() {
  const [title, action, group] = sidebarTemplate(state.activity);
  byId("sidebar-title").textContent = title;
  const content = byId("sidebar-content");
  content.innerHTML = "";
  const actionButton = document.createElement("button");
  actionButton.className = "sidebar-action";
  actionButton.type = "button";
  actionButton.textContent = `＋ ${action}`;
  actionButton.addEventListener("click", () => {
    if (state.activity === "translation") renderTranslationEditor();
    if (state.activity === "literature") renderLiteratureEditor();
    if (state.activity === "authoring") renderAuthoringEditor();
    if (state.activity === "projects") renderProjectEditor();
    if (state.activity === "rag") renderRagEditor();
    if (state.activity === "review") renderReviewEditor();
  });
  content.append(actionButton);
  const heading = document.createElement("div");
  heading.className = "sidebar-group-title";
  heading.textContent = group[0];
  content.append(heading);
  if (state.activity === "projects") {
    if (!state.projects.length) {
      const empty = document.createElement("div");
      empty.className = "sidebar-empty";
      empty.textContent = "No registered project";
      content.append(empty);
    }
    state.projects.forEach((project) => {
      const item = document.createElement("button");
      item.className = "sidebar-item";
      item.type = "button";
      item.innerHTML = `<span class="sidebar-item-title">${escapeText(project.name)}</span><span class="sidebar-item-meta">${escapeText(project.root_path)}</span>`;
      item.title = project.root_path;
      item.addEventListener("click", () => renderProjectDetails(project));
      content.append(item);
    });
  } else if (state.activity === "translation") {
    const translationRuns = translationSourceRuns(state.translationRuns);
    if (!translationRuns.length) {
      const empty = document.createElement("div");
      empty.className = "sidebar-empty";
      empty.textContent = group[1];
      content.append(empty);
    }
    translationRuns.forEach((run) => {
      const button = document.createElement("button");
      button.className = "sidebar-item translation-source-item";
      button.type = "button";
      if (run.id === state.selectedRunId) button.classList.add("active");
      button.innerHTML = `<span class="sidebar-item-title">${escapeText(runSourceLabel(run))}</span>`;
      button.title = runSourceLabel(run);
      button.addEventListener("click", () => selectTrackedRun(run.id));
      content.append(button);
    });
  } else {
    const item = document.createElement("button");
    item.className = "sidebar-item";
    item.type = "button";
    item.textContent = group[1];
    content.append(item);
  }
}

async function renderRagEditor() {
  byId("tab-title").textContent = "Knowledge Base";
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <h1>資料取り込みと検索</h1>
      <p class="lede">managed_sourcesとimport_stagingを差分走査し、SourceSpan・Chunk・BGE-M3埋め込み・FAISS indexを一括構築します。</p>
      <section class="corpus-sync-card">
        <div>
          <h2>Managed corpus sync</h2>
          <p class="muted">EPUB / PDF / TXT / Markdown / HTMLを、サブフォルダも含めて処理します。</p>
        </div>
        <form id="corpus-sync-form" class="corpus-sync-form">
          <label for="corpus-root-path">Target folder</label>
          <input id="corpus-root-path" name="root_path" required list="corpus-directory-options"
            value="${escapeText(state.health?.corpus_sources?.default_root || state.health?.managed_sources?.path || "")}"
            placeholder="同期する対象フォルダ">
          <datalist id="corpus-directory-options"></datalist>
          <button class="primary-action" type="submit">このフォルダ以下のRAGを同期・構築</button>
        </form>
      </section>
      <div id="corpus-sync-status"><p class="empty-state">同期履歴を読み込んでいます。</p></div>
      <details class="single-ingestion">
        <summary>単一資料だけを取り込む</summary>
      <form id="ingestion-form" class="form-grid compact-form">
        <div class="field full">
          <label for="ingestion-path">Source file</label>
          <input id="ingestion-path" name="source_path" required placeholder="C:\\path\\to\\sources\\book.epub">
        </div>
        <div class="field">
          <label for="ingestion-language">Language</label>
          <select id="ingestion-language" name="source_language">
            <option value="auto">Auto</option><option value="fr">French</option>
            <option value="en">English</option><option value="ja">Japanese</option>
            <option value="ru">Russian</option><option value="zh">Chinese</option>
            <option value="ko">Korean</option><option value="de">German</option>
          </select>
        </div>
        <div class="field">
          <label for="material-role">Material role</label>
          <select id="material-role" name="material_role">
            <option value="unknown">Unknown</option><option value="primary">Primary</option>
            <option value="secondary">Secondary</option><option value="reference">Reference</option>
          </select>
        </div>
        <button class="primary-action" type="submit">取り込みRunを開始</button>
      </form>
      </details>
      <h2>Knowledge Search</h2>
      <p class="muted">関連する資料の本文を読み、内容を文章で報告します。根拠の引用と、使用した文献の書誌を確認できます。</p>
      <form id="search-form" class="search-row">
        <label class="search-query-field">検索語・調べたいこと
          <input name="q" required maxlength="1000" placeholder="資料内を検索">
        </label>
        <button class="primary-action" type="submit" ${state.searchReportBusy ? "disabled" : ""}>${state.searchReportBusy ? "レポート作成中…" : "検索してレポートを作成"}</button>
      </form>
      <p id="search-report-status" role="status" class="muted"></p>
      <h3>検索レポート</h3>
      <p class="muted">選択すると右ペインで読めます。最新50件を表示します。</p>
      <div id="search-results" class="search-results"></div>
    </article>`;
  byId("ingestion-form").addEventListener("submit", startIngestion);
  byId("corpus-sync-form").addEventListener("submit", startCorpusSync);
  byId("search-form").addEventListener("submit", searchKnowledge);
  renderSearchReports();
  try {
    await Promise.all([refreshCorpus(), refreshCorpusDirectories(), refreshSearchReports()]);
  } catch (error) {
    addProblem(error.message);
  }
}

function renderCorpusStatus() {
  const target = byId("corpus-sync-status");
  if (!target) return;
  const batch = state.corpus?.batch;
  if (!batch) {
    target.innerHTML = '<p class="empty-state">まだCorpus同期は実行されていません。</p>';
    return;
  }
  const processed = Number(batch.ready_count) + Number(batch.reused_count)
    + Number(batch.ocr_required_count) + Number(batch.failed_count);
  const percent = batch.discovered_count ? Math.round(processed / batch.discovered_count * 100) : 0;
  const failedItems = (state.corpus.items || []).filter((item) => item.status === "failed" || item.status === "ocr_required");
  const ocrJobs = state.corpus.ocr_jobs || [];
  const ocrCounts = ocrJobs.reduce((counts, job) => {
    counts[job.status] = (counts[job.status] || 0) + 1;
    return counts;
  }, {});
  const activeOcrJobs = ocrJobs.filter((job) =>
    ["needs_language", "queued", "staged", "processing", "needs_review"].includes(job.status));
  const indexProgress = state.corpusIndexProgress || (
    batch.embedding_total_count
      ? {
          embedded_count: batch.embedding_completed_count,
          total_count: batch.embedding_total_count,
          chunks_per_second: batch.embedding_chunks_per_second,
          eta_seconds: batch.embedding_eta_seconds,
          batch_size: batch.embedding_batch_size,
          actual_embedding_device: batch.actual_embedding_device,
          average_tokens_per_chunk: batch.embedding_average_tokens_per_chunk,
          tokens_per_second: batch.embedding_tokens_per_second,
          padded_token_count: batch.embedding_padded_token_count,
          padding_token_count: batch.embedding_padding_token_count,
          padding_ratio: batch.embedding_padding_ratio,
          tokenization_seconds: batch.embedding_tokenization_seconds,
          gpu_inference_seconds: batch.embedding_gpu_inference_seconds,
          db_write_seconds: batch.embedding_db_write_seconds,
          gpu_utilization_percent: batch.embedding_gpu_utilization_percent,
          vram_used_mib: batch.embedding_vram_used_mib,
          vram_total_mib: batch.embedding_vram_total_mib,
          cpu_utilization_percent: batch.embedding_cpu_utilization_percent,
        }
      : null
  );
  const rate = Number(indexProgress?.chunks_per_second || 0);
  const etaSeconds = Number(indexProgress?.eta_seconds || 0);
  const etaMinutes = etaSeconds > 0 ? Math.ceil(etaSeconds / 60) : 0;
  const healthBuild = state.health?.semantic_retrieval?.build || {};
  const metricSource = { ...healthBuild, ...(indexProgress || {}) };
  const latestBatch = metricSource.latest_batch || {};
  const semanticHealth = state.health?.semantic_retrieval || {};
  const gpuInferenceTiming = latestBatch.gpu_timing_sampled === 0 || latestBatch.gpu_timing_sampled === false
    ? "not sampled"
    : `${(Number(latestBatch.gpu_inference_seconds || 0) * 1000).toFixed(2)}ms`;
  const gpuPercent = Number(metricSource.gpu_utilization_percent ?? metricSource.gpu_utilization_avg ?? 0);
  const cpuPercent = Number(metricSource.cpu_utilization_percent ?? metricSource.cpu_utilization_avg ?? 0);
  const vramUsed = Number(metricSource.vram_used_mib ?? metricSource.vram_used_avg_mib ?? 0);
  const vramTotal = Number(metricSource.vram_total_mib || 0);
  const preflight = indexProgress?.phase === "preflight_retrieval_groups"
    ? corpusPreflightProgress(state.corpusIndexEvents, state.corpusIndexNode,
      state.corpusIndexEstimate, batch.run_id) : null;
  const preflightLine = preflight
    ? `<p>事前検査：${preflight.completed.toLocaleString()}グループ確認済み
        ${preflight.total ? ` / 推定${preflight.total.toLocaleString()}（約${preflight.percent.toFixed(1)}%）` : " · 総数を推計中"}</p>
       <progress max="100" ${preflight.percent !== null ? `value="${preflight.percent}"` : ""}></progress>
       <p class="muted">経過${corpusDuration(preflight.elapsedSeconds)}
         · ${preflight.rate !== null ? `${Math.round(preflight.rate * 60).toLocaleString()}グループ/分（直近${corpusDuration(preflight.measuredSeconds)}）` : "速度を計測中"}
         ${preflight.etaHigh !== null ? ` · この検査の残り目安 ${corpusDuration(preflight.etaLow)}〜${corpusDuration(preflight.etaHigh)}` : ""}</p>
       ${preflight.total ? `<p class="muted">総数・割合・残時間は${preflight.sampleDocuments}資料の標本による概算です。資料の偏りで変動します。Embedding以降の時間は含みません。</p>` : ""}`
    : "";
  const indexLine = batch.status === "indexing" && preflight
    ? preflightLine
    : batch.status === "indexing" && indexProgress
    ? `<p class="muted">Embedding ${indexProgress.embedded_count} / ${indexProgress.total_count} chunks
        · ${rate.toFixed(2)} chunks/sec · ETA ${etaMinutes} min · batch ${indexProgress.batch_size || "?"}
        · device ${escapeText(indexProgress.actual_embedding_device || "unknown")}
        · dtype ${escapeText(semanticHealth.actual_embedding_dtype || "unknown")}
        · attention ${escapeText(semanticHealth.actual_attention_backend || "unknown")}
        · strategy ${escapeText(semanticHealth.configured_batch_strategy || "fixed")}</p>
       <div class="corpus-counts embedding-metrics">
         <span>avg tokens ${Number(metricSource.average_tokens_per_chunk || 0).toFixed(1)}</span>
         <span>${Number(metricSource.tokens_per_second || 0).toFixed(1)} tokens/sec</span>
         <span>padding ratio ${Number(metricSource.padding_ratio || 0).toFixed(3)}x</span>
       </div>
       <div class="embedding-resource-meters">
         <label>GPU ${gpuPercent.toFixed(1)}% <meter min="0" max="100" value="${gpuPercent}"></meter></label>
         <label>VRAM ${vramUsed.toFixed(0)} / ${vramTotal.toFixed(0)} MiB <meter min="0" max="${vramTotal || 1}" value="${vramUsed}"></meter></label>
         <label>Process CPU ${cpuPercent.toFixed(1)}% <meter min="0" max="100" value="${cpuPercent}"></meter></label>
       </div>
       <div class="corpus-counts embedding-timings" title="Latest encoded batch">
         <span>actual tokens ${Number(latestBatch.actual_token_count ?? latestBatch.token_count ?? 0)}</span>
         <span>padded tokens ${Number(latestBatch.padded_token_count || 0)}</span>
         <span>tokenize ${(Number(latestBatch.tokenization_seconds || 0) * 1000).toFixed(2)}ms</span>
         <span>GPU inference ${gpuInferenceTiming}</span>
         <span>DB writes ${(Number(latestBatch.db_write_seconds || 0) * 1000).toFixed(2)}ms</span>
       </div>`
    : "";
  target.innerHTML = `
    <section class="corpus-status">
      <div class="corpus-status-line"><strong>${escapeText(batch.status)}</strong><span>${processed} / ${batch.discovered_count}</span></div>
      <p class="muted corpus-root-path">${escapeText((batch.source_roots || [batch.root_path]).join(" / "))}</p>
      <progress max="100" value="${percent}">${percent}%</progress>
      <div class="corpus-counts">
        <span>ready ${batch.ready_count}</span><span>reused ${batch.reused_count}</span>
        <span>OCR ${batch.ocr_required_count}</span><span>failed ${batch.failed_count}</span>
      </div>
      ${ocrJobs.length ? `<div class="corpus-counts" aria-label="OCRジョブ状態">
        <span>OCR完了 ${ocrCounts.completed || 0}</span>
        <span>言語確認 ${ocrCounts.needs_language || 0}</span>
        <span>実行待ち ${ocrCounts.queued || 0}</span>
        <span>実行中 ${ocrCounts.processing || 0}</span>
        <span>失敗 ${ocrCounts.failed || 0}</span>
      </div>` : ""}
      ${indexLine}
      ${activeOcrJobs.length ? `<details><summary>OCR ${activeOcrJobs.length}件</summary>${activeOcrJobs.map((job) => `<p class="muted">${escapeText(job.status)} · ${escapeText(job.source_language || "und")} · ${escapeText(job.engine || "未選択")} · ${escapeText(job.language_origin || "unresolved")}${job.language_confidence == null ? "" : ` (${(Number(job.language_confidence) * 100).toFixed(0)}%)`}${job.language_decision?.method ? ` · ${escapeText(job.language_decision.method)}` : ""} · ${escapeText(job.source_path)}</p>`).join("")}</details>` : ""}
      ${failedItems.length ? `<details><summary>要確認 ${failedItems.length}件</summary>${failedItems.map((item) => `<p class="error-text">${escapeText(item.relative_path)} — ${escapeText(item.error?.message || item.status)}</p>`).join("")}</details>` : ""}
    </section>`;
}

async function refreshCorpus() {
  if (state.corpusRefreshInFlight) return state.corpus;
  state.corpusRefreshInFlight = true;
  try {
  state.corpus = await api("/api/corpus/latest");
  const batch = state.corpus?.batch;
  if (batch?.status === "indexing" && batch.run_id) {
    const snapshot = await api(`/api/runs/${encodeURIComponent(batch.run_id)}/progress?events_limit=200`);
    state.corpusIndexEvents = snapshot.events || [];
    state.corpusIndexNode = (snapshot.active_nodes || snapshot.current_nodes || [])
      .find((node) => node.node_id === "build_semantic_index");
    const latest = state.corpusIndexEvents.filter((event) => event.type === "corpus.index.progress").at(-1);
    if (latest) state.corpusIndexProgress = latest.payload;
    if (!state.corpusIndexEstimateLoaded) {
      state.corpusIndexEstimateLoaded = true;
      try { state.corpusIndexEstimate = await api("/corpus-index-estimate.json"); } catch { /* optional */ }
    }
  }
  renderCorpusStatus();
  return state.corpus;
  } finally {
    state.corpusRefreshInFlight = false;
  }
}

async function refreshCorpusDirectories() {
  const payload = await api("/api/corpus/directories");
  state.corpusDirectories = payload.directories || [];
  const datalist = byId("corpus-directory-options");
  if (datalist) {
    datalist.innerHTML = state.corpusDirectories
      .map((path) => `<option value="${escapeText(path)}"></option>`)
      .join("");
  }
  return payload;
}

async function startCorpusSync(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const rootPath = String(new FormData(form).get("root_path") || "").trim();
    const created = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        workflow_name: "corpus_sync",
        inputs: { root_path: rootPath },
      }),
    });
    state.run = created;
    state.events = [];
    state.corpusIndexProgress = null;
    renderRunStatus();
    connectToRun(created.run_id);
    selectPanel("run");
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function startIngestion(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const inputs = Object.fromEntries(new FormData(event.currentTarget).entries());
    const created = await api("/api/runs", {
      method: "POST", body: JSON.stringify({ workflow_name: "ingestion", inputs }),
    });
    state.run = created;
    state.events = [];
    renderRunStatus();
    connectToRun(created.run_id);
    selectPanel("run");
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function searchKnowledge(event) {
  event.preventDefault();
  if (state.searchReportBusy) return;
  const query = String(new FormData(event.currentTarget).get("q") || "").trim();
  if (!query) return;
  state.searchReportBusy = true;
  state.searchReportStatus = "関連資料を選び、本文・書誌を確認して報告をまとめています…";
  renderSearchReports();
  try {
    const report = await api("/api/search-reports", {
      method: "POST", body: JSON.stringify({ query, limit: 100 }),
    });
    state.searchReports = [report, ...state.searchReports.filter((item) => item.id !== report.id)].slice(0, 50);
    state.searchReportStatus = report.result_count
      ? "レポートを保存しました。一覧から開けます。"
      : "報告の根拠が不足しています。レポートを開くと確認できなかった点を読めます。";
    renderSearchReports();
  } catch (error) {
    state.searchReportStatus = `レポートを作成できませんでした。${error.message}`;
    addProblem(error.message);
  } finally {
    state.searchReportBusy = false;
    renderSearchReports();
  }
}

async function refreshSearchReports() {
  const reports = await api("/api/search-reports");
  state.searchReports = reports;
  renderSearchReports();
}

function renderSearchReports() {
  const target = byId("search-results");
  if (!target) return;
  byId("search-report-status").textContent = state.searchReportStatus;
  const button = byId("search-form").querySelector("button[type='submit']");
  button.disabled = state.searchReportBusy;
  button.textContent = state.searchReportBusy ? "レポート作成中…" : "検索してレポートを作成";
  target.replaceChildren();
  if (!state.searchReports.length) {
    target.innerHTML = '<p class="empty-state">まだ検索レポートはありません。検索語を入力して作成してください。</p>';
  }
  state.searchReports.forEach((report) => {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.className = "search-report-choice";
    choice.classList.toggle("selected", report.id === state.selectedSearchReportId);
    choice.setAttribute("aria-pressed", String(report.id === state.selectedSearchReportId));
    choice.setAttribute("aria-controls", "search-report-inspector");
    const date = new Date(report.created_at).toLocaleString("ja-JP");
    const title = document.createElement("strong");
    title.textContent = report.query;
    const meta = document.createElement("span");
    meta.textContent = `${date} · ${report.source_count}資料 · ${report.result_count}箇所 · 右ペインで読む`;
    choice.append(title, meta);
    choice.addEventListener("click", () => openSearchReport(report.id));
    target.append(choice);
  });
}

async function openSearchReport(id) {
  const selection = ++state.searchReportSelection;
  state.selectedSearchReportId = id;
  closeDraftPreview();
  setPaneVisibility("right", true);
  const workbench = document.querySelector(".workbench");
  workbench.classList.remove("mobile-review-open");
  workbench.classList.add("search-report-open");
  byId("inspector-title").textContent = "検索レポート";
  byId("inspector-default").hidden = true;
  byId("review-inspector").hidden = true;
  byId("search-report-inspector").hidden = false;
  byId("search-report-name").textContent = "レポートを読み込み中…";
  byId("search-report-export").hidden = true;
  byId("search-report-frame").srcdoc = reviewPreviewDocument("<p>レポートを読み込み中…</p>");
  renderSearchReports();
  try {
    const report = await api(`/api/search-reports/${encodeURIComponent(id)}`);
    if (selection !== state.searchReportSelection || state.activity !== "rag") return;
    byId("search-report-name").textContent = report.title;
    // srcdoc inherits the host page's base URL. Keep evidence links in the reader.
    byId("search-report-frame").srcdoc = reviewPreviewDocument(report.canonical_html)
      .replaceAll('href="#', 'href="about:srcdoc#');
    byId("search-report-export").href = `/api/search-reports/${encodeURIComponent(id)}/export.html`;
    byId("search-report-export").hidden = false;
    byId("search-report-close").focus({ preventScroll: true });
  } catch (error) {
    if (selection !== state.searchReportSelection) return;
    byId("search-report-name").textContent = "レポートを開けませんでした";
    byId("search-report-frame").srcdoc = reviewPreviewDocument(`<p>${escapeText(error.message)}</p>`);
  }
}

function closeSearchReport({ restoreFocus = false } = {}) {
  state.searchReportSelection += 1;
  state.selectedSearchReportId = null;
  document.querySelector(".workbench").classList.remove("search-report-open");
  byId("search-report-inspector").hidden = true;
  byId("search-report-frame").srcdoc = "";
  byId("inspector-default").hidden = false;
  byId("inspector-title").textContent = "レビュー";
  renderSearchReports();
  if (restoreFocus) byId("search-form")?.querySelector("input")?.focus();
}

function reviewWorkflowLabel(workflowName) {
  return {
    authoring: "Manuscript Generator",
    manuscript_generator: "Manuscript Generator",
    translation: "翻訳",
    manuscript_rewrite: "原稿改稿",
    ingestion: "OCR取り込み",
    corpus_sync: "Corpus同期",
  }[workflowName] || workflowName || "処理種別不明";
}

function groupOpenDefects(defects) {
  const groups = new Map();
  defects.forEach((defect) => {
    const sourceKey = defect.source_group_id
      || `${defect.object_type || "object"}:${defect.object_id || "unknown"}`;
    if (!groups.has(sourceKey)) {
      groups.set(sourceKey, {
        key: sourceKey,
        title: defect.source_title || defect.object_id || "対象不明",
        workflows: new Set(),
        runs: new Map(),
        count: 0,
      });
    }
    const group = groups.get(sourceKey);
    const runId = defect.run_id || "Run不明";
    group.count += 1;
    group.workflows.add(reviewWorkflowLabel(defect.workflow_name));
    if (!group.runs.has(runId)) group.runs.set(runId, []);
    group.runs.get(runId).push(defect);
  });
  return [...groups.values()];
}

function defectGroupMarkup(group) {
  const runs = [...group.runs.entries()].map(([runId, defects]) => `
    <details class="defect-run-group">
      <summary>
        <strong>${escapeText(reviewWorkflowLabel(defects[0]?.workflow_name))}</strong>
        <span>Run ${escapeText(runId)} · ${defects.length}件</span>
      </summary>
      <div class="defect-run-body">
        ${defects.map((defect) => `
          <button class="defect-card" type="button" data-defect="${escapeText(defect.id)}">
            <strong>${escapeText(defect.severity)} · ${escapeText(defect.defect_type)}</strong>
            <span>${escapeText(defect.rationale || defect.message)}</span>
            <small>${defect.page_start ? `p. ${escapeText(defect.page_start)}` : "ページ指定なし"}</small>
          </button>`).join("")}
      </div>
    </details>`).join("");
  return `
    <details class="defect-source-group">
      <summary>
        <span>
          <strong>${escapeText(group.title)}</strong>
          <small>${group.count}件 · ${escapeText([...group.workflows].join(" / "))} · Run ${group.runs.size}件</small>
        </span>
      </summary>
      <div class="defect-source-body">${runs}</div>
    </details>`;
}

function approvedReviewMarkup(review) {
  const title = review.source_title || review.question || "承認済み成果物";
  const revision = review.confirmed_revision_id
    ? ` · Revision ${escapeText(review.confirmed_revision_id)}`
    : "";
  const project = review.project_name
    ? ` · Project ${escapeText(review.project_name)}`
    : "";
  let destination = "";
  if (review.output_status === "ready" && review.output_folder_path) {
    destination = `
      <dl class="approval-output">
        <div><dt>出力先フォルダ</dt><dd><code>${escapeText(review.output_folder_path)}</code></dd></div>
        <div><dt>出力ファイル</dt><dd><code>${escapeText(review.output_path)}</code></dd></div>
      </dl>`;
  } else if (review.output_status === "pending") {
    destination = `<p class="muted approval-output-pending">承認済みです。最終出力を作成中です（Run ${escapeText(review.run_status)}）。</p>`;
  } else {
    destination = `
      <p class="error-text approval-output-pending">承認済み出力の最終化に失敗しました（Run ${escapeText(review.run_status || "unknown")}）。</p>
      ${review.object_type === "run_artifact"
        ? `<button class="secondary-action review-output-recover" type="button"
            data-review="${escapeText(review.id)}">承認済み出力を復旧</button>`
        : ""}`;
  }
  const preview = review.object_type === "manuscript_rewrite_proposal"
    ? `<button class="secondary-action manuscript-draft-open" type="button"
        data-proposal-id="${escapeText(review.object_id)}"
        data-draft-title="${escapeText(title)}">承認済み原稿をHTMLで確認</button>`
    : review.object_type === "run_artifact" || review.object_type === "authoring_submission"
      ? `<button class="secondary-action draft-open" type="button"
          data-artifact-id="${escapeText(review.object_id)}"
          data-draft-title="${escapeText(title)}">承認済みドラフトをHTMLで確認</button>`
      : "";
  const checkpointWarning = review.proposal_status === "checkpoint_failed"
    ? `<p class="error-text">Revisionとファイルは作成済みですが、Git checkpointに失敗しました。${escapeText(review.output_error || "")}</p>`
    : "";
  const conditionalApproval = review.bulk_approval_status === "approved_with_unresolved"
    ? `<p class="status-warning">文章は一括承認済みです。未解決事項 ${Number(review.quality_blocking_count || 0)}件は検証済みではありません。</p>`
    : "";
  const approvalRecord = review.object_type === "manuscript_rewrite_proposal"
    ? `<button class="secondary-action manuscript-proposal-open" type="button"
        data-proposal-id="${escapeText(review.object_id)}"
        data-run-id="${escapeText(review.run_id)}">承認記録と未解決事項を確認</button>`
    : "";
  return `
    <section class="review-card approved-review-entry">
      <h3>${escapeText(title)}</h3>
      <p class="muted">Run ${escapeText(review.run_id)}${project}${revision}</p>
      ${destination}
      ${checkpointWarning}
      ${conditionalApproval}
      ${preview || approvalRecord ? `<div class="review-actions">${preview}${approvalRecord}</div>` : ""}
    </section>`;
}

function openHumanReviewScreen(review, report) {
  const items = Array.isArray(report.review_items) ? report.review_items : [];
  const title = review.source_title || review.question || "原稿";
  const indexedItems = items.map((item, index) => ({ ...item, reviewIndex: index }));
  byId("tab-title").textContent = "人間確認リスト";
  byId("editor-content").innerHTML = `
    <article class="document-view human-review-screen">
      <header class="human-review-screen-header">
        <div>
          <p class="viewer-kicker">MANUSCRIPT REVIEW · ${escapeText(review.run_id)}</p>
          <h1>人間確認リスト</h1>
          <p class="muted">${escapeText(title)} · ${items.length}件</p>
        </div>
        <div class="review-actions">
          <button id="human-review-back" class="secondary-action" type="button">レビュー待ち一覧へ戻る</button>
          <button id="human-review-download" class="primary-action" type="button">Markdownをダウンロード</button>
        </div>
      </header>
      <p class="muted">項目を選ぶと確認理由と推奨確認を表示します。</p>
      ${groupHumanReviewItems(indexedItems).map((group) => `
        <section class="human-review-group" aria-label="${escapeText(group.sectionId)}の確認項目">
          <h2>${escapeText(group.sectionId)} <span>${group.items.length}件</span></h2>
          <div class="human-review-rows">
            ${group.items.map((item) => `
              <button class="human-review-row" type="button" data-human-review-index="${item.reviewIndex}">
                <span class="human-review-row-top">
                  <strong>${escapeText(humanReviewItemLabel(item))}</strong>
                  <span>${escapeText(humanReviewCategoryLabel(item.category))} · ${escapeText(item.status || "open")}</span>
                </span>
                <span class="human-review-row-preview">${escapeText(item.reason || "確認理由未記録")}</span>
                <span class="human-review-row-open">詳細を見る ↗</span>
              </button>`).join("")}
          </div>
        </section>`).join("") || '<p class="empty-state">確認項目はありません。</p>'}
    </article>`;
  byId("human-review-back").addEventListener("click", renderReviewEditor);
  byId("human-review-download").addEventListener("click", () => {
    const content = humanReviewMarkdown({ title, runId: review.run_id, items });
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `human-review-${String(review.run_id || "run").replace(/[^a-zA-Z0-9_-]/g, "-")}.md`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  byId("editor-content").querySelectorAll("[data-human-review-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const item = items[Number(button.dataset.humanReviewIndex)];
      if (!item) return;
      const dialog = byId("human-review-modal");
      byId("human-review-modal-name").textContent = humanReviewItemLabel(item);
      byId("human-review-modal-meta").textContent = `${item.section_id || "原稿全体"} · ${humanReviewCategoryLabel(item.category)} · ${item.status || "open"}`;
      byId("human-review-modal-id").textContent = item.review_item_id || item.id || "未記録";
      byId("human-review-modal-reason").textContent = item.reason || "確認理由未記録";
      byId("human-review-modal-sources").textContent = (item.source_ids || []).join("、") || "記録なし";
      byId("human-review-modal-check").textContent = item.recommended_check || "原典と本文の対応を確認";
      byId("human-review-modal-close").onclick = () => dialog.close();
      dialog.onclose = () => { if (button.isConnected) button.focus(); };
      dialog.showModal();
    });
  });
}

const reviewDrafts = new ReviewDrafts();
let selectedReviewId = null;
let reviewSelectionVersion = 0;
const reviewSaveTimers = new Map();
const reviewDraftKey = (id) => `fukamiru:review-draft:${id}`;

function persistReviewDraft(id) {
  const entry = reviewDrafts.entries.get(id);
  try {
    if (!entry.dirty) sessionStorage.removeItem(reviewDraftKey(id));
    else sessionStorage.setItem(reviewDraftKey(id), JSON.stringify({
      revision: entry.packet.revision, answers: entry.packet.answers,
      overall_comment: entry.packet.overall_comment,
    }));
  } catch { /* Server autosave remains available if browser storage is full. */ }
}

function reviewSaveStatus(message, error = false) {
  const status = byId("review-save-status");
  if (status) { status.textContent = message; status.classList.toggle("error-text", error); }
}

async function saveReviewDraft(id, submit = false) {
  clearTimeout(reviewSaveTimers.get(id));
  if (id === selectedReviewId) reviewSaveStatus(submit ? "回答を送信中…" : "下書きを保存中…");
  try {
    const packet = await reviewDrafts.save(id, (body) => api(`/api/reviews/${encodeURIComponent(id)}/feedback`, {
      method: "PUT", body: JSON.stringify(body),
    }), submit);
    persistReviewDraft(id);
    if (id === selectedReviewId) reviewSaveStatus(submit
      ? "回答を送信しました。次の生成・修正・再レビューに反映されます。"
      : packet.submitted_revision === packet.revision ? "送信済み" : "下書き保存済み");
    return packet;
  } catch (error) {
    if (id === selectedReviewId) reviewSaveStatus(`保存できませんでした: ${error.message}`, true);
    throw error;
  }
}

function editReviewDraft(id, update) {
  reviewDrafts.edit(id, update);
  persistReviewDraft(id);
  reviewSaveStatus("未送信の変更があります · 自動保存中…");
  clearTimeout(reviewSaveTimers.get(id));
  reviewSaveTimers.set(id, setTimeout(() => saveReviewDraft(id).catch(() => {}), 600));
}

async function selectReview(review) {
  closeSearchReport();
  const selection = ++reviewSelectionVersion;
  if (selectedReviewId && selectedReviewId !== review.id
      && reviewDrafts.entries.get(selectedReviewId)?.dirty) {
    try { await saveReviewDraft(selectedReviewId); } catch { /* Retain the local recovery copy. */ }
    if (selection !== reviewSelectionVersion) return;
  }
  selectedReviewId = review.id;
  document.querySelector(".workbench").classList.add("review-active", "mobile-review-open");
  closeDraftPreview();
  setPaneVisibility("right", true);
  byId("review-inspector").hidden = false;
  byId("inspector-default").hidden = true;
  byId("inspector-title").textContent = "レビュー";
  document.querySelectorAll("[data-review-select]").forEach((button) => {
    const selected = button.dataset.reviewSelect === review.id;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  byId("review-inspector").innerHTML = '<p class="empty-state">レビューを読み込み中…</p>';
  byId("review-selection").innerHTML = '<p class="empty-state">選択したレビューを読み込み中…</p>';
  try {
    const cached = reviewDrafts.entries.get(review.id);
    const entry = cached || reviewDrafts.load(await api(`/api/reviews/${encodeURIComponent(review.id)}/feedback`));
    if (selection !== reviewSelectionVersion) return;
    const packet = entry.packet;
    if (!cached && packet.status === "open") {
      try {
        const saved = JSON.parse(sessionStorage.getItem(reviewDraftKey(review.id)) || "null");
        if (saved && saved.revision === packet.revision) {
          reviewDrafts.edit(review.id, (draft) => {
            draft.answers = saved.answers; draft.overall_comment = saved.overall_comment;
          });
          reviewSaveTimers.set(review.id, setTimeout(() => saveReviewDraft(review.id).catch(() => {}), 600));
        } else if (saved) entry.recovery = saved;
      } catch { /* An unreadable recovery copy must not replace server data. */ }
    }
    const editable = packet.status === "open";
    const answered = new Map(packet.answers.map((answer) => [answer.question_id, answer]));
    const title = review.source_title || review.question || "原稿レビュー";
    byId("review-selection").innerHTML = `
      <h2>${escapeText(title)}</h2>
      <p class="muted">Run ${escapeText(review.run_id)} · 質問 ${packet.questions.length}件</p>
      <label class="review-field">全体へのコメント
        <textarea id="review-overall-comment" placeholder="原稿全体の方針や伝えておきたいこと" ${editable ? "" : "readonly"}>${escapeText(packet.overall_comment)}</textarea>
      </label>
      <p id="review-save-status" role="status">${entry.dirty || (packet.submitted_revision != null && packet.submitted_revision !== packet.revision) ? "未送信の下書きを表示しています。送信すると次の処理に反映されます。" : packet.submitted_at ? "送信済みの回答を表示しています" : "回答は自動保存されます。送信すると次の処理に反映されます。"}</p>
      <div class="review-actions">
        ${editable ? '<button class="secondary-action" id="review-submit" type="button">回答を送信</button>' : ""}
        ${editable && review.object_type !== "manuscript_rewrite_proposal" ? `
          ${!["authoring_graph_invalid_reference", "authoring_draft_source_unresolved"].includes(review.object_type) ? `<button class="primary-action review-decision" data-review="${escapeText(review.id)}" data-decision="approve">承認</button>` : ""}
          ${["authoring_submission", "run_artifact", "authoring_graph_invalid_reference"].includes(review.object_type) ? `<button class="secondary-action review-decision" data-review="${escapeText(review.id)}" data-decision="revise">回答を反映して${review.object_type === "authoring_submission" ? "再生成" : "修正"}</button>` : ""}
          ${review.object_type === "authoring_draft_source_unresolved" ? `<button class="secondary-action review-decision" data-review="${escapeText(review.id)}" data-decision="defer">出典未確認として保留し、生成を続ける</button>` : ""}
          <button class="danger-action review-decision" data-review="${escapeText(review.id)}" data-decision="reject">却下</button>` : ""}
        ${review.object_type === "manuscript_rewrite_proposal" ? `<button class="primary-action manuscript-proposal-open" data-proposal-id="${escapeText(review.object_id)}" data-run-id="${escapeText(review.run_id)}">改稿案の承認操作へ</button>${editable ? '<button class="secondary-action" id="review-proposal-revise" type="button">回答を反映して再改稿</button>' : ""}` : ""}
      </div>`;
    byId("review-inspector").innerHTML = `
      <button id="review-pane-back" class="secondary-action review-pane-back" type="button">一覧と全体コメントへ</button>
      <header class="review-pane-heading"><strong>${escapeText(title)}</strong><span class="muted">${packet.questions.length}問 · ${editable ? "回答待ち" : "回答記録"}</span></header>
      <div class="review-pane-tabs" role="tablist" aria-label="レビュー表示">
        <button id="review-questions-tab" role="tab" aria-controls="review-questions" aria-selected="true" tabindex="0">質問と回答</button>
        <button id="review-html-tab" role="tab" aria-controls="review-html" aria-selected="false" tabindex="-1">対象のHTML</button>
      </div>
      <section id="review-questions" role="tabpanel" aria-labelledby="review-questions-tab">
      ${entry.recovery ? `<details class="review-recovery"><summary>このタブに未保存の回答があります</summary><p>別の画面で更新されているため、自動では上書きしません。</p>${entry.recovery.answers.map((answer) => `<p><strong>${escapeText(packet.questions.find((question) => question.question_id === answer.question_id)?.question || "以前の質問")}</strong><br>${escapeText(answer.answer)}${answer.llm_overreach ? " · LLMの過剰要求である" : ""}</p>`).join("")}<p>${escapeText(entry.recovery.overall_comment)}</p><button id="review-recover-local" class="secondary-action" type="button">未保存の回答を復元して編集</button></details>` : ""}
        <p class="muted">質問ごとに回答を入力して送信します。過剰要求と判断した質問は、回答の代わりにチェックを付けます。</p>
        ${packet.questions.map((question, index) => {
          const answer = answered.get(question.question_id) || {};
          const previous = question.previous_answer;
          return `<section class="review-question" data-question-id="${reviewAttribute(question.question_id)}">
            <span class="review-question-number">質問 ${index + 1} <span>${escapeText(question.target)}</span></span>
            <h3 id="review-question-${index}">${escapeText(question.question)}</h3>
            ${question.context_note ? `<p class="review-question-context">${escapeText(question.context_note)}</p>` : ""}
            ${question.excerpt_scope === "paragraph" ? '<p class="review-excerpt-label">対象の段落</p>' : question.excerpt_scope === "section" ? '<p class="review-excerpt-label">対象の節</p>' : question.excerpt_scope === "planning" ? '<p class="review-excerpt-label">検討対象の内容</p>' : ""}
            ${question.excerpt ? question.excerpt.length > 360
              ? `<details class="review-excerpt-detail"><summary>本文の抜粋を読む</summary><blockquote class="review-question-excerpt">${escapeText(question.excerpt)}</blockquote></details>`
              : `<blockquote class="review-question-excerpt">${escapeText(question.excerpt)}</blockquote>` : ""}
            ${previous ? `<p class="review-previous-answer">前回の判断: ${previous.llm_overreach ? "LLMの過剰要求である" : escapeText(previous.answer)}</p>` : ""}
            <label class="review-field">回答<textarea data-question-answer="${reviewAttribute(question.question_id)}" aria-labelledby="review-question-${index}" placeholder="考えや補足情報を記入" ${editable ? "" : "readonly"}>${escapeText(answer.answer || "")}</textarea></label>
            <label class="review-overreach"><input type="checkbox" data-question-overreach="${reviewAttribute(question.question_id)}" ${answer.llm_overreach ? "checked" : ""} ${editable ? "" : "disabled"}><span>LLMの過剰要求である</span></label>
            <details class="review-question-details"><summary>質問の記録</summary><p>質問ID: ${escapeText(question.question_id)}</p>${question.recorded_question && question.recorded_question !== question.question ? `<p>保存された質問: ${escapeText(question.recorded_question)}</p>` : ""}<p>${escapeText(question.reason)}</p><p>関連する資料: ${escapeText(question.source_ids.join("、") || "なし")}</p></details>
          </section>`;
        }).join("") || '<p class="empty-state">新しく回答が必要な質問はありません。本文を確認し、中央で承認や全体コメントを行えます。</p>'}
      </section>
      <section id="review-html" role="tabpanel" aria-labelledby="review-html-tab" hidden>
        ${packet.preview_html ? '<button id="review-html-expand" class="secondary-action" type="button">本文を大きく表示</button><iframe id="review-html-frame" sandbox title="レビュー対象のHTML原稿"></iframe>' : '<p class="empty-state">このレビューにはHTML原稿がありません。質問の対象と理由を確認してください。</p>'}
      </section>`;
    const frame = byId("review-html-frame");
    byId("review-recover-local")?.addEventListener("click", () => {
      editReviewDraft(review.id, (draft) => {
        draft.answers = entry.recovery.answers; draft.overall_comment = entry.recovery.overall_comment;
      });
      delete entry.recovery;
      selectReview(review);
    });
    byId("review-pane-back").addEventListener("click", () => document.querySelector(".workbench").classList.remove("mobile-review-open"));
    if (frame) frame.srcdoc = reviewPreviewDocument(packet.preview_html);
    for (const name of ["questions", "html"]) {
      byId(`review-${name}-tab`).addEventListener("click", () => {
        for (const pane of ["questions", "html"]) {
          byId(`review-${pane}`).hidden = pane !== name;
          byId(`review-${pane}-tab`).setAttribute("aria-selected", String(pane === name));
          byId(`review-${pane}-tab`).tabIndex = pane === name ? 0 : -1;
        }
      });
      byId(`review-${name}-tab`).addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? "questions" : event.key === "End" ? "html" : name === "html" ? "questions" : "html";
        byId(`review-${next}-tab`).click();
        byId(`review-${next}-tab`).focus();
      });
    }
    byId("review-html-expand")?.addEventListener("click", () => {
      byId("review-html-modal-frame").srcdoc = reviewPreviewDocument(packet.preview_html);
      byId("review-html-modal").showModal();
    });
    byId("review-overall-comment").addEventListener("input", (event) => {
      editReviewDraft(review.id, (draft) => { draft.overall_comment = event.target.value; });
    });
    byId("review-inspector").querySelectorAll("[data-question-answer], [data-question-overreach]").forEach((input) => {
      input.addEventListener(input.type === "checkbox" ? "change" : "input", () => {
        const id = input.dataset.questionAnswer || input.dataset.questionOverreach;
        editReviewDraft(review.id, (draft) => {
          let answer = draft.answers.find((item) => item.question_id === id);
          if (!answer) { answer = { question_id: id, answer: "", llm_overreach: false }; draft.answers.push(answer); }
          if (input.type === "checkbox") answer.llm_overreach = input.checked;
          else answer.answer = input.value;
        });
      });
    });
    byId("review-submit")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await saveReviewDraft(review.id, true); } catch { /* Status retains the error. */ }
      finally { button.disabled = false; }
    });
    byId("review-selection").querySelectorAll(".review-decision").forEach((button) => button.addEventListener("click", resolveReview));
    byId("review-selection").querySelector(".manuscript-proposal-open")?.addEventListener("click", openManuscriptRewriteReview);
    byId("review-proposal-revise")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await saveReviewDraft(review.id, true);
        const result = await api(`/api/manuscript-rewrites/${encodeURIComponent(review.object_id)}/revise`, { method: "POST", body: JSON.stringify({}) });
        connectToRun(result.run_id);
        reviewSaveStatus("回答を反映する再改稿を開始しました。");
      } catch (error) { reviewSaveStatus(error.message, true); button.disabled = false; }
    });
  } catch (error) {
    if (selection !== reviewSelectionVersion) return;
    byId("review-inspector").innerHTML = `<p class="error-text">${escapeText(error.message)}</p>`;
    byId("review-selection").innerHTML = '<p class="error-text">レビューを読み込めませんでした。一覧から選び直してください。</p>';
  }
}

async function renderReviewEditor() {
  byId("tab-title").textContent = "レビュー待ち一覧";
  byId("editor-content").innerHTML = '<article class="document-view"><h1>レビュー待ち一覧</h1><p class="empty-state">読み込み中…</p></article>';
  try {
    const reviews = await api("/api/reviews?status=open");
    state.reviews = reviews;
    if (state.activity === "review") renderSidebar();
    byId("editor-content").innerHTML = `
      <article class="document-view review-workspace"><h1>レビュー待ち一覧 <span class="muted">${reviews.length}件</span></h1>
        <p class="muted">対象を選び、右ペインの本文と質問を確認してください。</p>
        <div class="review-queue-list" aria-label="承認待ちのレビュー">
          ${reviews.map((review) => `<button type="button" class="review-queue-choice" data-review-select="${escapeText(review.id)}" aria-pressed="false"><strong>${escapeText(review.source_title || review.question)}</strong><span>承認待ち · Run ${escapeText(review.run_id)}</span></button>`).join("") || '<p class="empty-state">承認待ちのレビューはありません。</p>'}
        </div>
        <section id="review-selection" aria-label="選択したレビュー"><p class="empty-state">レビューを選ぶと全体コメントと承認操作を表示します。</p></section>
        ${["approved", "rejected", "feedback"].map((status) => `<details class="review-archive" data-review-history="${status}"><summary>${status === "approved" ? "承認" : status === "rejected" ? "却下" : "送信した回答の"}履歴</summary><div class="review-archive-body"></div></details>`).join("")}
      </article>`;
    byId("editor-content").querySelectorAll("[data-review-select]").forEach((button) => {
      button.addEventListener("click", () => selectReview(reviews.find((item) => item.id === button.dataset.reviewSelect)));
    });
    byId("editor-content").querySelectorAll("[data-review-history]").forEach((details) => {
      details.addEventListener("toggle", async () => {
        if (!details.open || details.dataset.loaded) return;
        details.dataset.loaded = "true";
        const body = details.querySelector(".review-archive-body");
        try {
          const history = await api(details.dataset.reviewHistory === "feedback" ? "/api/review-feedback-history" : `/api/reviews?status=${details.dataset.reviewHistory}`);
          body.innerHTML = history.map((review) => `<div class="review-history-item"><button type="button" class="review-queue-choice" data-history-select="${escapeText(review.id)}"><strong>${escapeText(review.source_title || review.question)}</strong><span>回答記録を確認 · Run ${escapeText(review.run_id)}</span></button>${review.status === "approved" && approvedOutputReviews([review]).length ? approvedReviewMarkup(review) : ""}</div>`).join("") || '<p class="empty-state">履歴はありません。</p>';
          body.querySelectorAll("[data-history-select]").forEach((button) => button.addEventListener("click", () => selectReview(history.find((item) => item.id === button.dataset.historySelect))));
          body.querySelectorAll(".draft-open").forEach((button) => button.addEventListener("click", openDraftPreview));
          body.querySelectorAll(".manuscript-draft-open").forEach((button) => button.addEventListener("click", openManuscriptDraftPreview));
          body.querySelectorAll(".review-output-recover").forEach((button) => button.addEventListener("click", recoverApprovedReviewOutput));
        } catch (error) { delete details.dataset.loaded; body.textContent = error.message; }
      });
    });
    const selected = reviews.find((review) => review.id === selectedReviewId) || reviews[0];
    if (selected) await selectReview(selected);
    else { selectedReviewId = null; byId("review-inspector").hidden = true; byId("inspector-default").hidden = false; }
  } catch (error) { addProblem(error.message); }
}

async function discardManuscriptRewrite(event) {
  const button = event.currentTarget;
  const proposalId = button.dataset.proposalId;
  button.disabled = true;
  try {
    await api(`/api/manuscript-rewrites/${encodeURIComponent(proposalId)}/discard`, {
      method: "POST",
    });
    if (state.draftPreview?.artifactId === `manuscript-rewrite:${proposalId}`) {
      closeDraftPreview();
    }
    await renderReviewEditor();
  } catch (error) {
    addProblem(error.message);
    button.disabled = false;
  }
}

async function openManuscriptRewriteReview(event) {
  const proposalId = event.currentTarget.dataset.proposalId;
  const runId = event.currentTarget.dataset.runId;
  byId("tab-title").textContent = "Manuscript Review";
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <button class="secondary-action" type="button" id="manuscript-review-back">レビュー待ち一覧へ戻る</button>
      <p class="muted">Run ${escapeText(runId)} · Proposal ${escapeText(proposalId)}</p>
      <div id="manuscript-rewrite-result"><p class="empty-state">Loading…</p></div>
    </article>`;
  byId("manuscript-review-back").addEventListener("click", renderReviewEditor);
  try {
    const proposal = await api(`/api/manuscript-rewrites/${encodeURIComponent(proposalId)}`);
    renderManuscriptRewriteProposal(proposal);
  } catch (error) {
    addProblem(error.message);
    byId("manuscript-rewrite-result").innerHTML = '<p class="error-text">改稿案を取得できませんでした。</p>';
  }
}

async function openDefect(event) {
  try {
    const defect = await api(`/api/defects/${encodeURIComponent(event.currentTarget.dataset.defect)}`);
    byId("tab-title").textContent = "Defect Inspector";
    byId("editor-content").innerHTML = `
      <article class="document-view defect-detail">
        <h1>${escapeText(defect.defect_type)}</h1>
        <p><strong>${escapeText(defect.severity)}</strong> · ${escapeText(defect.status)}</p>
        <h2>判定根拠</h2><p>${escapeText(defect.rationale)}</p>
        <h2>問題箇所</h2><pre>${escapeText(defect.excerpt)}</pre>
        <h2>前後context</h2><pre>${escapeText(defect.context_text)}</pre>
        ${defect.repair_candidates?.length ? `<h2>修正候補</h2><ul>${defect.repair_candidates.map((item) => `<li>${escapeText(item)}</li>`).join("")}</ul>` : ""}
        ${defect.automatic_repair_allowed && defect.details?.evaluation_run_id
          ? `<button class="primary-action defect-auto-repair"
              data-evaluation="${escapeText(defect.details.evaluation_run_id)}"
              data-defect="${escapeText(defect.id)}">決定論的修復を実行</button>`
          : ""}
        ${defect.source?.page_url ? `<a class="artifact-link" href="${escapeText(defect.source.page_url)}" target="_blank" rel="noopener">原PDF p. ${escapeText(defect.page_start)}を開く</a>` : ""}
      </article>`;
    document.querySelector(".defect-auto-repair")?.addEventListener("click", repairDefect);
  } catch (error) {
    addProblem(error.message);
  }
}

async function repairDefect(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const attempt = await api(
      `/api/evaluations/${encodeURIComponent(button.dataset.evaluation)}/defects/${encodeURIComponent(button.dataset.defect)}/repair`,
      { method: "POST", body: JSON.stringify({ strategy: "restore_source_quote" }) },
    );
    button.insertAdjacentHTML(
      "afterend",
      `<p class="muted">Repair ${escapeText(attempt.status)} · Revision ${escapeText(attempt.output_revision_id || "none")}</p>`,
    );
  } catch (error) {
    addProblem(error.message);
    button.disabled = false;
  }
}

async function openOcrRevision(event) {
  const revisionId = event.currentTarget.dataset.ocrRevision;
  try {
    const [response, revision] = await Promise.all([
      fetch(`/api/ocr/revisions/${encodeURIComponent(revisionId)}/html`),
      api(`/api/ocr/revisions/${encodeURIComponent(revisionId)}`),
    ]);
    if (!response.ok) throw new Error(`OCR revision could not be opened (${response.status})`);
    byId("tab-title").textContent = "OCR Source";
    byId("editor-content").innerHTML = `
      <div class="canonical-ocr-view">${await response.text()}</div>
      <details class="ocr-correction-panel">
        <summary>OCRブロックを人間が修正</summary>
        <form id="ocr-correction-form" class="form-grid compact-form">
          <label class="field full">Block
            <select name="block_id">${revision.blocks.map((block) => `<option value="${escapeText(block.stable_block_id)}">${escapeText(block.stable_block_id)} · ${escapeText(block.block_type)}</option>`).join("")}</select>
          </label>
          <label class="field full">Corrected text
            <textarea name="corrected_text" required></textarea>
          </label>
          <label class="field full">Correction note
            <input name="message" placeholder="修正根拠">
          </label>
          <button class="primary-action" type="submit">Human OCR Revisionとして保存</button>
        </form>
      </details>`;
    const form = byId("ocr-correction-form");
    const select = form.elements.block_id;
    const textarea = form.elements.corrected_text;
    const updateText = () => {
      textarea.value = revision.blocks.find((block) => block.stable_block_id === select.value)?.text || "";
    };
    select.addEventListener("change", updateText);
    updateText();
    form.addEventListener("submit", async (submitEvent) => {
      submitEvent.preventDefault();
      try {
        const result = await api(`/api/ocr/revisions/${encodeURIComponent(revisionId)}/corrections`, {
          method: "POST",
          body: JSON.stringify({
            corrections: { [select.value]: textarea.value },
            message: form.elements.message.value,
          }),
        });
        await openOcrRevision({ currentTarget: { dataset: { ocrRevision: result.revision_id } } });
      } catch (error) {
        addProblem(error.message);
      }
    });
  } catch (error) {
    addProblem(error.message);
  }
}

async function resolveReview(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const reviewId = button.dataset.review;
    const entry = reviewDrafts.entries.get(reviewId);
    if (entry && (entry.dirty || entry.packet.answers.length || entry.packet.overall_comment.trim())) {
      await saveReviewDraft(reviewId, true);
    }
    let feedback = document.querySelector(`[data-review-feedback="${CSS.escape(reviewId)}"]`)?.value || "";
    const sourceInputs = [...document.querySelectorAll(`[data-review-source="${CSS.escape(reviewId)}"]`)];
    if (sourceInputs.length && button.dataset.decision === "revise") {
      feedback = JSON.stringify(sourceInputs.map((input) => {
        const evidenceId = input.dataset.evidenceId;
        const corrected = [...document.querySelectorAll(`[data-review-evidence-text="${CSS.escape(reviewId)}"]`)]
          .find((item) => item.dataset.evidenceId === evidenceId)?.value.trim();
        return {
          id: evidenceId,
          source_ids: input.value.split(",").map((value) => value.trim()).filter(Boolean),
          ...(corrected ? { text: corrected } : {}),
        };
      }));
    }
    const correctedText = document.querySelector(`[data-review-corrected="${CSS.escape(reviewId)}"]`)?.value || null;
    const claimId = document.querySelector(`[data-review-claim="${CSS.escape(reviewId)}"]`)?.value || null;
    const result = await api(`/api/reviews/${button.dataset.review}/resolve`, {
      method: "POST",
      body: JSON.stringify({
        decision: button.dataset.decision,
        feedback,
        corrected_text: correctedText,
        claim_id: claimId,
      }),
    });
    reviewDrafts.entries.delete(reviewId);
    connectToRun(result.run_id);
    await renderReviewEditor();
  } catch (error) {
    addProblem(error.message);
    button.disabled = false;
  }
}

async function recoverApprovedReviewOutput(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await api(`/api/reviews/${encodeURIComponent(button.dataset.review)}/recover-output`, {
      method: "POST",
    });
    connectToRun(result.run_id);
    await renderReviewEditor();
  } catch (error) {
    addProblem(error.message);
    button.disabled = false;
  }
}

function projectLifecycleAvailable() {
  return Boolean(state.health?.features?.project_lifecycle);
}

function manuscriptImportAvailable() {
  return Boolean(state.health?.features?.manuscript_import);
}

function manuscriptRewriteAvailable() {
  return Boolean(state.health?.features?.manuscript_rewrite);
}

function renderProjectEditor(message = "") {
  byId("tab-title").textContent = "Register Project";
  const lifecycleAvailable = projectLifecycleAvailable();
  const manuscriptImport = manuscriptImportAvailable()
    ? `<h2>Word / PDF / TXT / Markdown Import Preview</h2>
       <p class="muted">Project内の途中原稿を選択するとContent Document IDを自動発行します。元ファイルは変更せず、canonical HTMLを確認してから最初のRevisionとGit checkpointを作成します。</p>
       <form id="manuscript-import-form" class="form-grid">
         <div class="field full">
           <label for="manuscript-import-project">Project</label>
           <select id="manuscript-import-project" name="project_id" required>
             <option value="">Projectを選択</option>
             ${state.projects.map((project) => `<option value="${escapeText(project.id)}">${escapeText(project.name)} · ${escapeText(project.root_path)}</option>`).join("")}
           </select>
         </div>
         <div class="field full">
           <label for="manuscript-import-path">Project内の原稿</label>
           <select id="manuscript-import-path" name="source_path" required disabled>
             <option value="">先にProjectを選択してください</option>
           </select>
           <small id="manuscript-import-source-status">.docx / .pdf / .txt / .md / .markdownをProject配下から検索します。</small>
         </div>
         <div class="field full">
           <label for="manuscript-import-title">原稿タイトル</label>
           <input id="manuscript-import-title" name="title" required maxlength="500">
         </div>
         <div class="field">
           <label for="manuscript-import-kind">文書種別</label>
           <select id="manuscript-import-kind" name="document_kind" required>
             <option value="paper">論文・章（paper）</option>
             <option value="dissertation">博士論文全体（dissertation）</option>
             <option value="translation">翻訳原稿（translation）</option>
             <option value="note">研究ノート（note）</option>
             <option value="appendix">付録（appendix）</option>
             <option value="supplement">補足資料（supplement）</option>
           </select>
         </div>
         <button class="secondary-action" id="manuscript-import-refresh" type="button">原稿一覧を更新</button>
         <button class="primary-action" type="submit">Documentを登録してPreviewを作成</button>
       </form>
       <div id="manuscript-import-result"></div>`
    : "";
  const manuscriptRewrite = manuscriptRewriteAvailable()
    ? `<h2>途中原稿を内容に沿って改稿</h2>
       <p class="muted">現在のRevision、Dissertation Outlineの章概要・執筆指示、Corpusの研究資料を踏まえて改稿案を生成します。AI案は元原稿を上書きせず、diffを確認して承認したときだけ新Revisionになります。</p>
       <form id="manuscript-rewrite-form" class="form-grid">
         <div class="field full">
           <label for="manuscript-rewrite-document">Content Document ID</label>
           <input id="manuscript-rewrite-document" name="document_id" required>
         </div>
         <div class="field">
           <label for="manuscript-rewrite-base">Base Revision ID（省略時は現在版）</label>
           <input id="manuscript-rewrite-base" name="base_revision_id">
         </div>
         <div class="field">
           <label for="manuscript-rewrite-outline">Outline Node ID（任意）</label>
           <input id="manuscript-rewrite-outline" name="outline_node_id">
         </div>
         <div class="field full">
           <label for="manuscript-rewrite-instructions">どのように書き換えるか</label>
           <textarea id="manuscript-rewrite-instructions" name="instructions" rows="5" required placeholder="章の概要に合わせて論点を再構成し、先行研究との関係を明確にする。断定できない箇所は未解決として残す。"></textarea>
         </div>
         <label class="field full"><input name="use_rag" type="checkbox" checked> Corpusの研究資料を文脈として使う</label>
         <button class="primary-action" type="submit">内容を踏まえた改稿案を作成</button>
       </form>
       <div id="manuscript-rewrite-result"></div>`
    : "";
  const archivedProjects = lifecycleAvailable && state.archivedProjects.length
    ? `<section class="project-lifecycle">
        <h2>登録解除済みProject</h2>
        ${state.archivedProjects.map((project) => `
          <article class="review-card project-card">
            <div>
              <p><strong>${escapeText(project.name)}</strong></p>
              <p class="muted">${escapeText(project.root_path)}</p>
            </div>
            <button class="secondary-action restore-project" data-project-id="${escapeText(project.id)}">登録を復元</button>
          </article>
        `).join("")}
      </section>`
    : "";
  const preflightControl = lifecycleAvailable
    ? `<button class="secondary-action" id="project-preflight" type="button">保存先を確認</button>
       <div id="project-preflight-result" class="field full"></div>`
    : "";
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <h1>Projectを登録</h1>
      <p class="lede">保存先のフォルダをGitリポジトリとして初期化します。保存先は「ドキュメント」フォルダ内など、書き込み権限のある場所を絶対パスで指定してください。</p>
      ${message ? `<p class="notice">${escapeText(message)}</p>` : ""}
      <form id="project-form" class="form-grid">
        <div class="field full">
          <label for="project-name">Project name</label>
          <input id="project-name" name="name" required placeholder="Pawlowski dissertation">
        </div>
        <div class="field full">
          <label for="project-root">Project root</label>
          <input id="project-root" name="root_path" required placeholder="C:\\Research\\my-project">
          <small>NTFS上の既存フォルダまたは新しく作るフォルダを指定してください。既存ファイルは削除しません。</small>
        </div>
        ${preflightControl}
        <button class="primary-action" type="submit">Projectを作成・登録</button>
      </form>
      ${archivedProjects}
      <section class="dissertation-launcher">
        <h2>Dissertation Outline</h2>
        <form id="dissertation-form" class="form-grid">
          <div class="field full">
            <label for="dissertation-project">Project</label>
            <select id="dissertation-project" name="project_id" required>
              <option value="">Projectを選択</option>
              ${state.projects.map((project) => `<option value="${escapeText(project.id)}">${escapeText(project.name)}</option>`).join("")}
            </select>
          </div>
          <div class="field full">
            <label for="dissertation-title">Title</label>
            <input id="dissertation-title" name="title" required>
          </div>
          <button class="secondary-action" type="submit">Outlineを作成</button>
        </form>
        <div id="dissertation-result"></div>
        <form id="dissertation-csv-form" class="form-grid">
          <div class="field full">
            <label for="dissertation-csv-document">Dissertation Document ID</label>
            <input id="dissertation-csv-document" name="document_id" required>
          </div>
          <div class="field full">
            <label for="dissertation-csv-path">Outline CSV / TSV absolute path</label>
            <input id="dissertation-csv-path" name="source_path" required placeholder="C:\\path\\to\\outline.csv">
          </div>
          <div class="field">
            <label for="dissertation-csv-total">全体の目標文字数（任意）</label>
            <input id="dissertation-csv-total" name="total_target_characters" type="number" min="1" placeholder="80000">
          </div>
          <button class="secondary-action" type="submit">CSV生成計画をPreview</button>
        </form>
        <div id="dissertation-csv-result"></div>
        <form id="dissertation-evaluation-form" class="form-grid">
          <div class="field full">
            <label for="dissertation-evaluation-document">Dissertation Document ID</label>
            <input id="dissertation-evaluation-document" name="document_id" required>
          </div>
          <div class="field full">
            <label for="dissertation-evaluation-run">Run ID</label>
            <input id="dissertation-evaluation-run" name="run_id" required value="${escapeText(state.run?.run?.id || state.run?.run_id || "")}">
          </div>
          <button class="secondary-action" type="submit">全体整合性を検査</button>
        </form>
        <div id="dissertation-evaluation-result"></div>
      </section>
      <section class="evaluation-launcher">
        ${manuscriptImport}
        ${manuscriptRewrite}
        <h2>External HTML Import Preview</h2>
        <p class="muted">元HTMLを変更せず、canonical検証結果を確認してからhuman_import RevisionとGit checkpointを作成します。</p>
        <form id="html-import-form" class="form-grid">
          <div class="field full">
            <label for="html-import-document">Content Document ID</label>
            <input id="html-import-document" name="document_id" required>
          </div>
          <div class="field full">
            <label for="html-import-path">External HTML absolute path</label>
            <input id="html-import-path" name="source_path" required>
          </div>
          <button class="secondary-action" type="submit">Import Previewを作成</button>
        </form>
        <div id="html-import-result"></div>
        <form id="revision-history-form" class="form-grid">
          <div class="field full">
            <label for="revision-history-document">Revision History Document ID</label>
            <input id="revision-history-document" name="document_id" required>
          </div>
          <button class="secondary-action" type="submit">Revision履歴を表示</button>
        </form>
        <div id="revision-history-result"></div>
        <h2>Evaluation / Repair</h2>
        <p class="muted">Canonical RevisionのClaim–Evidence、direct quote、citation、cross-referenceを決定論的に検査し、Defectへ保存します。</p>
        <form id="evaluation-form" class="form-grid">
          <div class="field full">
            <label for="evaluation-run-id">Run ID</label>
            <input id="evaluation-run-id" name="run_id" required value="${escapeText(state.run?.run?.id || state.run?.run_id || "")}">
          </div>
          <div class="field full">
            <label for="evaluation-revision-id">Canonical Revision ID</label>
            <input id="evaluation-revision-id" name="revision_id" required>
          </div>
          <div class="field">
            <label for="evaluation-max-repairs">Maximum repair attempts</label>
            <input id="evaluation-max-repairs" name="max_repair_attempts" type="number" min="0" max="10" value="2">
          </div>
          <button class="secondary-action" type="submit">評価を実行</button>
        </form>
        <div id="evaluation-result"></div>
      </section>
    </article>
  `;
  byId("project-form").addEventListener("submit", registerProject);
  if (lifecycleAvailable) {
    byId("project-preflight").addEventListener("click", preflightProjectPath);
    document.querySelectorAll(".restore-project").forEach((button) => {
      button.addEventListener("click", restoreProject);
    });
  }
  byId("dissertation-form").addEventListener("submit", createDissertation);
  byId("dissertation-csv-form").addEventListener("submit", previewDissertationCSV);
  byId("dissertation-evaluation-form").addEventListener("submit", evaluateDissertation);
  if (manuscriptImportAvailable()) {
    byId("manuscript-import-form").addEventListener("submit", previewManuscriptImport);
    byId("manuscript-import-project").addEventListener("change", loadProjectManuscripts);
    byId("manuscript-import-path").addEventListener("change", suggestManuscriptTitle);
    byId("manuscript-import-refresh").addEventListener("click", () => {
      loadProjectManuscripts({ currentTarget: byId("manuscript-import-project") });
    });
  }
  if (manuscriptRewriteAvailable()) {
    byId("manuscript-rewrite-form").addEventListener("submit", startManuscriptRewrite);
  }
  byId("html-import-form").addEventListener("submit", previewHTMLImport);
  byId("revision-history-form").addEventListener("submit", loadRevisionHistory);
  byId("evaluation-form").addEventListener("submit", startEvaluation);
}

function renderProjectDetails(project) {
  byId("tab-title").textContent = project.name;
  const lifecycleAction = projectLifecycleAvailable()
    ? `<button class="danger-action" id="unregister-project">登録解除（ファイル保持）</button>`
    : "";
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <h1>${escapeText(project.name)}</h1>
      <p class="lede">${escapeText(project.root_path)}</p>
      <dl class="metadata-list">
        <div><dt>Project ID</dt><dd>${escapeText(project.id)}</dd></div>
        <div><dt>Git</dt><dd>${project.git_enabled ? "enabled" : "disabled"}</dd></div>
        <div><dt>Registered</dt><dd>${escapeText(project.created_at)}</dd></div>
      </dl>
      ${lifecycleAction}
      ${projectLifecycleAvailable() ? '<p class="muted">登録解除してもProjectフォルダ、Git履歴、ふかみる内の文書・改訂履歴・Runは削除されません。</p>' : ""}
    </article>
  `;
  if (projectLifecycleAvailable()) {
    byId("unregister-project").addEventListener("click", () => unregisterProject(project));
  }
}

async function preflightProjectPath() {
  const rootPath = String(new FormData(byId("project-form")).get("root_path") || "").trim();
  const result = byId("project-preflight-result");
  if (!rootPath) {
    result.innerHTML = '<p class="muted">Project rootを入力してください。</p>';
    return;
  }
  try {
    const assessment = await api("/api/projects/preflight", {
      method: "POST",
      body: JSON.stringify({ root_path: rootPath }),
    });
    result.innerHTML = `
      <p><strong>この保存先はProjectの保存条件を満たしています。</strong></p>
      <p class="muted">${escapeText(assessment.root_path)} · ${escapeText(assessment.filesystem || "filesystem unknown")} · ${Number(assessment.existing_entries)} existing entries</p>
    `;
  } catch (error) {
    result.innerHTML = `<p class="error-text">${escapeText(error.message)}</p>`;
  }
}

async function unregisterProject(project) {
  const confirmed = window.confirm(
    `「${project.name}」の登録を解除しますか？\nProjectフォルダとふかみる内のデータは削除されません。`,
  );
  if (!confirmed) return;
  try {
    const archived = await api(`/api/projects/${encodeURIComponent(project.id)}`, {
      method: "DELETE",
    });
    state.projects = state.projects.filter((item) => item.id !== project.id);
    state.archivedProjects = [archived, ...state.archivedProjects.filter((item) => item.id !== project.id)];
    renderSidebar();
    renderProjectEditor("Projectの登録を解除しました。フォルダとデータは保持されています。");
  } catch (error) {
    addProblem(error.message);
  }
}

async function restoreProject(event) {
  const projectId = event.currentTarget.dataset.projectId;
  event.currentTarget.disabled = true;
  try {
    const restored = await api(`/api/projects/${encodeURIComponent(projectId)}/restore`, {
      method: "POST",
    });
    state.archivedProjects = state.archivedProjects.filter((item) => item.id !== projectId);
    state.projects = [restored, ...state.projects.filter((item) => item.id !== projectId)];
    renderSidebar();
    renderProjectEditor("Projectの登録を復元しました。");
  } catch (error) {
    addProblem(error.message);
    event.currentTarget.disabled = false;
  }
}

async function loadRevisionHistory(event) {
  event.preventDefault();
  const documentId = new FormData(event.currentTarget).get("document_id");
  try {
    const revisions = await api(`/api/documents/${encodeURIComponent(documentId)}/revisions`);
    byId("revision-history-result").innerHTML = revisions.length ? revisions.map((revision) => `
      <article class="review-card">
        <p><strong>${escapeText(revision.operation)}</strong> · ${escapeText(revision.editor_type)} · ${escapeText(revision.created_at)}</p>
        <p class="muted">${escapeText(revision.id)} · ${Number(revision.block_count)} blocks · parent ${escapeText(revision.parent_revision_id || "none")}</p>
        <button class="artifact-link revision-diff" data-revision="${escapeText(revision.id)}">編集diffを表示</button>
      </article>`).join("") : '<p class="muted">Revisionはありません。</p>';
    document.querySelectorAll(".revision-diff").forEach((button) => {
      button.addEventListener("click", loadRevisionDiff);
    });
  } catch (error) {
    addProblem(error.message);
  }
}

async function loadRevisionDiff(event) {
  try {
    const diff = await api(`/api/revisions/${encodeURIComponent(event.currentTarget.dataset.revision)}/diff`);
    event.currentTarget.closest("article").insertAdjacentHTML(
      "beforeend",
      `<pre class="revision-diff-view">${escapeText(diff.unified_diff || "No textual changes")}</pre>`,
    );
    event.currentTarget.disabled = true;
  } catch (error) {
    addProblem(error.message);
  }
}

async function previewHTMLImport(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const preview = await api("/api/html-import-previews", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget).entries())),
    });
    renderHTMLImportPreview(preview);
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function previewManuscriptImport(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const preview = await api("/api/manuscript-import-previews", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget).entries())),
    });
    renderManuscriptImportPreview(preview);
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

function suggestManuscriptTitle(event) {
  const title = byId("manuscript-import-title");
  if (!title || title.value.trim()) return;
  const selected = event.currentTarget.selectedOptions?.[0];
  const filename = selected?.dataset?.filename || "";
  title.value = filename.replace(/\.(?:docx|pdf|txt|md|markdown)$/i, "");
}

async function loadProjectManuscripts(event) {
  const projectId = event.currentTarget.value;
  const source = byId("manuscript-import-path");
  const status = byId("manuscript-import-source-status");
  if (!source || !status) return;
  source.disabled = true;
  source.innerHTML = `<option value="">${projectId ? "原稿を検索中…" : "先にProjectを選択してください"}</option>`;
  if (!projectId) {
    status.textContent = ".docx / .pdf / .txt / .md / .markdownをProject配下から検索します。";
    return;
  }
  try {
    const result = await api(`/api/projects/${encodeURIComponent(projectId)}/manuscripts`);
    if (!result.files.length) {
      source.innerHTML = '<option value="">対応する原稿がありません</option>';
      status.textContent = `Project内に対応原稿がありません: ${result.root_path}`;
      return;
    }
    source.innerHTML = '<option value="">原稿を選択</option>';
    result.files.forEach((file) => {
      const option = document.createElement("option");
      option.value = file.relative_path;
      option.dataset.filename = file.name;
      option.textContent = `${file.relative_path} · ${String(file.source_format).toUpperCase()} · ${Number(file.size).toLocaleString()} bytes`;
      source.append(option);
    });
    source.disabled = false;
    status.textContent = `${result.files.length.toLocaleString()}件 · ${result.root_path}${result.truncated ? "（上限まで表示）" : ""}`;
  } catch (error) {
    source.innerHTML = '<option value="">原稿一覧を取得できません</option>';
    status.textContent = error.message;
    addProblem(error.message);
  }
}

function renderManuscriptImportPreview(preview) {
  const issues = preview.validation.issues.map((issue) =>
    `<li><strong>${escapeText(issue.severity)}</strong> ${escapeText(issue.code)}: ${escapeText(issue.message)}</li>`,
  ).join("");
  const warnings = (preview.extraction?.warnings || []).map((warning) =>
    `<li>${escapeText(warning)}</li>`,
  ).join("");
  const pageSummary = preview.extraction?.page_count
    ? ` · ${Number(preview.extraction.page_count)} pages`
    : "";
  const encodingSummary = preview.extraction?.encoding
    ? ` · ${escapeText(preview.extraction.encoding)}`
    : "";
  byId("manuscript-import-result").innerHTML = `
    <article class="review-card">
      <p><strong>${preview.validation.valid ? "Canonical validation passed" : "修正が必要"}</strong></p>
      <p class="muted">${escapeText(preview.project_name)} / ${escapeText(preview.document_title)}（${escapeText(preview.document_kind)}）</p>
      <p class="muted">Content Document ID ${escapeText(preview.document_id)}（Preview作成時に自動発行）</p>
      <p class="muted">${escapeText(String(preview.source_format).toUpperCase())} · ${Number(preview.extraction?.block_count || 0)} extracted blocks${pageSummary}${encodingSummary}</p>
      <p class="muted">Original unchanged: ${escapeText(String(preview.original_unchanged))} · SHA-256 ${escapeText(preview.source_sha256)}</p>
      ${warnings ? `<p><strong>抽出時の注意</strong></p><ul>${warnings}</ul>` : ""}
      ${issues ? `<p><strong>Canonical検証</strong></p><ul>${issues}</ul>` : ""}
      <textarea id="manuscript-import-proposed" rows="18">${escapeText(preview.proposed_html)}</textarea>
      <div class="review-actions">
        <button type="button" class="secondary-action" id="manuscript-import-validate">修正案を再検証</button>
        <button type="button" class="primary-action" id="manuscript-import-confirm" ${preview.validation.valid ? "" : "disabled"}>Revisionを確定</button>
      </div>
    </article>`;
  byId("manuscript-import-validate").addEventListener("click", async () => {
    try {
      const updated = await api(`/api/manuscript-import-previews/${encodeURIComponent(preview.id)}`, {
        method: "PUT",
        body: JSON.stringify({ canonical_html: byId("manuscript-import-proposed").value }),
      });
      renderManuscriptImportPreview(updated);
    } catch (error) {
      addProblem(error.message);
    }
  });
  byId("manuscript-import-confirm").addEventListener("click", async () => {
    try {
      const imported = await api(`/api/manuscript-import-previews/${encodeURIComponent(preview.id)}/confirm`, {
        method: "POST",
      });
      byId("manuscript-import-result").innerHTML = `
        <article class="review-card">
          <p><strong>${escapeText(imported.status)}</strong> · Revision ${escapeText(imported.imported_revision_id)}</p>
          <p class="muted">Canonical HTML: ${escapeText(imported.artifact_path || "not written")}</p>
          <p class="muted">Git ${escapeText(imported.checkpoint_sha || "checkpoint failed — retry required")}</p>
          ${manuscriptRewriteAvailable() ? '<button type="button" class="primary-action" id="manuscript-import-to-rewrite">この原稿の改稿へ進む</button>' : ""}
        </article>`;
      if (byId("manuscript-rewrite-document")) {
        byId("manuscript-rewrite-document").value = imported.document_id;
        byId("manuscript-rewrite-base").value = imported.imported_revision_id;
        byId("manuscript-import-to-rewrite")?.addEventListener("click", () => {
          byId("manuscript-rewrite-form").scrollIntoView({ behavior: "smooth", block: "start" });
          byId("manuscript-rewrite-instructions").focus();
        });
      }
    } catch (error) {
      addProblem(error.message);
    }
  });
}

async function startManuscriptRewrite(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const payload = {
    document_id: values.document_id,
    instructions: values.instructions,
    use_rag: values.use_rag === "on",
  };
  if (values.base_revision_id) payload.base_revision_id = values.base_revision_id;
  if (values.outline_node_id) payload.outline_node_id = values.outline_node_id;
  try {
    const created = await api("/api/manuscript-rewrites", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    byId("manuscript-rewrite-result").innerHTML = `
      <article class="review-card">
        <p><strong>改稿案を生成中</strong> · Run ${escapeText(created.run_id)}</p>
        <p class="muted">原稿・Outline・研究文脈を準備し、block単位の編集案を作成しています。Run欄は10秒ごとに更新されます。</p>
      </article>`;
    connectToRun(created.run_id);
    pollManuscriptRewrite(created.run_id);
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

function stopManuscriptRewritePolling() {
  if (state.manuscriptRewriteTimer) window.clearTimeout(state.manuscriptRewriteTimer);
  state.manuscriptRewriteTimer = null;
}

async function pollManuscriptRewrite(runId) {
  stopManuscriptRewritePolling();
  try {
    const proposal = await api(`/api/manuscript-rewrites/by-run/${encodeURIComponent(runId)}`);
    renderManuscriptRewriteProposal(proposal);
    return;
  } catch (error) {
    const currentRun = state.run?.run || state.run;
    if (error.status !== 404) {
      addProblem(error.message);
      return;
    }
    if (currentRun?.id === runId && ["failed", "cancelled"].includes(currentRun.status)) {
      byId("manuscript-rewrite-result").innerHTML = `
        <p class="error-text">改稿案を生成できませんでした。RunのエラーとEventsを確認してください。</p>`;
      return;
    }
  }
  state.manuscriptRewriteTimer = window.setTimeout(
    () => pollManuscriptRewrite(runId),
    10000,
  );
}

function updateReferenceSelectionSummary(fieldset) {
  const selectedCount = fieldset.querySelectorAll("input:checked").length;
  const summary = fieldset.querySelector(".reference-selection-summary");
  const button = fieldset.querySelector(".reference-select");
  if (summary) summary.textContent = `${selectedCount}件選択中`;
  if (button && fieldset.dataset.selectionMode === "multiple") {
    button.disabled = selectedCount === 0;
  }
}

const REFERENCE_METADATA_FIELD_LABELS = Object.freeze({
  author: "著者名",
  authors: "著者名",
  title: "書名・論文名",
  record_type: "資料種別",
  document_type: "資料種別（書籍・論文・章・未刊原稿）",
  year: "発表年",
  publication_year: "発表年",
  venue: "掲載先・出版社",
  venue_or_publisher: "掲載先または出版社",
  publisher: "出版社",
  container_title: "掲載誌・収録書",
  pages: "ページ",
  pages_if_applicable: "該当するページ",
  volume: "巻",
  issue: "号",
  volume_issue_if_applicable: "該当する巻号",
  edition: "版",
  is_translation: "原著／翻訳",
  translation_status: "原著／翻訳",
  translation_status_confirmation: "原著／翻訳の確認",
  translators: "訳者",
  is_self_authored: "自著かどうか",
  is_unpublished: "未刊資料かどうか",
});

function referenceMissingFields(item) {
  const fields = item.user_action_required?.fields;
  return Array.isArray(fields)
    ? [...new Set(fields.map((field) => String(field)).filter(Boolean))]
    : [];
}

function updateReferenceClassificationState(fieldset) {
  const classification = fieldset.querySelector(".reference-classification");
  if (!classification) return;
  const selected = [...classification.querySelectorAll('input[type="checkbox"]')]
    .filter((input) => input.checked);
  const requiresSelection = fieldset.dataset.confirmationKind === "unresolved_reference";
  const button = fieldset.querySelector(".reference-metadata-confirm");
  const status = classification.querySelector(".reference-classification-status");
  if (button && requiresSelection) button.disabled = selected.length === 0;
  if (status) {
    status.textContent = selected.length
      ? `選択中：${selected.map((input) => input.dataset.label).join("・")}`
      : requiresSelection
        ? "自著または未刊資料に該当する場合だけ選択してください。"
        : "資料の由来を確認してください。";
  }
}

function renderReferenceMetadataConfirmation(items, isPending) {
  if (!items.length) return "";
  return `<details open class="reference-metadata-review">
    <summary>最終Revision前に必要な書誌確認（${items.length}件）</summary>
    <p>原著／翻訳と書誌情報を人間が確定してください。機械判別参考文献は、確定後も「実際に参照した文献」には移動しません。</p>
    ${items.map((item, index) => {
      const referenceId = item.machine_reference_id || item.reference_id || "";
      const translationValue = item.is_translation === true || item.translation_status === "confirmed_translation"
        ? "true"
        : item.is_translation === false || item.translation_status === "confirmed_original"
          ? "false"
          : "";
      const isUnresolvedReference = item.confirmation_kind === "unresolved_reference";
      const hasClassificationControls = ["unresolved_reference", "self_or_unpublished"]
        .includes(item.confirmation_kind);
      const isConsultedManifestation = item.confirmation_kind === "consulted_manifestation";
      const isManualManifestation = isConsultedManifestation
        && item.manual_manifestation_entry === true;
      const kind = isUnresolvedReference
        ? "検索で同定できなかった参考文献"
        : item.confirmation_kind === "self_or_unpublished"
          ? "自著・未刊資料"
          : isConsultedManifestation
            ? "実際に参照した版・巻"
            : "機械判別参考文献";
      const manifestationIdentity = isConsultedManifestation
        ? [item.edition, item.volume, item.year, item.publisher]
          .map((value) => String(value || "").trim())
          .filter(Boolean)
          .join("・")
        : "";
      const missingFields = referenceMissingFields(item);
      const missingMarkup = missingFields.length || item.user_action_required?.message
        ? `<div class="reference-missing-fields" role="note">
            <strong>${isUnresolvedReference ? "文献の由来を確認してください" : "最終Revisionまでに必要な項目"}</strong>
            ${item.user_action_required?.message ? `<p>${escapeText(item.user_action_required.message)}</p>` : ""}
            ${missingFields.length ? `<p class="muted">未確認：${missingFields.map((field) =>
              escapeText(REFERENCE_METADATA_FIELD_LABELS[field] || field.replaceAll("_", " "))).join("・")}</p>` : ""}
          </div>`
        : "";
      const classificationMarkup = hasClassificationControls
        ? `<div class="reference-classification" role="group" aria-label="資料の由来">
            <strong>資料の由来</strong>
            <div class="reference-classification-options">
              <label><input type="checkbox" name="is_self_authored" data-label="自著" ${item.is_self_authored === true ? "checked" : ""}>自著</label>
              <label><input type="checkbox" name="is_unpublished" data-label="未刊資料" ${item.is_unpublished === true ? "checked" : ""}>未刊資料</label>
            </div>
            <p class="muted">自著と未刊資料の両方に該当する場合は、両方にチェックを付けてください。どちらにも該当しない場合はこの欄では確定せず、改稿コメントで著者・書名または検索条件の修正を指示してください。</p>
            <p class="reference-classification-status muted" role="status" aria-live="polite"></p>
          </div>`
        : "";
      const input = (name, label, value = "", placeholder = "") => `
        <label><span>${label}</span><input name="${name}" value="${escapeText(value || "")}" data-original-value="${escapeText(value || "")}" placeholder="${escapeText(placeholder)}" autocomplete="off"></label>`;
      return `<fieldset class="reference-choice reference-metadata-confirmation"
        data-reference-id="${escapeText(referenceId)}"
        data-confirmation-kind="${escapeText(item.confirmation_kind || "machine_detected")}">
        <legend>${escapeText(kind)}：${escapeText(item.title || "書名未確認")}${manifestationIdentity ? `（${escapeText(manifestationIdentity)}）` : ""}</legend>
        <p class="muted">この操作は書誌メタデータだけを確定します。本文とRevisionは承認しません。</p>
        ${isConsultedManifestation ? `<p class="muted">${isManualManifestation
          ? "検索候補と一致しなかったため、実際に参照した版・巻を手入力で確定します。自著・未刊資料への分類は不要です。"
          : "選択済みのこの版だけを補完します。ほかの版・巻・翻訳の情報は変更しません。"}</p>` : ""}
        ${missingMarkup}
        ${classificationMarkup}
        <div class="reference-metadata-grid">
          ${input("author", "著者名", item.author || (item.authors || []).join("・"), "著者名")}
          ${input("title", "書名・論文名", item.title, "書名または論文名")}
          <label><span>資料種別</span><select name="record_type" ${item.confirmation_kind === "self_or_unpublished" ? "required" : ""}>
            <option value="">未指定</option>
            ${[["book", "書籍"], ["article", "論文"], ["chapter", "書籍所収章"], ["unpublished", "未刊原稿"]].map(([value, label]) =>
              `<option value="${value}" ${String(item.record_type || item.bibliography_kind || "") === value ? "selected" : ""}>${label}</option>`).join("")}
          </select></label>
          <label><span>原著／翻訳</span><select name="is_translation" required>
            <option value="" ${translationValue === "" ? "selected" : ""}>選択してください</option>
            <option value="false" ${translationValue === "false" ? "selected" : ""}>原著・翻訳ではない</option>
            <option value="true" ${translationValue === "true" ? "selected" : ""}>翻訳書</option>
          </select></label>
          ${input("translators", "訳者（複数は／区切り）", (item.translators || []).join("／"), "例：山田太郎／佐藤花子")}
          ${input("original_title", "原題", item.original_title, "原著の題名")}
          ${input("translated_title", "邦題・翻訳題", item.translated_title, "翻訳版の題名")}
          ${input("year", "発表年", item.year, "例：2026")}
          ${input("venue", "掲載先・出版社", item.venue || item.container_title || item.publisher, "掲載誌、大学、出版社など")}
          ${input("pages", "ページ", item.pages, "例：10-24")}
          ${input("volume", "巻", item.volume)}
          ${input("issue", "号", item.issue)}
          ${input("edition", "版", item.edition, "例：改訳版")}
        </div>
        ${isPending ? `<button type="button" class="secondary-action reference-metadata-confirm" data-reference-index="${index}"
          ${isUnresolvedReference && item.is_self_authored !== true && item.is_unpublished !== true ? "disabled" : ""}>${isUnresolvedReference ? "分類して書誌情報を確定" : isManualManifestation ? "実際に参照した版・巻を登録" : "書誌情報を確定"}</button>` : ""}
        <p class="reference-status muted" role="status" aria-live="polite"></p>
      </fieldset>`;
    }).join("")}
  </details>`;
}

const SEMANTIC_REVIEW_KIND_LABELS = {
  term_introduction: "初出語の意味",
  citation_function: "引用の論証機能",
  claim_causality: "主張の因果関係",
  semantic_review: "意味上の判断",
};

function renderSemanticReviewAdjudication(items, isPending) {
  if (!items.length) return "";
  return `<details class="semantic-review-panel" open>
    <summary>人間による意味判定 ${items.length}件</summary>
    <p class="muted">各項目は個別に判定します。canonical HTMLを手修正しただけでは解消されません。判断と理由を明示して記録してください。</p>
    ${items.map((item, index) => {
      const kind = SEMANTIC_REVIEW_KIND_LABELS[item.kind] || item.kind || "意味上の判断";
      const group = Array.isArray(item.group_unit_ids) && item.group_unit_ids.length > 1
        ? `<p class="muted semantic-review-group">同一グループ ${item.group_unit_ids.length}件中の処理単位：<code>${escapeText(item.unit_id || "")}</code></p>`
        : item.unit_id ? `<p class="muted">処理単位：<code>${escapeText(item.unit_id)}</code></p>` : "";
      const relation = [
        item.term ? `用語：${item.term}` : "",
        item.cause ? `原因：${item.cause}` : "",
        item.effect ? `結果：${item.effect}` : "",
      ].filter(Boolean).join(" / ");
      return `<fieldset class="semantic-review-item" data-review-id="${escapeText(item.review_id)}">
        <legend>${index + 1}. ${escapeText(kind)}</legend>
        ${item.block_ids?.length ? `<p>本文箇所：<code>${escapeText(item.block_ids.join(" / "))}</code></p>` : ""}
        ${group}
        ${relation ? `<p>${escapeText(relation)}</p>` : ""}
        <p>${escapeText(item.explanation || "意味・論証関係を人間が確定する必要があります。")}</p>
        ${isPending ? `<label>判断
          <select name="semantic_decision" required>
            <option value="">選択してください</option>
            <option value="confirm_current_interpretation">現行稿の意味・関係を妥当と判断</option>
            <option value="clarify_interpretation">理由欄の説明により意味・関係を確定</option>
            <option value="not_applicable">この不確定項目は該当しないと判断</option>
          </select></label>
          <label>判断理由
            <textarea name="semantic_rationale" rows="4" required placeholder="何をどのように判断したか、根拠とともに記入してください。"></textarea>
          </label>
          <button type="button" class="secondary-action semantic-review-resolve">この意味判断を記録</button>
          <p class="semantic-review-status muted" role="status" aria-live="polite"></p>` : ""}
      </fieldset>`;
    }).join("")}
  </details>`;
}

function renderManuscriptRewriteProposal(proposal) {
  stopManuscriptRewritePolling();
  const bulkApproval = manuscriptRewriteWorkflowVersion(proposal) >= 24;
  const isPending = ["pending", "needs_human_review"].includes(proposal.status);
  const needsHumanReview = proposal.status === "needs_human_review";
  const isConfirmed = ["confirmed", "checkpoint_failed"].includes(proposal.status);
  const responseContractIncomplete = manuscriptResponseContractIncomplete(proposal);
  const semanticResolutionRequired = manuscriptSemanticResolutionRequired(proposal);
  const referenceApprovalRequired = manuscriptReferenceApprovalRequired(proposal);
  const structuralApprovalBlocked = bulkApproval && !manuscriptFinalApprovalAllowed(proposal);
  const finalApprovalWarningIds = (bulkApproval ? [] : [
    responseContractIncomplete ? "manuscript-response-contract-warning" : "",
    semanticResolutionRequired ? "manuscript-semantic-resolution-warning" : "",
    referenceApprovalRequired ? "manuscript-reference-approval-warning" : "",
  ]).filter(Boolean).join(" ");
  const evidence = proposal.context?.evidence || [];
  const references = proposal.context?.reference_entries || [];
  const referenceMetadataItems = proposal.context?.reference_metadata_confirmation_items || [];
  const semanticReviewItems = proposal.context?.semantic_review_items || [];
  const usesManifestationSelection = manuscriptRewriteWorkflowVersion(proposal) >= 22;
  const referenceMarkup = references.filter((entry) => entry.candidate_matches?.length).map((entry) => {
    const selectedIds = new Set(
      usesManifestationSelection
        ? entry.selected_candidate_ids || []
        : entry.selected_candidate_id ? [entry.selected_candidate_id] : [],
    );
    const selectionMode = usesManifestationSelection ? "multiple" : "single";
    const inputType = usesManifestationSelection ? "checkbox" : "radio";
    return `
      <fieldset class="reference-choice" data-reference-id="${escapeText(entry.reference_id)}"
        data-selection-mode="${selectionMode}">
        <legend>${escapeText(entry.author || "")}『${escapeText(entry.title || "参考文献")}』</legend>
        <p>${escapeText(entry.candidate_display || entry.candidate_matches.map((candidate) =>
          `候補〔${(candidate.authors || []).join("・")}・${candidate.title || ""}・${(candidate.translators || []).join("・") || "訳者未確認"}・${candidate.publisher || "出版社未確認"}・${candidate.year || "年未確認"}・${candidate.volume || "巻次未確認"}・${candidate.url || ""}〕`
        ).join("／"))}</p>
        ${entry.search_completeness?.status === "search_incomplete" ? `<p class="status-warning reference-search-completeness"><strong>${escapeText(entry.search_completeness.display_label || "確認できた候補すべて（網羅性未確認）")}</strong><br>件数制限付き・ページ送りなしの検索結果です。実際に参照した版・巻を確認してください。</p>` : ""}
        ${usesManifestationSelection ? '<p class="muted reference-selection-help">実際に参照した版・巻をすべて選択してください。</p>' : ""}
        ${entry.candidate_matches.map((candidate, index) => `
          <div class="reference-option-row">
            <label class="reference-option"><input type="${inputType}" name="ref-${escapeText(entry.reference_id)}"
              value="${escapeText(candidate.candidate_id)}" ${selectedIds.has(candidate.candidate_id) ? "checked" : ""}>
              <span>候補${index + 1}：${escapeText(candidate.publisher || "出版社未確認")}・${escapeText(candidate.year || "年未確認")}・${escapeText(candidate.volume || "巻次未確認")}・${escapeText(candidate.edition || "版未確認")}</span></label>
            ${/^https?:\/\//i.test(candidate.url || "") ? `<a href="${escapeText(candidate.url)}" target="_blank" rel="noopener noreferrer">出典を確認</a>` : ""}
          </div>
        `).join("")}
        ${usesManifestationSelection ? `<p class="reference-selection-summary" role="status" aria-live="polite">${selectedIds.size}件選択中</p>` : ""}
        ${["pending", "needs_human_review"].includes(proposal.status) ? `<button type="button" class="secondary-action reference-select" ${usesManifestationSelection && selectedIds.size === 0 ? "disabled" : ""}>${usesManifestationSelection ? "選択した書誌を確定" : "この候補の書誌を適用"}</button>` : ""}
        <p class="reference-status muted" role="status" aria-live="polite"></p>
      </fieldset>`;
  }).join("");
  const evidenceMarkup = evidence.length
    ? `<ul>${evidence.map((item) => {
        const citation = item.citation || {};
        const pages = citation.start_page
          ? ` p.${escapeText(citation.start_page)}${citation.end_page && citation.end_page !== citation.start_page ? `–${escapeText(citation.end_page)}` : ""}`
          : "";
        return `<li><strong>${escapeText(item.title || item.chunk_id)}</strong>${pages} · ${escapeText(item.retrieval || "context")}</li>`;
      }).join("")}</ul>`
    : '<p class="muted">参照したCorpus資料はありません。</p>';
  const warnings = proposal.edit_summary?.warnings || [];
  const unresolved = proposal.edit_summary?.unresolved_questions || [];
  const completionGate = proposal.context?.completion_gate || {};
  const qualityBlockers = completionGate.grouped_blocking_reasons || completionGate.blocking_reasons || [];
  const deferredCoherence = proposal.context?.coherence_deferred_reviews || [];
  const conditional = proposal.context?.conditional_success || {};
  const researchRequirements = conditional.research_requirements || [];
  const conditionalItems = conditional.reason === "coherence_unresolved"
    ? conditional.unresolved_routes || []
    : researchRequirements;
  const inferenceMarks = proposal.context?.enumeration_inference_marks || [];
  byId("manuscript-rewrite-result").innerHTML = `
    <article class="review-card">
      <p><strong>${needsHumanReview ? "改稿案（人間レビュー必須）" : isPending ? "改稿案（未確定）" : isConfirmed ? "承認済み原稿" : "改稿案（破棄済み）"}</strong> · ${Number(proposal.edit_summary?.applied_edit_count || 0)} edits</p>
      <p class="muted">Base Revision ${escapeText(proposal.base_revision_id)} · 元Revisionは変更されていません。</p>
      ${conditional.status === "conditional_success" ? `
        <div class="status-warning">
          <strong>${conditional.reason === "coherence_unresolved"
            ? "条件付き成功：原稿全体の確認が必要"
            : conditional.reason === "additional_research_required"
              ? "条件付き成功：追加研究待ち"
              : "条件付き成功：一括レビューが必要"}</strong>
          <p>${isPending
            ? "現時点の改稿稿を保存しました。未解決事項は修正済み・検証済みとは扱いません。原稿と差分を一括して確認してください。"
            : "承認時に残っていた未解決事項の記録です。文章の承認後も検証済みとは扱いません。"}</p>
          ${conditionalItems.length ? `<details><summary>未解決ルート ${conditionalItems.length}件を確認</summary>
            <ul>${conditionalItems.map((item) => `<li><strong>${escapeText(item.resolution_route || item.category || "追加研究")}</strong> ${escapeText(item.recommendation || item.research_query || item.explanation || "確認が必要です")}${item.block_ids?.length ? `<small> 対象: ${escapeText(item.block_ids.join(" / "))}</small>` : ""}</li>`).join("")}</ul>
          </details>` : ""}
        </div>` : ""}
      ${deferredCoherence.length ? `
        <div class="status-warning" id="manuscript-coherence-final-review" role="note">
          <strong>${isPending ? "整合性に関する編集判断を最終確認してください" : "整合性に関する編集判断の記録"}</strong>
          <p>${isPending ? "途中の承認は行っていません。以下の指摘と、この画面の原稿・差分を確認してから、改稿案を承認または破棄してください。" : "最終判断時に提示された整合性の指摘です。"}</p>
          <ul>${deferredCoherence.flatMap((item) => item.routes || []).map((route) => `
            <li><strong>${escapeText(route.category || "整合性")}</strong>
              ${escapeText(route.explanation || route.recommendation || "編集判断が必要です")}
              ${route.block_ids?.length ? `<small>対象: ${escapeText(route.block_ids.join(" / "))}</small>` : ""}
            </li>`).join("")}</ul>
        </div>` : ""}
      ${bulkApproval && (responseContractIncomplete || semanticResolutionRequired || referenceApprovalRequired) ? `
        <div class="status-warning" id="manuscript-bulk-unresolved-warning" role="note">
          <strong>未解決事項を引き継ぎます</strong>
          <p>文章の一括承認は、推論・意味判断・書誌の未確認事項を検証済みにしません。原稿全体と差分を確認し、必要な調査は承認後も続けてください。</p>
        </div>` : ""}
      ${structuralApprovalBlocked ? `
        <div class="status-warning" id="manuscript-v24-structure-warning" role="alert">
          <strong>この案は一括承認できません</strong>
          <p>canonical HTML、構造計画、または段落境界の検証が未完了です。再改稿または手修正の検証後に確認してください。</p>
        </div>` : ""}
      ${!bulkApproval && responseContractIncomplete ? `
        <div class="status-warning" id="manuscript-response-contract-warning" role="alert">
          <strong>最終Revisionへ承認できません</strong>
          <p>必須推論の応答には、欠落を1回補充した後も未完了の処理が残っています。改稿案を確認し、必要に応じて手修正してください。未完了の処理を解消するまで承認は停止されます。</p>
        </div>` : ""}
      ${!bulkApproval && semanticResolutionRequired ? `
        <div class="status-warning" id="manuscript-semantic-resolution-warning" role="alert">
          <strong>意味上の判断が必要です</strong>
          <p>初出語の意味、引用の論証機能、または主張の因果関係に人間確認が残っています。修正内容は改稿案として保存してください。下の項目ごとに判断を記録するまで最終Revisionの承認は停止されます。HTML手修正だけでは意味判定済みになりません。</p>
        </div>` : ""}
      ${bulkApproval ? "" : renderSemanticReviewAdjudication(semanticReviewItems, isPending)}
      ${!bulkApproval && referenceApprovalRequired ? `
        <div class="status-warning" id="manuscript-reference-approval-warning" role="alert">
          <strong>参考文献の確定が必要です</strong>
          <p>実際に参照した版・巻・原著／翻訳・訳者、または自著・未刊資料の書誌情報に未確定項目があります。改稿案を確認し、書誌情報を確定してください。書誌確認を終えるまで最終Revisionの承認は停止されます。</p>
        </div>` : ""}
      <p><strong>使用した研究文脈</strong></p>
      ${evidenceMarkup}
      ${warnings.length ? `<p><strong>適用しなかった変更・注意</strong></p><ul>${warnings.map((item) => `<li>${escapeText(item)}</li>`).join("")}</ul>` : ""}
      ${unresolved.length ? `<p><strong>未解決事項</strong></p><ul>${unresolved.map((item) => `<li>${escapeText(item)}</li>`).join("")}</ul>` : ""}
      ${inferenceMarks.length ? `<details><summary>LLM推論より導出 ${inferenceMarks.length}件</summary><ul>${inferenceMarks.map((item) => `<li><code>${escapeText(item.block_id || "block")}</code> ${escapeText(item.item || "")} · LLM推論より導出</li>`).join("")}</ul></details>` : ""}
      ${qualityBlockers.length ? `<details><summary>未解決の指摘（主張・本文箇所ごとに集約）${qualityBlockers.length}件</summary><ul>${qualityBlockers.map((item) => `<li><code>${escapeText(item.category || item.code || "quality")}</code> ${escapeText(item.message || item.claim || "")} ${item.block_ids?.length ? `<small>本文箇所: ${escapeText(item.block_ids.join(" / "))}</small>` : ""}${item.history?.length > 1 ? `<details><summary>同じ指摘の履歴 ${item.history.length}件</summary><ul>${item.history.map(h => `<li>${escapeText(h.message || h.code || "")}</li>`).join("")}</ul></details>` : ""}</li>`).join("")}</ul></details>` : ""}
      <details>
        <summary>llm-jp表現候補と人間の選択履歴</summary>
        <div id="manuscript-polish-candidates"><p class="muted">候補を読み込み中…</p></div>
      </details>
      ${referenceMarkup ? `<details><summary>参考文献候補：版・巻次を選択</summary>
        <p>候補は自動で確定しません。出典を確認して選択してください。書誌だけを変更し、本文・Revisionは承認しません。</p>
        ${referenceMarkup}</details>` : ""}
      ${bulkApproval ? "" : renderReferenceMetadataConfirmation(referenceMetadataItems, isPending)}
      <div class="review-actions">
        <button type="button" class="secondary-action" id="manuscript-rewrite-html"
          data-proposal-id="${escapeText(proposal.id)}"
          data-draft-title="${escapeText(proposal.context?.document_title || "原稿改稿案")}">HTMLでドラフト確認</button>
      </div>
      <details open><summary>原稿diff</summary><pre class="revision-diff-view">${escapeText(proposal.unified_diff || "変更なし")}</pre></details>
      <label for="manuscript-rewrite-proposed">提案canonical HTML</label>
      <textarea id="manuscript-rewrite-proposed" rows="20" ${isPending ? "" : "readonly"}>${escapeText(proposal.proposed_html)}</textarea>
      ${isPending ? `
        <div class="review-actions manuscript-html-actions">
          <button type="button" class="secondary-action" id="manuscript-rewrite-save-html">HTML手修正を保存・検証</button>
        </div>
        <section class="manuscript-revision-request" aria-labelledby="manuscript-revision-request-title">
          <h3 id="manuscript-revision-request-title">コメントに基づいて再改稿</h3>
          <p class="muted">現在の改稿案を入力として、コメントを人間レビューへ渡し、新しい改稿Runを開始します。元のProposalとRevisionは変更しません。</p>
          <label class="review-field" for="manuscript-rewrite-feedback">修正コメント
            <textarea id="manuscript-rewrite-feedback" rows="6" required placeholder="例：第6節の80字の移行段落を直前の意味段落へ統合し、985字の段落は論点の転換箇所で分割する。"></textarea>
          </label>
          <p class="manuscript-revision-status muted" id="manuscript-revision-status" role="status" aria-live="polite"></p>
          <div class="review-actions">
            <button type="button" class="secondary-action" id="manuscript-rewrite-validate">コメントを反映して再改稿</button>
          </div>
        </section>` : ""}
      <div class="review-actions${bulkApproval && isPending ? " manuscript-bulk-review-actions" : ""}">
        ${isPending ? `
          ${bulkApproval ? `<label class="manuscript-bulk-acknowledgement" for="manuscript-bulk-acknowledge">
            <input type="checkbox" id="manuscript-bulk-acknowledge">
            この原稿全体と差分、未解決事項を確認しました。文章の変更を一括承認し、未確認事項は未解決のまま引き継ぎます。
          </label>` : ""}
          <button type="button" class="primary-action" id="manuscript-rewrite-confirm"
            ${manuscriptFinalApprovalAllowed(proposal) && !bulkApproval ? "" : "disabled"}
            ${structuralApprovalBlocked ? 'aria-describedby="manuscript-v24-structure-warning"' : ""}
            ${finalApprovalWarningIds
              ? `aria-describedby="${finalApprovalWarningIds}" title="未解決の必須確認を解消するまで承認できません"`
              : ""}>${bulkApproval ? "原稿全体の変更を一括承認" : "この改稿を新Revisionとして承認"}</button>
          <button type="button" class="danger-action" id="manuscript-rewrite-discard"
            data-proposal-id="${escapeText(proposal.id)}">この改稿案を破棄</button>`
          : ""}
      </div>
    </article>`;
  byId("manuscript-rewrite-html").addEventListener("click", openManuscriptDraftPreview);
  byId("manuscript-bulk-acknowledge")?.addEventListener("change", (event) => {
    const button = byId("manuscript-rewrite-confirm");
    if (button) button.disabled = !event.currentTarget.checked || !manuscriptFinalApprovalAllowed(proposal);
  });
  renderManuscriptPolishCandidates(proposal.id);
  document.querySelectorAll('.reference-choice[data-selection-mode="multiple"]').forEach((fieldset) => {
    fieldset.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.addEventListener("change", () => updateReferenceSelectionSummary(fieldset));
    });
  });
  document.querySelectorAll(".reference-select").forEach((button) => {
    button.addEventListener("click", async () => {
      const fieldset = button.closest("fieldset");
      const selected = [...fieldset.querySelectorAll("input:checked")];
      const status = fieldset.querySelector(".reference-status");
      if (!selected.length) { status.textContent = "候補を選択してください。"; return; }
      if (byId("manuscript-rewrite-proposed").value !== proposal.proposed_html) {
        status.textContent = "先にHTML手修正を保存してください。未保存内容は上書きしません。";
        return;
      }
      button.disabled = true;
      status.textContent = "書誌を適用中…";
      try {
        const payload = { reference_id: fieldset.dataset.referenceId,
          expected_sha256: proposal.proposed_sha256 };
        if (fieldset.dataset.selectionMode === "multiple") {
          payload.candidate_ids = selected.map((input) => input.value);
        } else {
          payload.candidate_id = selected[0].value;
        }
        const updated = await api(`/api/manuscript-rewrites/${encodeURIComponent(proposal.id)}/reference-selection`, {
          method: "POST", body: JSON.stringify(payload),
        });
        renderManuscriptRewriteProposal(updated);
      } catch (error) { status.textContent = error.message; button.disabled = false; }
    });
  });
  document.querySelectorAll(".reference-metadata-confirm").forEach((button) => {
    button.addEventListener("click", async () => {
      const fieldset = button.closest("fieldset");
      const status = fieldset.querySelector(".reference-status");
      const classificationInputs = [...fieldset.querySelectorAll(
        '.reference-classification input[type="checkbox"]',
      )];
      if (
        fieldset.dataset.confirmationKind === "unresolved_reference"
        && !classificationInputs.some((input) => input.checked)
      ) {
        status.textContent = "自著または未刊資料に該当する場合だけ分類を選択してください。";
        return;
      }
      if (byId("manuscript-rewrite-proposed").value !== proposal.proposed_html) {
        status.textContent = "先にHTML手修正を保存してください。未保存内容は上書きしません。";
        return;
      }
      const translation = fieldset.querySelector('[name="is_translation"]').value;
      if (!translation) {
        status.textContent = "原著か翻訳書かを選択してください。";
        fieldset.querySelector('[name="is_translation"]').focus();
        return;
      }
      const translatorInput = fieldset.querySelector('[name="translators"]');
      const translators = translatorInput.value
        .split(/[、,，／/]/).map((value) => value.trim()).filter(Boolean);
      if (translation === "true" && !translators.length) {
        status.textContent = "翻訳書には確認済みの訳者を入力してください。";
        translatorInput.focus();
        return;
      }
      const payload = {
        expected_sha256: proposal.proposed_sha256,
        is_translation: translation === "true",
        translators,
      };
      if (classificationInputs.length) {
        payload.is_self_authored = fieldset.querySelector('[name="is_self_authored"]').checked;
        payload.is_unpublished = fieldset.querySelector('[name="is_unpublished"]').checked;
      }
      for (const field of ["author", "title", "record_type", "original_title", "translated_title", "year", "venue", "pages", "volume", "issue", "edition"]) {
        const input = fieldset.querySelector(`[name="${field}"]`);
        const value = input.value.trim();
        const unchangedIdentityField = ["author", "title"].includes(field)
          && value === String(input.dataset.originalValue || "").trim();
        if (value && !unchangedIdentityField) payload[field] = value;
      }
      button.disabled = true;
      status.textContent = "書誌情報を確認中…";
      try {
        const updated = await api(
          `/api/manuscript-rewrites/${encodeURIComponent(proposal.id)}/machine-references/${encodeURIComponent(fieldset.dataset.referenceId)}`,
          { method: "PATCH", body: JSON.stringify(payload) },
        );
        renderManuscriptRewriteProposal(updated);
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll(".reference-metadata-confirmation").forEach((fieldset) => {
    fieldset.querySelectorAll('.reference-classification input[type="checkbox"]').forEach((input) => {
      input.addEventListener("change", () => updateReferenceClassificationState(fieldset));
    });
    updateReferenceClassificationState(fieldset);
  });
  document.querySelectorAll(".semantic-review-resolve").forEach((button) => {
    button.addEventListener("click", async () => {
      const fieldset = button.closest("fieldset");
      const status = fieldset.querySelector(".semantic-review-status");
      if (byId("manuscript-rewrite-proposed").value !== proposal.proposed_html) {
        status.textContent = "先にHTML手修正を保存してください。保存だけでは意味判定は完了しません。";
        return;
      }
      const decision = fieldset.querySelector('[name="semantic_decision"]').value;
      const rationaleInput = fieldset.querySelector('[name="semantic_rationale"]');
      const rationale = rationaleInput.value.trim();
      if (!decision) {
        status.textContent = "判断を選択してください。";
        fieldset.querySelector('[name="semantic_decision"]').focus();
        return;
      }
      if (!rationale) {
        status.textContent = "判断理由を入力してください。";
        rationaleInput.focus();
        return;
      }
      button.disabled = true;
      status.textContent = "意味判断を記録中…";
      try {
        const updated = await api(
          `/api/manuscript-rewrites/${encodeURIComponent(proposal.id)}/semantic-reviews/${encodeURIComponent(fieldset.dataset.reviewId)}`,
          {
            method: "PATCH",
            body: JSON.stringify({
              expected_sha256: proposal.proposed_sha256,
              expected_context_sha256: proposal.semantic_review_context_sha256,
              decision,
              rationale,
            }),
          },
        );
        renderManuscriptRewriteProposal(updated);
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  });
  byId("manuscript-rewrite-discard")?.addEventListener("click", discardManuscriptRewrite);
  byId("manuscript-rewrite-save-html")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const updated = await api(`/api/manuscript-rewrites/${encodeURIComponent(proposal.id)}`, {
        method: "PUT",
        body: JSON.stringify({ canonical_html: byId("manuscript-rewrite-proposed").value }),
      });
      renderManuscriptRewriteProposal(updated);
    } catch (error) {
      addProblem(error.message);
      button.disabled = false;
    }
  });
  byId("manuscript-rewrite-validate")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const feedback = byId("manuscript-rewrite-feedback").value.trim();
    const status = byId("manuscript-revision-status");
    if (!feedback) {
      status.textContent = "修正コメントを入力してください。";
      status.classList.add("error-text");
      byId("manuscript-rewrite-feedback").focus();
      return;
    }
    button.disabled = true;
    button.textContent = "改稿Runを作成中…";
    status.classList.remove("error-text");
    status.textContent = "現在のProposalとコメントを改稿ワークフローへ渡しています。";
    try {
      const created = await api(`/api/manuscript-rewrites/${encodeURIComponent(proposal.id)}/revise`, {
        method: "POST",
        body: JSON.stringify({ human_review: feedback }),
      });
      byId("manuscript-rewrite-result").innerHTML = `
        <article class="review-card">
          <p><strong>コメントに基づく再改稿を開始しました</strong> · Run ${escapeText(created.run_id)}</p>
          <p class="muted">元Proposal ${escapeText(proposal.id)} は変更せず保持しています。新しい改稿案が完成すると、この欄へ表示します。</p>
        </article>`;
      connectToRun(created.run_id);
      pollManuscriptRewrite(created.run_id);
    } catch (error) {
      addProblem(error.message);
      status.textContent = error.message;
      status.classList.add("error-text");
      button.disabled = false;
      button.textContent = "コメントを反映して再改稿";
    }
  });
  byId("manuscript-rewrite-confirm")?.addEventListener("click", async (event) => {
    if (bulkApproval && byId("manuscript-rewrite-proposed").value !== proposal.proposed_html) {
      addProblem("未保存のHTML手修正があります。保存・検証してから原稿全体を確認してください。");
      return;
    }
    if (bulkApproval && !byId("manuscript-bulk-acknowledge")?.checked) return;
    event.currentTarget.disabled = true;
    try {
      const confirmed = await api(`/api/manuscript-rewrites/${encodeURIComponent(proposal.id)}/confirm`, {
        method: "POST",
        ...(bulkApproval ? { body: JSON.stringify({
          expected_sha256: proposal.proposed_sha256,
          acknowledge_unresolved: true,
        }) } : {}),
      });
      byId("manuscript-rewrite-result").innerHTML = `
        <article class="review-card">
          <p><strong>${bulkApproval ? "原稿全体の変更を一括承認しました" : "改稿を承認しました"}</strong> · Revision ${escapeText(confirmed.confirmed_revision_id)}</p>
          ${bulkApproval && confirmed.context?.bulk_approval?.status === "approved_with_unresolved"
            ? '<p class="status-warning">未解決事項はRevisionの承認記録に残っています。事実確認・追加調査は完了扱いになっていません。</p>' : ""}
          <dl class="approval-output">
            <div><dt>出力先フォルダ</dt><dd><code>${escapeText(confirmed.output_folder_path || "未確定")}</code></dd></div>
            <div><dt>出力ファイル</dt><dd><code>${escapeText(confirmed.artifact_path || "未確定")}</code></dd></div>
          </dl>
          <p class="muted">元Revisionは履歴に保持されています。Git ${escapeText(confirmed.checkpoint_sha || "checkpoint failed — retry required")}</p>
          <button class="secondary-action" type="button" id="manuscript-confirm-review-queue">レビュー待ち一覧を開く</button>
        </article>`;
      byId("manuscript-confirm-review-queue").addEventListener("click", renderReviewEditor);
    } catch (error) {
      addProblem(error.message);
      event.currentTarget.disabled = !manuscriptFinalApprovalAllowed(proposal)
        || (bulkApproval && !byId("manuscript-bulk-acknowledge")?.checked);
    }
  });
}

async function renderManuscriptPolishCandidates(proposalId) {
  const container = byId("manuscript-polish-candidates");
  if (!container) return;
  try {
    const packet = await api(`/api/manuscript-rewrites/${encodeURIComponent(proposalId)}/polish-candidates`);
    if (!packet.targets.length) {
      container.innerHTML = '<p class="muted">独立した表現候補が必要な問題箇所は検出されませんでした。</p>';
      return;
    }
    const progress = `${Number(packet.preference_count || 0)}/${Number(packet.human_training_target || 100)}`;
    container.innerHTML = `
      <p class="muted">人間選択の蓄積 ${escapeText(progress)}（順位づけの参考情報）。履歴数や候補数だけでは自動適用を止めません。候補はllm-jpが生成し、順位は独立した検証器が決定しています。</p>
      ${packet.targets.map((target) => `
        <section class="polish-candidate-target" data-target-id="${escapeText(target.id)}">
          <p><strong>${escapeText(target.block_id)}</strong> · ${escapeText((target.issue_codes || []).join(" / "))}</p>
          <blockquote>${escapeText(target.source_text)}</blockquote>
          <p class="muted">${escapeText(({
            no_revision_needed: "修正不要と判断", candidate_generation_failed: "候補を生成できなかった",
            candidates_inappropriate: "候補が不適切", semantic_analysis_unresolved: "意味解析が未確定",
            human_selection_pending: "人間の選択待ち", mandatory_revision_unmet: "必須修正が未達",
            independent_auto: "独立検査後に適用", block_invariant_original: "適用後検査で復元・必須修正未達",
          })[(target.candidates || []).find((candidate) => candidate.selected)?.selection_mode] || "判定記録を確認中")}</p>
          <div class="polish-candidate-options">
            ${(target.candidates || []).map((candidate) => `
              <button type="button"
                class="secondary-action manuscript-polish-choice ${candidate.selected ? "is-selected" : ""}"
                data-target-id="${escapeText(target.id)}"
                data-candidate-id="${escapeText(candidate.id)}">
                <span>${escapeText(candidate.text)}</span>
                <small>${escapeText(candidate.source_kind === "original" ? "原文維持" : candidate.relation_type)} · score ${escapeText(candidate.total_score ?? "-")}${candidate.selected ? " · 選択中" : ""}</small>
              </button>`).join("")}
          </div>
          ${Number(target.vetoed_candidate_count || 0) ? `<p class="muted">強制棄却 ${Number(target.vetoed_candidate_count)}件（選択肢には表示していません）</p>` : ""}
        </section>`).join("")}`;
    container.querySelectorAll(".manuscript-polish-choice").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/api/manuscript-polish-targets/${encodeURIComponent(button.dataset.targetId)}/select`, {
            method: "POST",
            body: JSON.stringify({
              candidate_id: button.dataset.candidateId,
              scope_type: "document",
            }),
          });
          await renderManuscriptPolishCandidates(proposalId);
        } catch (error) {
          addProblem(error.message);
          button.disabled = false;
        }
      });
    });
  } catch (error) {
    container.innerHTML = `<p class="error-text">表現候補を取得できません: ${escapeText(error.message)}</p>`;
  }
}

function renderHTMLImportPreview(preview) {
  const issues = preview.validation.issues.map((issue) =>
    `<li><strong>${escapeText(issue.severity)}</strong> ${escapeText(issue.code)}: ${escapeText(issue.message)}</li>`,
  ).join("");
  byId("html-import-result").innerHTML = `
    <article class="review-card">
      <p><strong>${preview.validation.valid ? "Canonical validation passed" : "修正が必要"}</strong></p>
      <p class="muted">Original unchanged: ${escapeText(String(preview.original_unchanged))} · SHA-256 ${escapeText(preview.source_sha256)}</p>
      ${issues ? `<ul>${issues}</ul>` : ""}
      <textarea id="html-import-proposed" rows="16">${escapeText(preview.proposed_html)}</textarea>
      <div class="review-actions">
        <button class="secondary-action" id="html-import-validate">修正案を再検証</button>
        <button class="primary-action" id="html-import-confirm" ${preview.validation.valid ? "" : "disabled"}>Revisionを確定</button>
      </div>
    </article>`;
  byId("html-import-validate").addEventListener("click", async () => {
    try {
      const updated = await api(`/api/html-import-previews/${encodeURIComponent(preview.id)}`, {
        method: "PUT",
        body: JSON.stringify({ canonical_html: byId("html-import-proposed").value }),
      });
      renderHTMLImportPreview(updated);
    } catch (error) {
      addProblem(error.message);
    }
  });
  byId("html-import-confirm").addEventListener("click", async () => {
    try {
      const imported = await api(`/api/html-import-previews/${encodeURIComponent(preview.id)}/confirm`, {
        method: "POST",
      });
      byId("html-import-result").innerHTML = `
        <article class="review-card">
          <p><strong>${escapeText(imported.status)}</strong> · Revision ${escapeText(imported.imported_revision_id)}</p>
          <p class="muted">Git ${escapeText(imported.checkpoint_sha || "checkpoint failed — retry required")}</p>
        </article>`;
    } catch (error) {
      addProblem(error.message);
    }
  });
}

async function createDissertation(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const dissertation = await api("/api/dissertations", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget).entries())),
    });
    byId("dissertation-result").innerHTML = `
      <article class="review-card">
        <p><strong>${escapeText(dissertation.document.title)}</strong></p>
        <p class="muted">Document ${escapeText(dissertation.document.id)} · Root ${escapeText(dissertation.nodes[0].id)}</p>
      </article>
    `;
    byId("dissertation-evaluation-document").value = dissertation.document.id;
    byId("dissertation-csv-document").value = dissertation.document.id;
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function previewDissertationCSV(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  const documentId = payload.document_id;
  delete payload.document_id;
  if (payload.total_target_characters) {
    payload.total_target_characters = Number(payload.total_target_characters);
  } else {
    delete payload.total_target_characters;
  }
  try {
    const preview = await api(`/api/dissertations/${encodeURIComponent(documentId)}/outline-imports/preview`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderDissertationCSVPreview(documentId, preview);
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderDissertationCSVPreview(documentId, preview) {
  const rows = preview.plan.map((item) => `
    <tr>
      <td>${escapeText(item.source_row_number)}</td>
      <td>${escapeText(item.node_type)}</td>
      <td><strong>${escapeText(item.title)}</strong><br><small>${escapeText(item.summary || "概要未指定")}</small></td>
      <td>${escapeText(Number(item.target_characters).toLocaleString())}字</td>
      <td>${escapeText(item.instructions || "章題と概要に沿って構成")}</td>
    </tr>`).join("");
  const warnings = preview.warnings.length
    ? `<ul>${preview.warnings.map((warning) => `<li>${escapeText(warning)}</li>`).join("")}</ul>`
    : "";
  byId("dissertation-csv-result").innerHTML = `
    <article class="review-card">
      <p><strong>${escapeText(preview.row_count)}件</strong> · 合計 ${escapeText(Number(preview.target_characters).toLocaleString())}字</p>
      <p class="muted">${escapeText(preview.source_path)} · ${escapeText(preview.source_encoding)} · PreviewではOutlineを変更していません。</p>
      ${warnings}
      <div class="table-scroll"><table>
        <thead><tr><th>行</th><th>種別</th><th>タイトル / 概要</th><th>目標</th><th>指示</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <button class="primary-action" id="dissertation-csv-confirm">この計画でOutlineを登録</button>
    </article>`;
  byId("dissertation-csv-confirm").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      const result = await api(`/api/dissertations/${encodeURIComponent(documentId)}/outline-imports/${encodeURIComponent(preview.id)}/confirm`, {
        method: "POST",
      });
      byId("dissertation-csv-result").innerHTML = `
        <article class="review-card">
          <p><strong>CSV Outlineを登録しました</strong></p>
          <p class="muted">${escapeText(result.dissertation.nodes.length - 1)}構成要素 · 合計 ${escapeText(Number(result.import.target_characters).toLocaleString())}字 · Import ${escapeText(result.import.id)}</p>
        </article>`;
    } catch (error) {
      addProblem(error.message);
      event.currentTarget.disabled = false;
    }
  });
}

async function evaluateDissertation(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  const documentId = payload.document_id;
  delete payload.document_id;
  try {
    const evaluation = await api(`/api/dissertations/${encodeURIComponent(documentId)}/evaluations`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    byId("dissertation-evaluation-result").innerHTML = `
      <article class="review-card">
        <p><strong>${escapeText(evaluation.status)}</strong> · ${Number(evaluation.summary.finding_count)} findings</p>
        <p class="muted">${Number(evaluation.summary.pinned_revision_count)} pinned revisions · Evaluation ${escapeText(evaluation.id)}</p>
      </article>
    `;
    await refreshProblems();
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function startEvaluation(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.max_repair_attempts = Number(payload.max_repair_attempts);
  try {
    const evaluation = await api("/api/evaluations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    byId("evaluation-result").innerHTML = `
      <article class="review-card">
        <p><strong>${escapeText(evaluation.status)}</strong> · ${Number(evaluation.summary.finding_count)} findings</p>
        <p class="muted">Evaluation ${escapeText(evaluation.id)}</p>
      </article>
    `;
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function registerProject(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.projects.unshift(project);
    renderSidebar();
    byId("editor-content").innerHTML = `
      <article class="document-view">
        <h1>${escapeText(project.name)}</h1>
        <p class="lede">Projectを登録し、Gitの初期checkpointを作成しました。</p>
        <p>${escapeText(project.root_path)}</p>
      </article>
    `;
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderWelcome() {
  byId("tab-title").textContent = "Research Workspace";
  const activeRuns = state.runs
    .filter((item) => ["queued", "running", "waiting_human"].includes((item.run || item).status))
    .slice(0, 5);
  const recentRuns = state.runs.slice(0, 8);
  const runRows = recentRuns.length
    ? recentRuns.map((item, index) => {
        const run = item.run || item;
        const progress = item.progress || {};
        const execution = runExecutionState(item);
        const percentage = runHasLiveProgress(item) ? progress.percentage : null;
        const timestamp = run.completed_at || run.started_at || run.created_at;
        const timestampLabel = timestamp ? String(timestamp).replace("T", " ").slice(0, 16) : "—";
        return `<button type="button" class="home-run home-run-open" data-run-id="${escapeText(run.id)}">
          <span class="home-run-index">${escapeText(String(index + 1).padStart(2, "0"))}</span>
          <span class="home-run-copy"><strong>${escapeText(runSourceLabel(run))}</strong><small>${escapeText(workflowDisplayName(run.workflow_name))} · ${escapeText(run.id.slice(0, 8))}</small></span>
          <span class="home-run-state" data-status="${escapeText(run.status)}">${escapeText(execution.statusLabel)}${percentage != null ? ` · ${escapeText(percentage)}%` : ""}</span>
          <time datetime="${escapeText(timestamp || "")}">${escapeText(timestampLabel)}</time>
          ${percentage != null ? `<progress class="home-progress" max="100" value="${escapeText(percentage)}">${escapeText(percentage)}%</progress>` : ""}
        </button>`;
      }).join("")
    : '<p class="empty-state home-empty">Run履歴はまだありません。</p>';
  const projectRows = state.projects.length
    ? state.projects.slice(0, 6).map((project, index) => `
        <button type="button" class="home-project" data-project-id="${escapeText(project.id)}">
          <span class="home-project-index">${escapeText(String(index + 1).padStart(2, "0"))}</span>
          <span class="home-project-copy"><strong>${escapeText(project.name)}</strong><small>${escapeText(project.root_path)}</small></span>
          <span class="home-project-open" aria-hidden="true">↗</span>
        </button>`).join("")
    : '<p class="empty-state home-empty">Projectはまだ登録されていません。</p>';
  byId("editor-content").innerHTML = `
    <article class="workspace-home">
      <header class="home-hero">
        <div class="home-heading">
          <div class="home-title-lockup">
            <span class="home-title-mark" aria-hidden="true">
              <svg viewBox="0 0 64 72">
                <path d="M9 10h15M42 10h13M27 59V41l3-5 3 4 4-6v25M27 49h10M26 59h12v7H26z"/>
                <path d="M26 31l-8-3 3-7 8 4zM32 27l-2-8 7-3 2 8zM25 17l-7-5 8-3 5 6zM36 13l4-8 6 6-4 7zM42 27l8-3 1 8-8 1zM20 36l-8 2 3 7 8-5zM35 31l5-6 4 5-5 6z"/>
              </svg>
            </span>
            <h1>ふかみる <span class="home-title-english">Fukamiru</span></h1>
          </div>
          <p class="home-expansion">Bibliography-Aware User-Guided Layered Language-Model Engine for Texts</p>
          <p class="home-meta">${escapeText(state.projects.length)} Projects · ${escapeText(state.runs.length)} recent Runs · Updated now</p>
        </div>
        <div class="home-actions">
          <button class="secondary-action home-navigate" type="button" data-activity-target="review">レビューを開く</button>
        </div>
      </header>
      <nav class="home-tabs" aria-label="Workspace sections">
        <span class="home-tab current" aria-current="page">Overview</span>
        <button class="home-tab home-panel-open" type="button">Runs</button>
        <button class="home-tab home-navigate" type="button" data-activity-target="projects">Projects</button>
        <button class="home-tab home-navigate" type="button" data-activity-target="review">Review</button>
      </nav>
      <section class="home-question" aria-labelledby="workspace-principle-title">
        <header><span id="workspace-principle-title">WORKSPACE PRINCIPLE</span><span class="home-system-state">${escapeText(state.health?.status === "ready" ? "SYSTEM READY" : "CONNECTING")}</span></header>
        <p>資料・翻訳・原稿を出典とRun履歴に結び付けて管理します。</p>
      </section>
      <div class="home-grid">
        <section class="home-card home-runs">
          <header class="home-card-header">
            <div><p class="section-kicker">RESEARCH LEDGER</p><h2>Recent Runs</h2></div>
            <span>${escapeText(activeRuns.length)} active</span>
          </header>
          <div class="home-run-table">
            <div class="home-table-head" aria-hidden="true"><span>#</span><span>Source</span><span>Status</span><span>Updated</span></div>
            ${runRows}
          </div>
        </section>
        <section class="home-card">
          <header class="home-card-header">
            <div><p class="section-kicker">LIBRARY</p><h2>Projects</h2></div>
            <button class="home-project-register" type="button">登録</button>
          </header>
          <div class="home-project-list">${projectRows}</div>
        </section>
      </div>
    </article>
  `;
  document.querySelectorAll(".home-navigate").forEach((button) => {
    button.addEventListener("click", () => activateActivity(button.dataset.activityTarget));
  });
  document.querySelectorAll(".home-run-open").forEach((button) => {
    button.addEventListener("click", () => selectTrackedRun(button.dataset.runId));
  });
  document.querySelectorAll(".home-project").forEach((button) => {
    button.addEventListener("click", () => {
      const project = state.projects.find((item) => item.id === button.dataset.projectId);
      if (project) renderProjectDetails(project);
    });
  });
  document.querySelectorAll(".home-panel-open").forEach((button) => {
    button.addEventListener("click", () => selectPanel("run"));
  });
  document.querySelector(".home-project-register")?.addEventListener("click", () => renderProjectEditor());
}

async function renderLiteratureEditor() {
  byId("tab-title").textContent = "Literature Discovery";
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <h1>Literature Discovery</h1>
      <p class="lede">Providerから候補だけを取得し、承認後にBibliographicRecordへ登録します。OA PDFは承認済み候補だけをstaging経由で取得します。</p>
      <form id="literature-search-form" class="form-grid">
        <div class="field">
          <label for="literature-provider">Provider</label>
          <select id="literature-provider" name="provider">
            <option value="openalex">OpenAlex</option>
            <option value="crossref">Crossref</option>
            <option value="semantic_scholar">Semantic Scholar</option>
          </select>
        </div>
        <div class="field">
          <label for="literature-limit">Limit</label>
          <input id="literature-limit" name="limit" type="number" min="1" max="100" value="20">
        </div>
        <div class="field full">
          <label for="literature-query">Bibliographic query</label>
          <input id="literature-query" name="query" required>
        </div>
        <button class="primary-action" type="submit">候補を取得</button>
      </form>
      <section><h2>Candidate Review</h2><div id="literature-candidates"><p class="muted">Loading…</p></div></section>
    </article>`;
  byId("literature-search-form").addEventListener("submit", searchLiterature);
  await refreshLiteratureCandidates();
}

async function searchLiterature(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.limit = Number(payload.limit);
  button.disabled = true;
  try {
    await api("/api/literature/searches", { method: "POST", body: JSON.stringify(payload) });
    await refreshLiteratureCandidates();
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

async function refreshLiteratureCandidates() {
  const target = byId("literature-candidates");
  if (!target) return;
  try {
    const [candidates, approved] = await Promise.all([
      api("/api/literature/candidates?status=pending"),
      api("/api/literature/candidates?status=approved"),
    ]);
    const pendingCards = candidates.map((candidate) => `
      <article class="review-card">
        <h3>${escapeText(candidate.title)}</h3>
        <p>${candidate.authors.map(escapeText).join(", ")}</p>
        <p class="muted">${escapeText(candidate.provider)} · ${escapeText(candidate.publication_date || "date unknown")} · DOI ${escapeText(candidate.doi || "none")}</p>
        <p class="muted">OA ${escapeText(candidate.oa_status || "unknown")} · License ${escapeText(candidate.license_url || "not supplied")}</p>
        ${candidate.url ? `<a class="artifact-link" href="${escapeText(candidate.url)}" target="_blank" rel="noopener">Provider record</a>` : ""}
        <div class="review-actions">
          <button class="primary-action literature-review" data-candidate="${escapeText(candidate.id)}" data-decision="approve">登録</button>
          <button class="danger-action literature-review" data-candidate="${escapeText(candidate.id)}" data-decision="reject">却下</button>
        </div>
      </article>`).join("");
    const approvedCards = approved.map((candidate) => `
      <article class="review-card">
        <h3>${escapeText(candidate.title)}</h3>
        <p class="muted">登録済み · ${escapeText(candidate.provider)} · License ${escapeText(candidate.license_url || "not supplied")}</p>
        ${candidate.oa_url ? `<button class="secondary-action literature-acquire" data-candidate="${escapeText(candidate.id)}">OA PDFを取得</button>` : '<p class="muted">OA PDFの取得先URLがありません。</p>'}
      </article>`).join("");
    target.innerHTML = `
      <h3>未レビュー</h3>${pendingCards || '<p class="muted">未レビュー候補はありません。</p>'}
      <h3>承認済み / OA取得待ち</h3>${approvedCards || '<p class="muted">取得待ち候補はありません。</p>'}
    `;
    document.querySelectorAll(".literature-review").forEach((button) => {
      button.addEventListener("click", reviewLiteratureCandidate);
    });
    document.querySelectorAll(".literature-acquire").forEach((button) => {
      button.addEventListener("click", acquireLiteratureCandidate);
    });
  } catch (error) {
    addProblem(error.message);
  }
}

async function acquireLiteratureCandidate(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api(`/api/literature/candidates/${encodeURIComponent(button.dataset.candidate)}/acquire`, {
      method: "POST",
    });
    await refreshLiteratureCandidates();
  } catch (error) {
    addProblem(error.message);
    button.disabled = false;
  }
}

async function reviewLiteratureCandidate(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api(`/api/literature/candidates/${encodeURIComponent(button.dataset.candidate)}/review`, {
      method: "POST",
      body: JSON.stringify({ decision: button.dataset.decision }),
    });
    await refreshLiteratureCandidates();
  } catch (error) {
    addProblem(error.message);
    button.disabled = false;
  }
}

function renderTranslationEditor() {
  byId("tab-title").textContent = "New Translation";
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <h1>翻訳ワークフロー</h1>
      <p class="lede">未登録PATHはIngestionへ接続し、登録済み資料は既存のDocumentUnitを参照します。</p>
      <form id="translation-form" class="form-grid">
        <div class="field full">
          <label for="source-path">Source path</label>
          <input id="source-path" name="source_path" required placeholder="C:\\path\\to\\sources\\source.pdf">
        </div>
        <div class="field">
          <label for="document-unit">DocumentUnit / range</label>
          <input id="document-unit" name="document_unit" placeholder="Auto / pages 12–20">
        </div>
        <div class="field">
          <label for="source-language">Source language</label>
          <select id="source-language" name="source_language">
            <option value="auto">Auto detect</option>
            <option value="fr">French</option>
            <option value="en">English</option>
            <option value="ja">Japanese</option>
          </select>
        </div>
        <div class="field">
          <label for="target-language">Target language</label>
          <select id="target-language" name="target_language">
            <option value="ja">Japanese</option>
            <option value="fr">French</option>
            <option value="en">English</option>
          </select>
        </div>
        <div class="field full">
          <label for="translation-instructions">Translation instructions</label>
          <textarea id="translation-instructions" name="instructions" placeholder="訳語、文体、注釈、候補保持などの指示"></textarea>
        </div>
        <button class="primary-action" type="submit">翻訳Runを開始</button>
      </form>
      <section class="translation-memory-search">
        <h2>Translation Memory</h2>
        <p class="muted">人間が修正・承認した既存訳から、原文が一致または類似する候補を検索します。</p>
        <label for="translation-memory-query">Source text</label>
        <textarea id="translation-memory-query" placeholder="再利用候補を探す原文"></textarea>
        <button id="translation-memory-search" class="secondary-action" type="button">候補を検索</button>
        <div id="translation-memory-results"></div>
      </section>
      <section class="translation-alignment-inspector">
        <h2>SourceSpan Alignment Inspector</h2>
        <p class="muted">Translation Versionから文単位の原文・訳文・SourceSpan・原資料頁を表示します。</p>
        <label for="translation-alignment-version">Translation Version ID</label>
        <input id="translation-alignment-version" placeholder="UUID">
        <button id="translation-alignment-open" class="secondary-action" type="button">対応を開く</button>
        <div id="translation-alignment-results"></div>
      </section>
    </article>
  `;
  byId("translation-form").addEventListener("submit", startTranslation);
  byId("translation-memory-search").addEventListener("click", searchTranslationMemory);
  byId("translation-alignment-open").addEventListener("click", inspectTranslationAlignment);
}

async function inspectTranslationAlignment() {
  const versionId = byId("translation-alignment-version").value.trim();
  if (!versionId) return;
  const target = byId("translation-alignment-results");
  target.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const result = await api(`/api/translation-versions/${encodeURIComponent(versionId)}/alignments`);
    target.innerHTML = result.segments.length ? result.segments.map((segment) => `
      <article class="review-card alignment-segment">
        <p><strong>#${Number(segment.segment_ordinal) + 1}</strong> · ${escapeText(segment.alignment_status)}</p>
        <p lang="${escapeText(result.source_language)}">${escapeText(segment.source_text)}</p>
        <p lang="${escapeText(result.target_language)}">${escapeText(segment.target_text)}</p>
        <p class="muted">${segment.source_page_start ? `p. ${escapeText(segment.source_page_start)}–${escapeText(segment.source_page_end)}` : "page unknown"} · ${segment.source_span_ids.map(escapeText).join(", ")}</p>
        ${segment.source_spans.map((span) => span.source_path && span.start_page
          ? `<a class="artifact-link" href="/api/sources/${encodeURIComponent(span.source_asset_id)}/pages/${encodeURIComponent(span.start_page)}" target="_blank" rel="noopener">原資料 p. ${escapeText(span.start_page)}</a>`
          : "").join("")}
      </article>`).join("") : '<p class="muted">Segment alignmentはありません。</p>';
  } catch (error) {
    target.innerHTML = "";
    addProblem(error.message);
  }
}

async function searchTranslationMemory() {
  const query = byId("translation-memory-query").value.trim();
  const results = byId("translation-memory-results");
  if (!query) {
    results.innerHTML = '<p class="muted">原文を入力してください。</p>';
    return;
  }
  try {
    const sourceLanguage = byId("source-language").value;
    const targetLanguage = byId("target-language").value;
    const payload = await api(`/api/translation-memory?q=${encodeURIComponent(query)}&source_language=${encodeURIComponent(sourceLanguage)}&target_language=${encodeURIComponent(targetLanguage)}`);
    results.innerHTML = payload.results.length
      ? payload.results.map((item) => `
          <article class="review-card">
            <p><strong>${escapeText(item.match_type)}</strong> · ${(Number(item.similarity) * 100).toFixed(1)}% · ${escapeText(item.quality_status)}</p>
            <p>${escapeText(item.source_text)}</p>
            <p>${escapeText(item.target_text)}</p>
          </article>
        `).join("")
      : '<p class="muted">候補はありません。</p>';
  } catch (error) {
    addProblem(error.message);
  }
}

function renderAuthoringEditor() {
  byId("tab-title").textContent = "Manuscript Generator";
  const projectOptions = state.projects.map((project) =>
    `<option value="${escapeText(project.id)}">${escapeText(project.name)}</option>`).join("");
  byId("editor-content").innerHTML = `
    <article class="document-view">
      <h1>ノートから論証構造を組み立てる</h1>
      <p class="lede">ノートを命題へ分解し、問い・主張階層・論証グラフ・談話構造・節契約を作ってから本文を生成します。元ノートの節分けや順序は引き継ぎません。</p>
      <form id="authoring-form" class="form-grid">
        <div class="field full"><label for="authoring-project">Project</label>
          <select id="authoring-project" name="project_id" required>${projectOptions || "<option value=''>Projectを先に登録してください</option>"}</select></div>
        <div class="field full"><label for="authoring-title">ノートの題名</label>
          <input id="authoring-title" name="title" required placeholder="研究メモの題名"></div>
        <div class="field full"><label for="authoring-folder">対象のフォルダを選択</label>
          <select id="authoring-folder" name="source_folder" disabled><option value="">目次を含むフォルダを検索中…</option></select>
          <small>Project内の同じフォルダに、目次ファイルと各項目名に対応する本文ファイルを用意してください。</small></div>
        <div id="authoring-folder-preview" class="field full" aria-live="polite"></div>
        <div id="authoring-note-field" class="field full"><label for="authoring-note">ノート本文（フォルダを選択しない場合）</label>
          <textarea id="authoring-note" name="note_text" required placeholder="箇条書きや断片のままで構いません"></textarea></div>
        <button class="primary-action" type="submit" ${projectOptions ? "" : "disabled"}>原稿生成を開始</button>
      </form>
      <div id="authoring-summary" aria-live="polite"></div>
    </article>`;
  byId("authoring-form").addEventListener("submit", startAuthoring);
  byId("authoring-project").addEventListener("change", loadAuthoringFolders);
  byId("authoring-folder").addEventListener("change", previewAuthoringFolder);
  loadAuthoringFolders();
  renderAuthoringSummary();
}

async function loadAuthoringFolders() {
  const projectId = byId("authoring-project")?.value;
  const selector = byId("authoring-folder");
  if (!selector) return;
  state.authoringFolderPreview = null;
  selector.disabled = true;
  selector.innerHTML = '<option value="">目次を含むフォルダを検索中…</option>';
  byId("authoring-folder-preview").innerHTML = "";
  byId("authoring-note").disabled = false;
  byId("authoring-note").required = true;
  byId("authoring-note-field").hidden = false;
  if (!projectId) return;
  try {
    const result = await api(`/api/projects/${encodeURIComponent(projectId)}/authoring-folders`);
    if (byId("authoring-project")?.value !== projectId) return;
    selector.innerHTML = '<option value="">ノート本文を直接入力</option>';
    result.folders.forEach((folder) => {
      const option = document.createElement("option");
      option.value = folder;
      option.textContent = folder === "." ? "Projectの直下" : folder;
      selector.append(option);
    });
    selector.disabled = !result.folders.length;
    if (!result.folders.length) {
      byId("authoring-folder-preview").textContent = "目次.md などを含むフォルダがありません。ノート本文を下の欄に直接入力してください。";
    }
  } catch (error) {
    selector.innerHTML = '<option value="">フォルダを取得できません</option>';
    addProblem(error.message);
  }
}

async function previewAuthoringFolder() {
  const projectId = byId("authoring-project").value;
  const folder = byId("authoring-folder").value;
  const target = byId("authoring-folder-preview");
  const note = byId("authoring-note");
  state.authoringFolderPreview = null;
  note.disabled = Boolean(folder);
  note.required = !folder;
  byId("authoring-note-field").hidden = Boolean(folder);
  target.innerHTML = folder ? '<p class="muted">目次と本文を確認中…</p>' : "";
  if (!folder) return;
  try {
    const preview = await api(`/api/projects/${encodeURIComponent(projectId)}/authoring-input-preview`, {
      method: "POST",
      body: JSON.stringify({ folder_path: folder }),
    });
    if (byId("authoring-project")?.value !== projectId || byId("authoring-folder")?.value !== folder) return;
    state.authoringFolderPreview = preview;
    const items = preview.sections.map((section) =>
      `<li>${escapeText(section.title)} <small>— ${escapeText(section.source_path)}</small></li>`).join("");
    const warnings = preview.warnings.length
      ? `<p class="status-warning">${preview.warnings.map(escapeText).join("<br>")}</p>` : "";
    target.innerHTML = `<div class="review-card authoring-folder-result">
      <p><strong>目次と本文の対応を確認しました</strong> · ${preview.sections.length}件</p>
      <p class="muted">目次: ${escapeText(preview.toc_path)}</p>
      <ol>${items}</ol>${warnings}
      <details><summary>生成するMarkdownを確認</summary><textarea readonly aria-label="生成するMarkdown">${escapeText(preview.note_text)}</textarea></details>
    </div>`;
  } catch (error) {
    target.innerHTML = `<p class="status-warning">${escapeText(error.message)}</p>`;
  }
}

async function startAuthoring(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const inputs = Object.fromEntries(new FormData(form).entries());
    const projectId = inputs.project_id;
    delete inputs.project_id;
    if (inputs.source_folder) {
      if (!state.authoringFolderPreview || state.authoringFolderPreview.folder_path !== inputs.source_folder) {
        throw new Error("目次と本文の対応を確認してから開始してください");
      }
      delete inputs.note_text;
    } else {
      delete inputs.source_folder;
    }
    const created = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ workflow_name: "manuscript_generator", project_id: projectId, inputs }),
    });
    state.run = created;
    state.events = [];
    renderRunStatus();
    connectToRun(created.run_id);
    selectPanel("run");
    renderAuthoringSummary();
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

function flattenObjects(value, output = []) {
  if (Array.isArray(value)) value.forEach((item) => flattenObjects(item, output));
  else if (value && typeof value === "object") {
    output.push(value);
    Object.values(value).forEach((item) => {
      if (item && typeof item === "object") flattenObjects(item, output);
    });
  }
  return output;
}

function renderAuthoringSummary() {
  const target = byId("authoring-summary");
  if (!target) return;
  const snapshot = state.run || {};
  const records = flattenObjects(snapshot);
  const first = (...keys) => {
    for (const record of records) for (const key of keys) {
      if (record[key] !== undefined && record[key] !== null && record[key] !== "") return record[key];
    }
    return null;
  };
  const question = first("research_question", "researchQuestion");
  const researchProblem = first("research_problem");
  const bodyOutputs = first("body_section_outputs");
  const defenseObligations = first("defense_obligations");
  const conclusionSynthesis = first("conclusion_synthesis");
  const claims = first("claim_tree", "claimTree", "claims");
  const discourse = first("discourse_tree", "discourseTree", "discourse");
  const required = first("required_user_information", "requiredUserInformation", "required_inputs");
  const reviews = Array.isArray(snapshot.reviews) ? snapshot.reviews : [];
  const defects = Array.isArray(snapshot.defects) ? snapshot.defects : [];
  const artifacts = Array.isArray(snapshot.artifacts) ? snapshot.artifacts : [];
  const sections = [];
  const add = (title, value) => {
    if (value === null) return;
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    sections.push(`<section class="authoring-result"><h2>${escapeText(title)}</h2><pre>${escapeText(text)}</pre></section>`);
  };
  add("Research question", question);
  add("入力Markdown", first("generated_note_path"));
  add("Research problem", researchProblem);
  add("Body section outputs", bodyOutputs);
  add("Defense obligations", defenseObligations);
  add("Conclusion synthesis", conclusionSynthesis);
  add("Claim tree", claims);
  add("Discourse structure", discourse);
  add("必要なユーザー情報", required);
  add("レビュー", reviews);
  add("構造監査の指摘", defects);
  if (artifacts.length) sections.push(`<section class="authoring-result"><h2>生成物</h2>${artifacts.map((artifact) => `<a class="artifact-link" href="/api/artifacts/${encodeURIComponent(artifact.id)}" target="_blank" rel="noopener">${escapeText(artifact.title || artifact.name || artifact.id)}</a>`).join("")}</section>`);
  target.innerHTML = sections.length ? `<h2>Runの構造と出力</h2>${sections.join("")}` : "";
}

async function startTranslation(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  const form = new FormData(event.currentTarget);
  const inputs = Object.fromEntries(form.entries());
  try {
    const created = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ workflow_name: "translation", inputs }),
    });
    state.run = created;
    state.events = [];
    renderRunStatus();
    connectToRun(created.run_id);
    selectPanel("run");
  } catch (error) {
    addProblem(error.message);
  } finally {
    button.disabled = false;
  }
}

function addProblem(message) {
  state.problems.push({ message, createdAt: new Date() });
  byId("problem-count").textContent = state.problems.length;
  selectPanel("problems");
}

function rememberSelectedTabs() {
  try {
    window.history.replaceState(
      window.history.state,
      "",
      urlForSelectedTabs(window.location.href, state),
    );
  } catch {
    // Navigation still works if the browser does not allow URL updates.
  }
}

function selectPanel(panel) {
  setPaneVisibility("bottom", true);
  state.panel = panel;
  document.querySelectorAll(".panel-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === panel);
  });
  rememberSelectedTabs();
  renderPanel();
}

function progressMarkupForRun(item, selected) {
  const progress = runMeasuredProgress(item,
    selected ? state.runProgress || item.progress : item.progress,
    selected ? state.events : item.events || []);
  const activeNodes = runDisplayNodes(item);
  const terminal = terminalRunProgressState(item, progress);
  if (terminal) {
    return `<div class="run-terminal-state" data-status="${escapeText(terminal.status)}" role="${terminal.status === "failed" ? "alert" : "status"}">
      <div class="run-terminal-heading"><span aria-hidden="true">!</span><strong>${escapeText(terminal.title)}</strong></div>
      ${terminal.stoppedAt ? `<p>${escapeText(terminal.stoppedAt)}</p>` : ""}
      ${terminal.reason ? `<small>${escapeText(terminal.reason)}</small>` : ""}
    </div>`;
  }
  const ingestion = ingestionActivity(progress, activeNodes, selected ? state.events : []);
  const progressLabel = progressUnitLabel(progress, activeNodes);
  const execution = runExecutionState(item);
  const updatedAt = progress?.updated_at
    || activeNodes.find((node) => node.lease_heartbeat_at)?.lease_heartbeat_at
    || (item.run || item).started_at
    || (item.run || item).created_at;
  const updateLabel = updatedAt ? `最終更新 ${String(updatedAt).replace("T", " ").slice(0, 19)} UTC · ` : "";
  if (ingestion) {
    const details = ingestionActivityDetails(ingestion);
    return `<div class="run-progress ingestion-progress">
      <div><strong>${escapeText(ingestion.label)}</strong></div>
      ${ingestion.indeterminate || ingestion.percentage == null
        ? "<progress></progress>"
        : `<progress max="100" value="${escapeText(ingestion.percentage)}">${escapeText(ingestion.percentage)}%</progress>`}
      <small>${details ? `${escapeText(details)} · ` : ""}${escapeText(updateLabel)}10秒ごとに自動更新</small>
    </div>`;
  }
  if (progress?.kind === "manuscript_argument_processes") {
    const statusLabels = {
      pending: "待機",
      running: "処理中",
      completed: "完了",
      reused: "保存結果を再利用",
      invalid: "未解決あり",
      rolled_back: "悪化候補を棄却",
    };
    const processes = Array.isArray(progress.argument_processes)
      ? progress.argument_processes
      : [];
    const rows = processes.map((process) => {
      const status = String(process.status || "pending");
      const grace = process.grace_state === "waiting"
        ? '<span class="argument-grace" role="status">終了猶予中</span>'
        : process.grace_state === "accepted"
          ? '<span class="argument-grace accepted">遅延結果を検証して採用</span>'
          : "";
      return `<li class="argument-process" data-status="${escapeText(status)}">
        <span class="argument-process-marker" aria-hidden="true"></span>
        <span class="argument-process-name">${escapeText(argumentProcessLabel(process.process_name))}</span>
        <span class="argument-process-status">${escapeText(statusLabels[status] || status)}${grace}</span>
        <small>対象 ${escapeText(process.target_count ?? 0)} · 完了 ${escapeText(process.completed_count ?? 0)} · 再利用 ${escapeText(process.reused_count ?? 0)} · 不正リンク棄却 ${escapeText(process.vetoed_link_count ?? 0)} · 未解決 ${escapeText(process.unresolved_count ?? 0)}</small>
      </li>`;
    }).join("");
    return `<div class="run-progress argument-progress" aria-live="polite">
      <div><strong>${escapeText(progress.percentage ?? 0)}%</strong> · ${escapeText(progressLabel)}</div>
      <progress max="100" value="${escapeText(progress.percentage ?? 0)}">${escapeText(progress.percentage ?? 0)}%</progress>
      <ol class="argument-process-list">${rows}</ol>
      <small>対象 ${escapeText(progress.target_count ?? 0)} · 完了 ${escapeText(progress.completed_count ?? 0)} · 再利用 ${escapeText(progress.reused_count ?? 0)} · 未解決 ${escapeText(progress.unresolved_count ?? 0)} · ${escapeText(updateLabel)}10秒ごとに自動更新</small>
    </div>`;
  }
  const numeric = numericProgressSummary(progress, progress?.kind ? progressLabel : "処理");
  if (numeric) {
    return `<div class="run-progress numeric-progress" role="status">
      <div><strong>${escapeText(numeric.headline)}</strong>${numeric.percentage !== null ? ` · ${escapeText(numeric.percentage)}%` : ""}</div>
      <progress max="100" aria-label="${escapeText(numeric.title)}" ${numeric.percentage !== null ? `value="${escapeText(numeric.percentage)}"` : ""}></progress>
      <small>${numeric.details ? `${escapeText(numeric.details)} · ` : ""}${escapeText(updateLabel)}10秒ごとに自動更新</small>
    </div>`;
  }
  return `<p class="muted">${escapeText(execution.detail)} · ${escapeText(updateLabel)}10秒ごとに自動更新</p>`;
}

function renderPanel() {
  const content = byId("panel-content");
  if (state.panel === "problems") {
    content.innerHTML = state.problems.length
      ? state.problems.map((problem) => `<p class="error-text">${escapeText(problem.message)}</p>`).join("")
      : '<p class="empty-state">No problems detected.</p>';
    return;
  }
  if (state.panel === "run") {
    const trackedRuns = [...state.runs];
    if (state.run) {
      const selectedRun = state.run.run || state.run;
      const index = trackedRuns.findIndex((item) => (item.run || item).id === selectedRun.id);
      if (index >= 0) trackedRuns[index] = state.run;
      else trackedRuns.unshift(state.run);
    }
    if (!trackedRuns.length) {
      content.innerHTML = '<p class="empty-state">No runs.</p>';
      return;
    }
    content.innerHTML = `<div class="run-list">${trackedRuns.map((item) => {
      const run = item.run || item;
      const selected = run.id === state.selectedRunId;
      const displayNodes = runDisplayNodes(item);
      const artifacts = item.artifacts || [];
      const execution = runExecutionState(item);
      const patch = patchProcessingState(item.patch_processing);
      const externalWait = externalRunWaitState(item);
      const error = run.error && !externalWait
        ? `<p class="error-text">${escapeText(run.error.message || JSON.stringify(run.error))}</p>`
        : "";
      return `<article class="run-summary-card${selected ? " selected" : ""}">
        <div class="run-summary-heading">
          <div><strong>${escapeText(runSourceLabel(run))}</strong> · ${escapeText(workflowDisplayName(run.workflow_name))} · <span class="run-status-${escapeText(run.status)}">${escapeText(execution.statusLabel)}</span></div>
          <button type="button" class="artifact-link run-select" data-run-id="${escapeText(run.id)}">${selected ? "選択中" : "詳細・Events"}</button>
        </div>
        <p class="muted">Run ${escapeText(run.id)}</p>
        ${externalWait ? externalWaitMarkup(run, externalWait) : ""}
        ${progressMarkupForRun(item, selected)}
        ${displayNodes.length ? `<div class="run-execution-nodes" aria-label="現在の実行ノード">
          ${displayNodes.map((node) => `<div class="run-node" data-status="${escapeText(node.status || "unknown")}">
            <span class="run-node-marker" aria-hidden="true"></span>
            <code>${escapeText(runNodeTechnicalName(node))}</code>
            <strong>${escapeText(runNodeLabel(node))}</strong>
            <small>${escapeText(runNodeStatusLabel(node))}</small>
          </div>`).join("")}
        </div>` : ""}
        <div class="run-patch-state" data-status="${escapeText(patch.status)}">
          <span class="run-patch-label">パッチ処理 · ${escapeText(patch.label)}</span>
          <small>${escapeText(patch.detail)}</small>
        </div>
        ${artifacts.map((artifact) => artifact.media_type === "text/html"
          ? `<button type="button" class="artifact-link draft-open" data-artifact-id="${escapeText(artifact.id)}" data-draft-title="${escapeText(artifact.title)}">${escapeText(artifact.title)} · HTML</button>`
          : `<a class="artifact-link" href="/api/artifacts/${encodeURIComponent(artifact.id)}" target="_blank" rel="noopener">${escapeText(artifact.title)}</a>`).join("")}
        ${error}
      </article>`;
    }).join("")}</div>`;
    content.querySelectorAll(".run-select").forEach((button) => {
      button.addEventListener("click", () => selectTrackedRun(button.dataset.runId));
    });
    content.querySelectorAll(".draft-open").forEach((button) => {
      button.addEventListener("click", openDraftPreview);
    });
    content.querySelectorAll(".run-external-resume").forEach((button) => {
      button.addEventListener("click", resumeExternalRun);
    });
    content.querySelectorAll(".run-external-cancel").forEach((button) => {
      button.addEventListener("click", cancelExternalWaitRun);
    });
    renderAuthoringSummary();
    return;
  }
  content.innerHTML = state.events.length
    ? `<p class="muted">Selected Run ${escapeText(state.selectedRunId || "")}</p>${state.events.map((event) => `
      <div class="event-row">
        <time>${escapeText((event.created_at || "").slice(11, 19))}</time>
        <span class="event-type">${escapeText(event.type)}</span>
        <span>${escapeText(JSON.stringify(event.payload || {}))}</span>
      </div>`).join("")}`
    : '<p class="empty-state">No events received.</p>';
}

function setExternalWaitActionState(button, message) {
  const card = button.closest(".run-external-wait");
  card?.querySelectorAll("button").forEach((action) => {
    action.disabled = true;
  });
  const status = card?.querySelector(".run-external-action-status");
  if (status) status.textContent = message;
  return { card, status };
}

function restoreExternalWaitActions(card, status, message) {
  card?.querySelectorAll("button").forEach((action) => {
    action.disabled = action.classList.contains("run-external-resume")
      && card.dataset.waitKind === "engine_authorization"
      && !action.dataset.engineFingerprint;
  });
  if (status) status.textContent = message;
}

async function refreshAfterRunAction(runId) {
  await refreshRunComponent(runId);
  await refreshRunList();
  const current = state.run?.run || state.run;
  if (["queued", "running", "waiting_human"].includes(current?.status)) {
    connectToRun(runId);
  } else {
    state.eventSource?.close();
    state.eventSource = null;
    startRunPolling(null);
  }
}

async function resumeExternalRun(event) {
  const button = event.currentTarget;
  const runId = String(button.dataset.runId || "");
  const item = (state.run?.run || state.run)?.id === runId
    ? state.run
    : state.runs.find((candidate) => (candidate.run || candidate).id === runId);
  const waitState = externalRunWaitState(item);
  const request = externalResumeRequestOptions(waitState);
  if (!request) {
    addProblem("承認対象の推論エンジンを確認できないため、Runを再開できません。");
    return;
  }
  const { card, status } = setExternalWaitActionState(button, "再開を要求しています…");
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/resume-external`, request);
    if (status) status.textContent = "再開要求を受け付けました。状態を更新します。";
    await refreshAfterRunAction(runId);
  } catch (error) {
    restoreExternalWaitActions(card, status, `再開できませんでした：${error.message}`);
    addProblem(error.message);
  }
}

async function cancelExternalWaitRun(event) {
  const button = event.currentTarget;
  const runId = String(button.dataset.runId || "");
  if (!window.confirm(`Run ${runId} を中止しますか？このRunは再開できなくなります。`)) return;
  const { card, status } = setExternalWaitActionState(button, "Runを中止しています…");
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
    if (status) status.textContent = "Runを中止しました。状態を更新します。";
    await refreshAfterRunAction(runId);
  } catch (error) {
    restoreExternalWaitActions(card, status, `中止できませんでした：${error.message}`);
    addProblem(error.message);
  }
}

async function refreshSnapshot(runId) {
  const snapshot = await api(`/api/runs/${runId}/snapshot`);
  state.run = snapshot;
  state.selectedRunId = runId;
  sessionStorage.setItem(`fukamiru:last-event:${runId}`, String(snapshot.latest_event_id || 0));
  renderRunStatus();
  renderPanel();
  renderAuthoringSummary();
  return snapshot.latest_event_id || 0;
}

async function refreshRunComponent(runId) {
  if (state.runPollInFlight) return;
  state.runPollInFlight = true;
  try {
    let progress;
    try {
      progress = await api(`/api/runs/${encodeURIComponent(runId)}/progress?events_limit=200`);
    } catch (error) {
      if (error.status !== 404) throw error;
      state.runProgress = null;
      await refreshSnapshot(runId);
      return;
    }
    state.run = progress;
    state.selectedRunId = runId;
    state.runProgress = progress.progress;
    state.events = progress.events || [];
    const latest = progress.latest_event_id || 0;
    sessionStorage.setItem(`fukamiru:last-event:${runId}`, String(latest));
    renderRunStatus();
    if (["run", "events"].includes(state.panel)) renderPanel();
    const status = progress.run?.status;
    if (!["queued", "running", "waiting_human"].includes(status)) {
      state.eventSource?.close();
      state.eventSource = null;
    }
  } finally {
    state.runPollInFlight = false;
  }
}

async function refreshRunList() {
  if (state.runListPollInFlight || state.health?.features?.multi_run_progress === false) return;
  state.runListPollInFlight = true;
  try {
    let payload;
    try {
      payload = await api("/api/runs?limit=25");
    } catch (error) {
      if ([404, 405].includes(error.status)) {
        state.runs = state.run ? [state.run] : [];
        return;
      }
      throw error;
    }
    state.runs = payload.runs || [];
    try {
      const translationFiles = await api("/api/translation/source-files");
      state.translationRuns = translationFiles.runs || [];
    } catch (error) {
      if (![404, 405].includes(error.status)) throw error;
      state.translationRuns = state.runs;
    }
    renderRunStatus();
    if (state.activity === "translation") renderSidebar();
    if (state.panel === "run") renderPanel();
    if (state.activity === "projects" && byId("editor-content").querySelector(".workspace-home")) {
      renderWelcome();
    }
  } finally {
    state.runListPollInFlight = false;
  }
}

async function selectTrackedRun(runId) {
  const changed = state.selectedRunId !== runId;
  if (changed) {
    state.events = [];
    state.runProgress = null;
  }
  state.selectedRunId = runId;
  localStorage.setItem("fukamiru:current-run", runId);
  try {
    const progress = await refreshRunComponent(runId);
    const run = state.run?.run || state.run;
    if (["queued", "running", "waiting_human"].includes(run?.status)) {
      connectToRun(runId);
    } else {
      state.eventSource?.close();
      state.eventSource = null;
      startRunPolling(null);
    }
    selectPanel("run");
    return progress;
  } catch (error) {
    addProblem(error.message);
    return null;
  }
}

function stopRunPolling() {
  if (state.runPollTimer) window.clearInterval(state.runPollTimer);
  state.runPollTimer = null;
}

function startRunPolling(runId) {
  stopRunPolling();
  refreshRunList().catch(() => {
    byId("connection-status").textContent = "RECONNECTING";
  });
  if (runId) {
    refreshRunComponent(runId).catch(() => {
      byId("connection-status").textContent = "RECONNECTING";
    });
  }
  state.runPollTimer = window.setInterval(() => {
    if (state.corpus?.batch?.status === "indexing") refreshCorpus().catch(() => {});
    refreshRunList().catch(() => {
      byId("connection-status").textContent = "RECONNECTING";
    });
    if (runId) {
      refreshRunComponent(runId).catch(() => {
        byId("connection-status").textContent = "RECONNECTING";
      });
    }
  }, 10000);
}

function connectToRun(runId) {
  if (state.selectedRunId !== runId) {
    state.events = [];
    state.runProgress = null;
  }
  state.selectedRunId = runId;
  localStorage.setItem("fukamiru:current-run", runId);
  startRunPolling(runId);
  state.eventSource?.close();
  const key = `fukamiru:last-event:${runId}`;
  const after = state.events.length ? Number(sessionStorage.getItem(key) || 0) : 0;
  const source = new EventSource(`/api/runs/${runId}/events?after=${after}`);
  state.eventSource = source;

  const receive = (message) => {
    const event = JSON.parse(message.data);
    if (!state.events.some((item) => item.event_id === event.event_id)) {
      state.events.push(event);
      state.events = state.events.slice(-200);
    }
    if (event.event_id) sessionStorage.setItem(key, String(event.event_id));
    renderPanel();
    if (event.type.startsWith("corpus.")) {
      if (event.type === "corpus.index.progress") state.corpusIndexProgress = event.payload;
      refreshCorpus().catch((error) => addProblem(error.message));
    }
    if (event.type === "run.completed") {
      refreshRunComponent(runId).catch(() => {});
      api("/api/health").then((health) => {
        state.health = health;
        renderSystemStatus();
        renderSidebar();
      }).catch((error) => addProblem(error.message));
    }
  };
  ["run.created", "node.queued", "node.started", "node.completed", "node.failed", "node.waiting_human", "review.requested", "review.resolved", "metric.updated", "repair.updated", "ingestion.progress", "manuscript.reasoning.progress", "manuscript.structure_plan.progress", "manuscript.argument_plan.progress", "manuscript.argument_plan.precommit", "manuscript.evidence.progress", "manuscript.references.progress", "manuscript.references.quality_checked", "manuscript.paragraph_plan.progress", "manuscript.rewrite.parallelism", "manuscript.rewrite.progress", "manuscript.predicate_resolution.progress", "manuscript.coherence.progress", "manuscript.coherence.conditional_success", "manuscript.polish.progress", "translation.batch.progress", "translation.post_edit.progress", "translation.research.triggers.detected", "translation.research.evidence.resolved", "translation.compliance.checked", "corpus.item.updated", "corpus.index.progress", "corpus.index.completed", "ocr.job.created", "ocr.job.language_assigned", "ocr.job.staged", "ocr.job.reconciled", "ocr.run.resumed", "run.completed", "run.failed"]
    .forEach((eventName) => source.addEventListener(eventName, receive));
  source.addEventListener("sync.required", async (message) => {
    const event = JSON.parse(message.data);
    await refreshRunComponent(runId);
    source.close();
    connectToRun(runId);
    state.events.push(event);
  });
  source.onerror = () => {
    byId("connection-status").textContent = "RECONNECTING";
  };
  source.onopen = () => {
    byId("connection-status").textContent = "CONNECTED";
  };
}

function activateActivity(activity, { remember = true } = {}) {
  if (state.selectedSearchReportId && activity !== "rag") closeSearchReport();
  state.activity = activity;
  if (activity !== "review") document.querySelector(".workbench").classList.remove("mobile-review-open");
  document.querySelectorAll(".activity-button[data-activity]").forEach((item) => {
    item.classList.toggle("active", item.dataset.activity === activity);
  });
  if (remember) rememberSelectedTabs();
  renderSidebar();
  if (activity === "translation") renderTranslationEditor();
  else if (activity === "authoring") renderAuthoringEditor();
  else if (activity === "rag") renderRagEditor();
  else if (activity === "review") renderReviewEditor();
  else if (activity === "literature") renderLiteratureEditor();
  else renderWelcome();
}

function openDraftPreview(event) {
  const artifactId = event.currentTarget.dataset.artifactId;
  const title = event.currentTarget.dataset.draftTitle || "Translation draft";
  const previewUrl = `/api/artifacts/${encodeURIComponent(artifactId)}`;
  const sourceUrl = `/api/artifacts/${encodeURIComponent(artifactId)}`;
  showDraftPreview({ artifactId, title, previewUrl, sourceUrl });
}

function openManuscriptDraftPreview(event) {
  const proposalId = event.currentTarget.dataset.proposalId;
  const title = event.currentTarget.dataset.draftTitle || "原稿改稿案";
  showDraftPreview({
    artifactId: `manuscript-rewrite:${proposalId}`,
    title,
    previewUrl: `/api/manuscript-rewrites/${encodeURIComponent(proposalId)}/draft.html`,
    sourceUrl: `/api/manuscript-rewrites/${encodeURIComponent(proposalId)}/draft.html`,
  });
}

function showDraftPreview({ artifactId, title, previewUrl, sourceUrl }) {
  closeSearchReport();
  state.draftPreview = { artifactId, title, previewUrl, sourceUrl };
  setPaneVisibility("right", true);
  document.querySelector(".workbench").classList.add("draft-docked");
  byId("inspector-title").textContent = "Draft Review";
  byId("inspector-default").hidden = true;
  byId("review-inspector").hidden = true;
  byId("draft-inspector").hidden = false;
  byId("draft-inspector-name").textContent = title;
  byId("draft-source-link").href = sourceUrl;
  byId("draft-source-link").download = `${title.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-") || "draft"}.html`;
  byId("draft-inspector-frame").src = previewUrl;
}

function closeDraftPreview() {
  state.draftPreview = null;
  document.querySelector(".workbench").classList.remove("draft-docked");
  byId("inspector-title").textContent = "レビュー";
  byId("draft-inspector").hidden = true;
  byId("draft-inspector-frame").src = "about:blank";
  byId("inspector-default").hidden = false;
}

function expandDraftPreview() {
  if (!state.draftPreview) return;
  const modal = byId("draft-modal");
  byId("draft-modal-name").textContent = state.draftPreview.title;
  byId("draft-modal-frame").src = state.draftPreview.previewUrl;
  if (typeof modal.showModal === "function") modal.showModal();
  else modal.setAttribute("open", "");
}

function closeDraftModal() {
  const modal = byId("draft-modal");
  if (typeof modal.close === "function" && modal.open) modal.close();
  else modal.removeAttribute("open");
  byId("draft-modal-frame").src = "about:blank";
}

function bindDraftViewer() {
  byId("search-report-close").addEventListener("click", () => closeSearchReport({ restoreFocus: true }));
  byId("draft-close").addEventListener("click", closeDraftPreview);
  byId("draft-expand").addEventListener("click", expandDraftPreview);
  byId("draft-modal-close").addEventListener("click", closeDraftModal);
  byId("draft-modal").addEventListener("close", () => {
    byId("draft-modal-frame").src = "about:blank";
  });
}

function bindNavigation() {
  document.querySelectorAll(".activity-button[data-activity]").forEach((button) => {
    button.addEventListener("click", () => {
      activateActivity(button.dataset.activity);
    });
  });
  const launcher = byId("workflow-launcher");
  const menu = byId("workflow-links-menu");
  const closeWorkflowMenu = () => {
    menu.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
  };
  launcher.addEventListener("click", () => {
    const opening = menu.hidden;
    if (opening) {
      const rect = launcher.getBoundingClientRect();
      menu.style.left = `${Math.round(rect.right + 8)}px`;
      menu.style.bottom = `${Math.max(8, window.innerHeight - rect.bottom)}px`;
    }
    menu.hidden = !opening;
    launcher.setAttribute("aria-expanded", String(opening));
    if (opening) menu.querySelector("a")?.focus({ preventScroll: true });
  });
  menu.querySelectorAll("a").forEach((link) => link.addEventListener("click", closeWorkflowMenu));
  document.addEventListener("pointerdown", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && !launcher.contains(event.target)) {
      closeWorkflowMenu();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      closeWorkflowMenu();
      launcher.focus();
    }
  });
  document.querySelectorAll(".panel-tab").forEach((button) => {
    button.addEventListener("click", () => selectPanel(button.dataset.panel));
  });
}

async function boot() {
  const selectedTabs = readSelectedTabs(window.location.search);
  state.activity = selectedTabs.activity;
  state.panel = selectedTabs.panel;
  bindPaneLayout();
  bindNavigation();
  bindDraftViewer();
  document.querySelectorAll(".activity-button[data-activity]").forEach((button) => {
    button.classList.toggle("active", button.dataset.activity === state.activity);
  });
  document.querySelectorAll(".panel-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === state.panel);
  });
  renderSidebar();
  if (state.activity === "projects") renderWelcome();
  else {
    const titles = {
      translation: "Translation",
      authoring: "Manuscript Generator",
      literature: "Literature Discovery",
      rag: "Knowledge Base",
      review: "Review",
      settings: "Settings",
    };
    byId("tab-title").textContent = titles[state.activity] || "Workbench";
    byId("editor-content").innerHTML = '<p class="empty-state">読み込み中…</p>';
  }
  try {
    [state.health, state.workflows, state.projects] = await Promise.all([
      api("/api/health"),
      api("/api/workflows"),
      api("/api/projects"),
    ]);
    renderSystemStatus();
    renderSidebar();
    // Show the requested screen before loading the slower run/history lists.
    activateActivity(state.activity, { remember: false });
    renderPanel();
    if (projectLifecycleAvailable()) {
      const allProjects = await api("/api/projects?include_archived=true");
      state.projects = allProjects.filter((project) => !project.archived_at);
      state.archivedProjects = allProjects.filter((project) => project.archived_at);
      renderSidebar();
      if (state.activity === "projects") renderWelcome();
      else if (state.activity === "authoring") renderAuthoringEditor();
    }
    await refreshRunList();
    const requestedRunId = new URLSearchParams(window.location.search).get("run_id");
    const savedRunId = requestedRunId || localStorage.getItem("fukamiru:current-run");
    if (savedRunId) {
      try {
        await refreshRunComponent(savedRunId);
        const status = state.run?.run?.status;
        if (["queued", "running", "waiting_human"].includes(status)) {
          connectToRun(savedRunId);
        } else {
          startRunPolling(null);
        }
      } catch (error) {
        localStorage.removeItem("fukamiru:current-run");
        addProblem(error.message);
        startRunPolling(null);
      }
    } else {
      startRunPolling(null);
    }
  } catch (error) {
    byId("tab-status-dot").className = "status-dot error";
    byId("connection-status").textContent = "DISCONNECTED";
    addProblem(error.message);
  }
}

boot();
