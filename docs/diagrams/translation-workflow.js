(() => {
  "use strict";
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const laneLabel = lane => ({
    ingestion_cpu: "CPU",
    codex_remote: "CODEX CLI",
    local_llm_gpu: "LOCAL LLM",
    embedding_gpu: "EMBEDDING GPU",
  })[lane] || lane || "CPU";
  const canvas = document.getElementById("flow-canvas");
  const columnsRoot = document.getElementById("flow-columns");
  const svg = document.getElementById("flow-edges");
  const inspector = document.getElementById("flow-inspector");
  let selectedId = "";
  let workflow;
  let nodeElements = new Map();

  function graphRanks(nodes, edges) {
    const order = new Map(nodes.map((node, index) => [node.id, index]));
    const incoming = new Map(nodes.map(node => [node.id, 0]));
    const outgoing = new Map(nodes.map(node => [node.id, []]));
    for (const edge of edges) {
      const from = edge.from || edge.source;
      const to = edge.to || edge.target;
      if (!incoming.has(from) || !incoming.has(to)) continue;
      incoming.set(to, incoming.get(to) + 1);
      outgoing.get(from).push(to);
    }
    const queue = nodes.filter(node => incoming.get(node.id) === 0).map(node => node.id);
    const ranks = new Map(nodes.map(node => [node.id, 0]));
    let visited = 0;
    while (queue.length) {
      const id = queue.shift();
      visited += 1;
      for (const target of outgoing.get(id)) {
        ranks.set(target, Math.max(ranks.get(target), ranks.get(id) + 1));
        incoming.set(target, incoming.get(target) - 1);
        if (incoming.get(target) === 0) queue.push(target);
      }
    }
    if (visited !== nodes.length) {
      nodes.forEach((node, index) => ranks.set(node.id, order.get(node.id) ?? index));
    }
    return { ranks, order };
  }

  function showNode(node) {
    const incoming = workflow.edges.filter(edge => (edge.to || edge.target) === node.id);
    const outgoing = workflow.edges.filter(edge => (edge.from || edge.source) === node.id);
    const titleById = new Map(workflow.nodes.map(item => [item.id, item.title || item.id]));
    inspector.innerHTML = `<div class="eyebrow">NODE INSPECTOR</div><h2>${escapeHtml(node.title || node.id)}</h2><p>${escapeHtml(node.id)}</p><div class="detail"><h3>処理</h3><code>${escapeHtml(node.handler || "—")}</code></div><div class="detail"><h3>実行レーン</h3><p>${escapeHtml(laneLabel(node.resource_lane))}</p></div><div class="detail"><h3>前段</h3>${incoming.length ? `<div class="links">${incoming.map(edge => { const id = edge.from || edge.source; return `<button type="button" data-node-id="${escapeHtml(id)}">${escapeHtml(titleById.get(id) || id)}</button>`; }).join("")}</div>` : "<p>開始ノード</p>"}</div><div class="detail"><h3>次段</h3>${outgoing.length ? `<div class="links">${outgoing.map(edge => { const id = edge.to || edge.target; return `<button type="button" data-node-id="${escapeHtml(id)}">${escapeHtml(titleById.get(id) || id)}</button>`; }).join("")}</div>` : "<p>終端ノード</p>"}</div>`;
    inspector.querySelectorAll("[data-node-id]").forEach(button => button.addEventListener("click", () => selectNode(button.dataset.nodeId)));
  }

  function selectNode(id) {
    const node = workflow.nodes.find(item => item.id === id);
    if (!node) return;
    selectedId = id;
    nodeElements.forEach((element, nodeId) => element.classList.toggle("selected", nodeId === id));
    showNode(node);
  }

  function drawEdges() {
    const box = canvas.getBoundingClientRect();
    const width = Math.max(canvas.scrollWidth, canvas.clientWidth);
    const height = Math.max(canvas.scrollHeight, canvas.clientHeight);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.querySelectorAll(".flow-edge").forEach(path => path.remove());
    for (const edge of workflow.edges) {
      const from = nodeElements.get(edge.from || edge.source);
      const to = nodeElements.get(edge.to || edge.target);
      if (!from || !to) continue;
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      const x1 = a.right - box.left;
      const y1 = a.top + a.height / 2 - box.top;
      const x2 = b.left - box.left;
      const y2 = b.top + b.height / 2 - box.top;
      const bend = Math.max(32, Math.abs(x2 - x1) * 0.45);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
      path.setAttribute("class", `flow-edge${edge.condition && edge.condition !== "success" ? " conditional" : ""}`);
      path.setAttribute("marker-end", `url(#${edge.condition && edge.condition !== "success" ? "flow-arrow-conditional" : "flow-arrow"})`);
      svg.append(path);
    }
  }

  function renderGraph(definition) {
    workflow = definition;
    const nodes = definition.nodes || [];
    const edges = definition.edges || [];
    const { ranks, order } = graphRanks(nodes, edges);
    const groups = new Map();
    for (const node of nodes) {
      const rank = ranks.get(node.id) || 0;
      if (!groups.has(rank)) groups.set(rank, []);
      groups.get(rank).push(node);
    }
    columnsRoot.innerHTML = [...groups.entries()].sort((a, b) => a[0] - b[0]).map(([rank, members]) => {
      members.sort((a, b) => order.get(a.id) - order.get(b.id));
      return `<section class="flow-column"><div class="column-heading"><strong>${String(rank + 1).padStart(2, "0")}</strong>依存段階 · ${members.length}ノード</div><div class="flow-node-list">${members.map(node => `<button class="flow-node" type="button" data-flow-node="${escapeHtml(node.id)}"><small>${escapeHtml(node.id)}</small><strong>${escapeHtml(node.title || node.id)}</strong><span class="lane">${escapeHtml(laneLabel(node.resource_lane))}</span></button>`).join("")}</div></section>`;
    }).join("");
    nodeElements = new Map([...columnsRoot.querySelectorAll("[data-flow-node]")].map(element => [element.dataset.flowNode, element]));
    nodeElements.forEach((element, id) => element.addEventListener("click", () => selectNode(id)));
    document.getElementById("workflow-version").textContent = `v${definition.version} · ${definition.title || "Translation"}`;
    document.getElementById("workflow-node-count").textContent = String(nodes.length);
    document.getElementById("workflow-edge-count").textContent = String(edges.length);
    document.getElementById("workflow-note").textContent = "アプリが現在実行する翻訳定義を表示しています。実行履歴・通過経路ではなく設計上の依存関係です。";
    requestAnimationFrame(() => {
      drawEdges();
      if (nodes.length) selectNode(definition.entry_node || nodes[0].id);
    });
    window.addEventListener("resize", drawEdges, { passive: true });
  }

  fetch("/api/workflows", { cache: "no-store" })
    .then(response => {
      if (!response.ok) throw new Error(`Workflow API: ${response.status}`);
      return response.json();
    })
    .then(workflows => {
      const translation = workflows.find(item => item.name === "translation");
      if (!translation) throw new Error("現行の翻訳ワークフロー定義が見つかりません。");
      renderGraph(translation);
    })
    .catch(error => {
      document.getElementById("workflow-note").textContent = "翻訳ワークフローを読み込めませんでした。ふかみるのローカルAPI状態を確認してください。";
      document.getElementById("flow-columns").innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
      inspector.innerHTML = `<div class="eyebrow">WORKFLOW STATUS</div><h2>読み込みに失敗しました</h2><p>この図は実行中のワークフロー定義を取得します。ローカルAPIへの接続後に再読み込みしてください。</p>`;
    });
})();
