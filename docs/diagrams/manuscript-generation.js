(() => {
  "use strict";
  const copySource = window.MANUSCRIPT_NODE_COPY;
  const source = window.MANUSCRIPT_GRAPH_V32;
  if (!copySource || !source) throw new Error("Diagram data was not loaded");

  const copyById = new Map(copySource.nodes.map(node => [node.id, node]));
  const newLane = {
    classify_verification_scopes: "source",
    bind_claims_to_propositions: "source",
    derive_atomic_evidence_obligations: "source",
    bounded_research_retry: "evidence",
    decide_evidence_realization: "evidence",
    ratify_research_problem: "argument",
    select_defense_obligations: "argument",
    classify_audit_findings: "draft",
    diagnose_japanese_prose: "japanese",
    redraft_japanese_paragraphs: "japanese",
    review_japanese_sections: "japanese",
    start_paragraph_formation: "discourse",
    redraft_body_prose: "submission",
    finalize_section_headings: "submission",
    rank_proofreading_candidates: "japanese",
  };
  const newCopy = {
    prepare_note: {
      purpose: "原ノートと対象原稿を登録",
      summary: "直接入力したノート、またはProject内の目次と対応する本文ファイルから生成したMarkdownを、原ノートとして登録します。フォルダ入力では目次順と各ファイルの見出しを保持します。",
      input: "note_text、任意のsource_sectionsと生成MarkdownのProject内パス",
      output: "原ノートのsource_revision、対象原稿、目次項目と本文ファイルの対応",
    },
    decompose_memo: {
      purpose: "本文を原子命題へ分解",
      summary: "フォルダ入力では目次一覧とファイル名を命題化せず、各本文を対応する見出しとともに分割して処理します。原ノートの見出しや順序は、生成原稿の章立てとして固定しません。",
      input: "原ノートの本文。フォルダ入力では各source_sectionの見出しと本文",
      output: "atomic_propositions、本文中のsource_spans",
    },
    redraft_body_prose: {
      purpose: "脚注付き本文を節ごと一度だけ改稿",
      summary: "完成済みの節構成と段落IDを保ち、各節の文章を一度だけ改稿します。引用済み文献を参照候補と呼び直すなど、新たな作業用語を加えた候補は元稿へ戻します。書誌同定のみの引用がある変更段落は一度だけ読後評価し、自然さ・論の進行・話題の明確さを満たさない候補も元稿へ戻します。再起草ループは行いません。",
      input: "脚注・参考文献描画済みHTML、固定済み節と段落、作品カード、段落と出典の対応",
      output: "改稿版HTML、段落単位の採否と保護判定、prose_redraft_report_v31（reverted_scaffold_candidates、citation_reader_reviews、reverted_reader_candidates）",
    },
    start_paragraph_formation: {
      purpose: "節の主張を固定して段落形成を開始",
      summary: "節の中心命題と上流の出典対応を保ったまま新しい段落形成世代を開始します。後続の列挙整理と段落計画は、この世代の入力に基づきます。",
      input: "固定済み節契約、主張、根拠、上流の計画ID",
      output: "新しい段落形成世代と固定した入力対応",
    },
    finalize_section_headings: {
      purpose: "完成した本文に合わせて見出しを一度だけ整える",
      summary: "各節の冒頭と末尾の文章から実際の分析対象と比較を読み、抽象的な移動表現を避けて見出しを一度だけ編集します。節IDと見出しIDを維持します。",
      input: "一度改稿した本文、節ID・見出しID、節の冒頭・末尾段落",
      output: "見出しを反映したHTML、heading_revision_report_v31",
    },
    classify_verification_scopes: {
      purpose: "命題ごとの検証範囲を定める",
      summary: "命題の種類と、外部事実・資料内情報・留保など、本文で述べるために必要な確認水準を分けて記録します。",
      input: "propositions、classifications",
      output: "proposition_verification_scopes（各命題の検証要件と範囲）、分類レポート",
    },
    bind_claims_to_propositions: {
      purpose: "主張と原子命題の対応を記録",
      summary: "複合的な主張を構成する個々の命題との多対多対応を明示し、各主張に少なくとも一つの命題を結び付けます。",
      input: "claims、propositions、classifications、proposition_verification_scopes",
      output: "claim_proposition_bindings（主張・命題の対応）、claim_binding_report",
    },
    derive_atomic_evidence_obligations: {
      purpose: "命題単位で必要な証拠と調査を導く",
      summary: "主張のまとまりではなく原子命題ごとに、検証に必要な証拠義務と調査候補を作ります。",
      input: "propositions、proposition_verification_scopes、claim_proposition_bindings",
      output: "atomic_evidence_obligations、research_query_candidates、research_query_assignments、research_search_plan",
    },
    bounded_research_retry: {
      purpose: "未解決の証拠義務を追加調査",
      summary: "既存の調査で解決しなかった義務だけを対象に、上限を設けた追加検索を一度行い、根拠を再評価します。",
      input: "未解決のatomic_evidence_obligations、既存の検索割当・検索計画・調査結果",
      output: "更新された調査結果とevidence_context、bounded_research_retry_report",
    },
    decide_evidence_realization: {
      purpose: "命題ごとの本文での扱いを確定",
      summary: "確認できた根拠と検証要件に基づき、命題を出典付きで述べる・資料に帰属する・限定する・省く判断を固定します。未検証の中核事実は断定せず省き、未解決項目として記録して論証を続けます。",
      input: "atomic_evidence_obligations、検証済みのevidence_context、命題に対応する引用情報",
      output: "evidence_realization_decisions（扱い・根拠ID・断言可否・帰属／限定条件）",
    },
    ratify_research_problem: {
      purpose: "研究問題を根拠と照合して確定",
      summary: "研究上の問いを命題・主張・採用済み根拠と照合し、研究問題として妥当か検証します。不整合があれば制約付きで一度だけ修正します。",
      input: "research_question、propositions、claims、accepted_evidence、instructions",
      output: "検証済みresearch_problem、更新されたresearch_question、research_problem_ratification",
    },
    select_defense_obligations: {
      purpose: "論証で応答すべき反論を選ぶ",
      summary: "研究問題・主張・主張範囲・論証ノード・採用済み根拠を基に、扱うべき反論や応答義務を構造化します。",
      input: "research_problem、claims、claim_scopes、argument_nodes、accepted_evidence",
      output: "検証・正規化されたdefense_obligationsと参照の正規化情報",
    },
    classify_audit_findings: {
      purpose: "監査指摘を次の対応先へ振り分ける",
      summary: "監査結果を固定し、自動修復・人間確認・処理停止が必要な指摘に決定的に分類します。",
      input: "audit_findings、proposed_html、section_contracts、evidence_uses",
      output: "分類済みaudit bundle、auto_repair_findings、human_review_findings、blocking_findings",
    },
    rank_proofreading_candidates: {
      purpose: "校正候補を原文と比較して選ぶ",
      summary: "安全性確認を通過した日本語の修正候補を原文と文脈に照らして順位付けし、確信が持てない場合は原文を維持します。",
      input: "原文センテンス、前後の文脈、安全性確認済みの校正候補",
      output: "センテンスごとの候補選択・順位付け結果",
    },
    diagnose_japanese_prose: {
      purpose: "日本語と段落の分析を診断",
      summary: "語の組合せ、主述、指示語に加え、定義の連続や分析予定だけの段落を調べます。専門用語は文脈で判断し、この段階では本文を編集しません。",
      input: "一貫性検証済みの正本HTML、固定した節契約、根拠・脚注の対応情報",
      output: "段落IDごとの診断と保持すべき用語・出典",
    },
    redraft_japanese_paragraphs: {
      purpose: "段落単位で日本語を再起草",
      summary: "主張・観察・前後の段落から論述を組み直し、定義文や自己言及の反復を避けます。候補は出典名、脚注対応、段落ID、引用・数値の保持を確認します。",
      input: "診断結果、原文段落、固定した節契約、前回の評価で残った問題",
      output: "段落ごとの再起草候補と採否・保護判定",
    },
    review_japanese_sections: {
      purpose: "節全体の論述と日本語を評価",
      summary: "本文の初稿段階で節の論述、日本語、主張と出典の保持を確認します。問題のある節は段落再起草へ戻せます。この工程は、後段の脚注付き本文を一度だけ整える工程とは別です。",
      input: "再起草候補、原文段落、診断結果、脚注の出典対応情報",
      output: "採用済みの正本HTML、七項目の品質評価、意味・出典保持と各試行のレポート",
    },
  };
  const currentCopyOverrides = {
    build_argument_graph: {
      summary: "主張・根拠・論拠を有向の型付き関係で結びます。根拠→論拠→主張の同一主張内の経路を認め、残る内容上の不整合は候補と確認項目に残して後続へ渡します。",
      output: "argument_nodes、argument_edges、claim_scopes、未解決のグラフ確認項目",
    },
    resolve_enumeration_context: {
      purpose: "列挙候補から分析上の焦点を選ぶ",
      summary: "元の命題、主張、根拠、論拠から列挙候補を集め、節の問いと結果を添えます。候補の項目順を説明順序にせず、段落が扱う中心の関係を選ぶ材料にします。",
      input: "節契約、元の命題、主張・根拠・論拠ノード",
      output: "列挙候補と出所、節の問い・分析の焦点、必要な区別",
    },
    draft_rewrite: {
      purpose: "対象の分析を起草しLLMの推論を記録",
      summary: "固定した論証と資料から段落を新たに起草し、項目の定義より中心となる判断を述べます。段落計画の検証状況や編集用指示は本文執筆用の指示として渡しません。資料に直接ない解釈は本文で自然に論じ、理由・根拠ID・段落位置を別の推論記録へ残します。引用と脚注の対応を保ちます。",
      output: "本文候補、段落・文単位の出典対応、推論記録、確認項目",
    },
    render_evidence_footnotes: {
      summary: "根拠文と同じ段落の脚注を結び、脚注番号を読点・句点の手前に置きます。同じ出典が別段落にあるだけでは対応済みにせず、残る欠落は出典と文IDを付けて最終確認へ渡します。",
      output: "脚注付きHTML、段落単位の引用対応、未解決の出典・文ID",
    },
    join_submission_validations: {
      summary: "参考文献・脚注、HTML構造、内容の検証を合流します。記録済みの脚注欠落だけを条件付きの最終確認に送り、別種の参照エラーや構造破損は失敗として扱います。",
    },
    classify_audit_findings: {
      summary: "意味・帰属・文章品質の指摘は人間確認へ送り、原稿生成を進めます。ID不整合などの契約違反は停止し、対象節が明確な局所的な機械修正だけを自動修復へ送ります。",
    },
    join_draft_audits: {
      summary: "監査結果を合流します。指摘の分類は分類ノードの最新版、ソース保全判定は保全ノードの結果を採用し、古い自動修復指摘を復活させません。意味上の指摘は人間確認に残して進み、契約違反は停止します。",
    },
    repair_failed_sections: {
      summary: "対象節が明確な局所修正に限る任意の編集経路です。意味監査を通すための全文再生成は行いません。",
    },
    validate_evidence_ready: {
      summary: "命題ごとに断定可否を固定します。未検証の中核事実は本文で断定せず省き、確認課題として記録しますが、論証計画全体は止めません。",
      output: "authoring_evidence_gate、authoring_evidence_advisories、命題別の本文扱い",
    },
    plan_semantic_paragraphs: {
      purpose: "段落が読者に伝える判断を計画",
      summary: "段落ごとに関係・変化・対立・観察のどれを核にするかを選び、内容から境界を決めます。標準ラベル25種は任意の内部メタデータであり、項目やラベルごとに説明文を増やしません。",
    },
    polish_prose: {
      purpose: "停滞した論述を主張を保って組み直す",
      summary: "既存の主張・脚注対応を保ち、定義の連続や理論採用の定型文を段落単位で再構成します。新しい事実や無意味な留保を加えません。",
    },
    build_discourse_tree: {
      summary: "複数の論点枝を一つの本文節で扱えます。各枝の寄与は保ち、根拠のない比較・因果・統合を作らず、関係が未確定ならそのまま留保します。すべてのclaimをどこかの節で扱う契約は維持します。",
    },
  };
  const codexModelOverrides = {
    decompose_memo: { model: "GPT-6 Luna (gpt-6-luna)", effort: "low" },
    classify_propositions: { model: "GPT-6 Luna (gpt-6-luna)", effort: "low" },
    bounded_research_retry: { model: "GPT-6 Luna (gpt-6-luna)", effort: "low" },
    screen_proofreading_with_luna: { model: "GPT-5.6 Luna (gpt-5.6-luna)", effort: "none" },
    rank_proofreading_candidates: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "low" },
    validate_draft_coherence: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "high" },
    diagnose_japanese_prose: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "medium" },
    redraft_japanese_paragraphs: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "medium" },
    review_japanese_sections: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "medium" },
    redraft_body_prose: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "medium" },
    repair_final_coherence: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "high" },
    validate_footnoted_coherence: { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "high" },
  };
  const lanes = copySource.lanes.map(lane => lane.id === "submission"
    ? { ...lane, note: "脚注・参考文献→節ごとの本文改稿→見出し→構造と出典の検証→人間確認" } : lane);
  const nodeById = new Map();
  const nodes = source.nodes.map((current, order) => {
    const copy = copyById.get(current.id);
    const added = newCopy[current.id];
    const override = {
      ...currentCopyOverrides[current.id], ...source.research_policy?.[current.id],
    };
    const node = {
      ...current, order,
      lane: copy?.lane || newLane[current.id],
      purpose: override?.purpose || copy?.purpose || added?.purpose || current.title,
      summary: override?.summary || copy?.summary || added?.summary || current.title,
      input: override?.input || copy?.input || added?.input || "",
      output: override?.output || copy?.output || added?.output || "",
      wave: copy?.wave || "",
      parallel: copy?.parallel || "",
    };
    if (!lanes.some(lane => lane.id === node.lane)) throw new Error(`Unassigned lane: ${node.id}`);
    nodeById.set(node.id, node);
    return node;
  });
  const supportPairs = new Set([
    "extract_manuscript_references>resolve_reference_metadata",
    "build_source_preservation_map>plan_manuscript_structure",
    "reconcile_candidate_context>render_evidence_footnotes",
  ]);
  const edges = source.edges.map((edge, index) => {
    const pair = `${edge.from}>${edge.to}`;
    let type = "primary";
    if (edge.condition === "failure" || edge.from.startsWith("repair_")) type = "repair";
    else if (edge.to === "human_approval") type = "review";
    else if (supportPairs.has(pair)) type = "support";
    return { ...edge, index, type };
  });

  const viewport = document.getElementById("viewport");
  const world = document.getElementById("world");
  const svg = document.getElementById("edges");
  const inspector = document.getElementById("inspector");
  const switches = Object.fromEntries(["support", "repair", "review"].map(type => [
    type, document.getElementById(`show-${type}`),
  ]));
  document.getElementById("node-count").textContent = String(nodes.length);
  document.getElementById("edge-count").textContent = String(edges.length);

  const state = {
    collapsed: new Set(), focusedLane: null, selected: null, hovered: null,
    hops: 1, scale: 1, tx: 0, ty: 0, worldWidth: 0, worldHeight: 0,
    positions: new Map(), laneBoxes: new Map(), edgeElements: [],
  };
  const NODE_W = 154, NODE_H = 78, LANE_TOP = 150, LANE_GAP = 56, RANK_H = 108;
  const laneIndex = new Map(lanes.map((lane, index) => [lane.id, index]));

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }
  const moveGroupTitles = { introduction: "序論", body: "本論", conclusion: "結論" };
  function renderArgumentMoveCatalog(query = "") {
    const normalizedQuery = String(query).trim().toLocaleLowerCase("ja");
    const catalog = source.paragraph_argument_move_catalog || [];
    const matching = catalog.filter(item => [item.id, item.label, item.description]
      .some(value => String(value || "").toLocaleLowerCase("ja").includes(normalizedQuery)));
    const html = ["introduction", "body", "conclusion"].map(group => {
      const entries = matching.filter(item => item.group === group);
      if (!entries.length) return "";
      return `<section class="label-group"><h3>${moveGroupTitles[group]}<small>${entries.length}種</small></h3><ul class="label-list">${entries.map(item => `<li class="label-entry"><strong>${escapeHtml(item.label)}</strong><code>${escapeHtml(item.id)}</code><p>${escapeHtml(item.description)}</p></li>`).join("")}</ul></section>`;
    }).join("");
    document.getElementById("label-groups").innerHTML = html;
    document.getElementById("label-empty").hidden = matching.length > 0;
  }
  const labelLegend = document.getElementById("label-legend");
  const labelLegendSearch = document.getElementById("label-legend-search");
  document.getElementById("label-catalog-open").addEventListener("click", () => {
    renderArgumentMoveCatalog(labelLegendSearch.value);
    labelLegend.showModal();
    labelLegendSearch.focus();
  });
  document.getElementById("label-legend-close").addEventListener("click", () => labelLegend.close());
  labelLegendSearch.addEventListener("input", () => renderArgumentMoveCatalog(labelLegendSearch.value));
  labelLegend.addEventListener("click", event => {
    if (event.target === labelLegend) labelLegend.close();
  });
  function mode(node) {
    if (node.id === "human_approval") return "HUMAN";
    if (node.id === "submission_package") return "OUTPUT";
    if (node.id.startsWith("join_")) return "CPU BARRIER";
    return { ingestion_cpu: "CPU", codex_remote: "CODEX CLI", local_llm_gpu: "LOCAL LLM" }[node.resource_lane] || node.resource_lane;
  }
  function codexModel(node) {
    if (node.resource_lane !== "codex_remote") return null;
    return codexModelOverrides[node.id] || { model: "GPT-5.6 Sol (gpt-5.6-sol)", effort: "medium" };
  }
  function shape(node) {
    if (node.id === "human_approval") return "human";
    if (node.id === "submission_package") return "output";
    if (node.id.startsWith("join_")) return "barrier";
    return "normal";
  }
  function edgeVisible(edge) {
    return edge.type === "primary" || switches[edge.type].checked;
  }
  function actualVisible(edge) {
    return edgeVisible(edge) && !state.collapsed.has(nodeById.get(edge.from).lane)
      && !state.collapsed.has(nodeById.get(edge.to).lane);
  }

  function laneRanks(laneNodes) {
    const laneId = laneNodes[0]?.lane;
    const rank = new Map(laneNodes.map(node => [node.id, 0]));
    const localEdges = edges.filter(edge => edge.type === "primary"
      && nodeById.get(edge.from).lane === laneId && nodeById.get(edge.to).lane === laneId);
    for (let pass = 0; pass < laneNodes.length; pass++) {
      let changed = false;
      for (const edge of localEdges) {
        const next = rank.get(edge.from) + 1;
        if (next > rank.get(edge.to)) { rank.set(edge.to, next); changed = true; }
      }
      if (!changed) break;
      if (pass === laneNodes.length - 1) throw new Error(`Primary cycle in ${laneId}`);
    }
    for (const node of laneNodes) {
      if (rank.get(node.id) > 0) continue;
      const sourceEdge = edges.find(edge => edge.to === node.id && edge.type !== "support"
        && nodeById.get(edge.from).lane === laneId);
      if (sourceEdge) rank.set(node.id, rank.get(sourceEdge.from) + 1);
    }
    return rank;
  }

  function render() {
    world.querySelectorAll(".lane").forEach(element => element.remove());
    state.positions.clear();
    state.laneBoxes.clear();
    const prepared = lanes.map(lane => {
      const items = nodes.filter(node => node.lane === lane.id);
      const ranks = laneRanks(items);
      const rows = new Map();
      for (const node of items) {
        const rank = ranks.get(node.id);
        if (!rows.has(rank)) rows.set(rank, []);
        rows.get(rank).push(node);
      }
      const maxPerRow = Math.max(...[...rows.values()].map(row => row.length));
      return { lane, items, rows, width: state.collapsed.has(lane.id) ? 66 : Math.max(240, maxPerRow * (NODE_W + 18) + 30),
        maxRank: Math.max(...rows.keys()) };
    });
    const laneHeight = Math.max(...prepared.map(item => 90 + item.maxRank * RANK_H + NODE_H + 40));
    let x = 34;
    for (const [index, item] of prepared.entries()) {
      const { lane, items, rows, width } = item;
      const collapsed = state.collapsed.has(lane.id);
      const box = { x, y: LANE_TOP, width, height: laneHeight };
      state.laneBoxes.set(lane.id, box);
      const section = document.createElement("section");
      section.className = `lane${collapsed ? " collapsed" : ""}${state.focusedLane === lane.id ? " focused" : ""}`;
      section.style.cssText = `left:${x}px;top:${LANE_TOP}px;width:${width}px;height:${laneHeight}px`;
      section.dataset.lane = lane.id;
      section.innerHTML = `<div class="lane-head"><span class="lane-index">${String(index + 1).padStart(2, "0")}</span><div class="lane-title"><h2>${escapeHtml(lane.title)}</h2><small>${escapeHtml(lane.note)}</small></div><span class="lane-count">${items.length}</span><button class="lane-action collapse" type="button" title="${collapsed ? "工程を展開" : "工程を折り畳む"}" aria-label="${escapeHtml(lane.title)}を${collapsed ? "展開" : "折り畳む"}">${collapsed ? "＋" : "−"}</button><button class="lane-action focus" type="button" title="工程フォーカス" aria-label="${escapeHtml(lane.title)}にフォーカス">⌕</button></div>`;
      section.querySelector(".lane-head").addEventListener("click", event => {
        if (performance.now() < suppressLaneClickUntil) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        if (event.target.closest(".collapse")) {
          if (collapsed) state.collapsed.delete(lane.id); else state.collapsed.add(lane.id);
          if (state.selected && nodeById.get(state.selected).lane === lane.id) state.selected = null;
          state.focusedLane = null; render(); fitAll(); showInspector();
        } else { focusLane(lane.id); }
      });
      if (!collapsed) {
        for (const [rank, row] of [...rows.entries()].sort((a, b) => a[0] - b[0])) {
          const usable = width - 30;
          const step = usable / row.length;
          row.forEach((node, col) => {
            const nodeWidth = shape(node) === "barrier" ? 142 : NODE_W;
            const localX = 15 + step * col + (step - nodeWidth) / 2;
            const localY = 89 + rank * RANK_H + (shape(node) === "barrier" ? 6 : 0);
            state.positions.set(node.id, { x: x + localX, y: LANE_TOP + localY, w: nodeWidth,
              h: shape(node) === "barrier" ? 66 : NODE_H, lane: lane.id });
            const element = document.createElement("button");
            element.type = "button";
            element.className = `node ${shape(node)}`;
            element.dataset.node = node.id;
            element.style.cssText = `left:${localX}px;top:${localY}px`;
            element.innerHTML = `<span class="node-id">${escapeHtml(node.id)}</span><span class="node-purpose">${escapeHtml(node.purpose)}</span><span class="node-bottom"><em>${escapeHtml(mode(node))}</em>${node.wave ? `<span class="tiny-badge">${escapeHtml(node.wave)}</span>` : ""}${node.parallel ? '<span class="tiny-badge">∥</span>' : ""}</span>`;
            element.addEventListener("mouseenter", () => {
              if (drag) return;
              state.hovered = node.id;
              updateEmphasis();
            });
            element.addEventListener("mouseleave", () => {
              if (drag) return;
              state.hovered = null;
              updateEmphasis();
            });
            element.addEventListener("click", event => { event.stopPropagation(); selectNode(node.id); });
            section.appendChild(element);
          });
        }
      }
      world.appendChild(section);
      x += width + LANE_GAP;
    }
    state.worldWidth = x - LANE_GAP + 34;
    state.worldHeight = LANE_TOP + laneHeight + 245;
    world.style.width = `${state.worldWidth}px`;
    world.style.height = `${state.worldHeight}px`;
    svg.setAttribute("width", String(state.worldWidth));
    svg.setAttribute("height", String(state.worldHeight));
    svg.setAttribute("viewBox", `0 0 ${state.worldWidth} ${state.worldHeight}`);
    renderEdges();
    updateEmphasis();
    document.documentElement.dataset.renderedNodes = String(state.positions.size);
    document.documentElement.dataset.renderedEdges = String(state.edgeElements.length);
  }

  function edgePath(edge, typeOrdinal) {
    const a = state.positions.get(edge.from), b = state.positions.get(edge.to);
    const sourceBox = state.laneBoxes.get(a.lane), targetBox = state.laneBoxes.get(b.lane);
    const ay = a.y + a.h / 2, by = b.y + b.h / 2;
    const duplicateOffset = edge.condition === "conditional_success" ? 8 : 0;
    if (edge.type === "primary" && a.lane === b.lane && b.y > a.y) {
      const x1 = a.x + a.w / 2 + duplicateOffset, x2 = b.x + b.w / 2 + duplicateOffset;
      const mid = (a.y + a.h + b.y) / 2 + duplicateOffset;
      return `M${x1},${a.y + a.h} V${mid} H${x2} V${b.y - 5}`;
    }
    if (edge.type === "primary" && laneIndex.get(b.lane) === laneIndex.get(a.lane) + 1) {
      const trackX = sourceBox.x + sourceBox.width + 18 + (typeOrdinal % 3) * 6;
      return `M${a.x + a.w},${ay + duplicateOffset} H${trackX} V${by + duplicateOffset} H${b.x - 5}`;
    }
    const topRoute = edge.type === "support" || edge.type === "primary";
    const routeY = topRoute ? 24 + typeOrdinal * 15 :
      LANE_TOP + sourceBox.height + (edge.type === "repair" ? 27 : 145) + typeOrdinal * 15;
    const exitX = sourceBox.x + sourceBox.width + 12 + (typeOrdinal % 2) * 6;
    const enterX = targetBox.x - 12 - (typeOrdinal % 2) * 6;
    return `M${a.x + a.w},${ay} H${exitX} V${routeY} H${enterX} V${by} H${b.x - 5}`;
  }

  function renderEdges() {
    svg.querySelectorAll("path.edge").forEach(path => path.remove());
    state.edgeElements = [];
    const ordinals = { primary: 0, support: 0, repair: 0, review: 0 };
    for (const edge of edges) {
      if (!state.positions.has(edge.from) || !state.positions.has(edge.to)) continue;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.classList.add("edge", edge.type);
      path.setAttribute("d", edgePath(edge, ordinals[edge.type]++));
      path.setAttribute("marker-end", `url(#arrow-${edge.type})`);
      path.dataset.from = edge.from;
      path.dataset.to = edge.to;
      path.dataset.type = edge.type;
      svg.appendChild(path);
      state.edgeElements.push({ edge, element: path });
    }
  }

  function updateEmphasis() {
    const anchor = state.selected || state.hovered;
    const reachable = new Set(anchor ? [anchor] : []);
    const litEdges = new Set();
    let frontier = anchor ? [anchor] : [];
    for (let hop = 0; hop < state.hops; hop++) {
      const next = [];
      for (const edge of edges) {
        if (!actualVisible(edge)) continue;
        if (frontier.includes(edge.from) || frontier.includes(edge.to)) {
          litEdges.add(edge.index);
          for (const id of [edge.from, edge.to]) if (!reachable.has(id)) { reachable.add(id); next.push(id); }
        }
      }
      frontier = next;
    }
    for (const element of world.querySelectorAll(".node")) {
      const id = element.dataset.node;
      element.classList.toggle("selected", id === state.selected);
      element.classList.toggle("dimmed", Boolean(anchor) && !reachable.has(id));
    }
    for (const { edge, element } of state.edgeElements) {
      const visible = edgeVisible(edge);
      element.classList.toggle("hidden", !visible);
      element.classList.toggle("dimmed", Boolean(anchor) && !litEdges.has(edge.index));
      element.classList.toggle("emphasis", Boolean(anchor) && litEdges.has(edge.index));
    }
    const all = Object.values(switches).every(input => input.checked);
    document.getElementById("show-all").setAttribute("aria-pressed", String(all));
    document.getElementById("hop-toggle").setAttribute("aria-pressed", String(state.hops === 2));
  }

  function connectionMarkup(node, direction) {
    const matching = edges.filter(edge => direction === "in" ? edge.to === node.id : edge.from === node.id);
    if (!matching.length) return `<p>—（${direction === "in" ? "入口" : "終端"}）</p>`;
    return `<div class="connections">${matching.map(edge => {
      const other = direction === "in" ? edge.from : edge.to;
      const color = `var(--${edge.type})`;
      return `<button class="connection" type="button" data-select="${escapeHtml(other)}"><i style="--edge-color:${color}"></i><span>${escapeHtml(other)} <small>· ${escapeHtml(edge.condition)} / ${escapeHtml(edge.type)}</small></span></button>`;
    }).join("")}</div>`;
  }

  function showInspector() {
    const node = state.selected && nodeById.get(state.selected);
    if (!node) {
      if (state.focusedLane) {
        const lane = lanes.find(item => item.id === state.focusedLane);
        const members = nodes.filter(item => item.lane === lane.id);
        inspector.innerHTML = `<div class="eyebrow">工程フォーカス / ${String(laneIndex.get(lane.id) + 1).padStart(2, "0")}</div><h2>${escapeHtml(lane.title)}</h2><p>${escapeHtml(lane.note)}</p><div class="field"><h3>この工程</h3><p>${members.length}ノード。分岐・合流と接続を拡大して確認できます。</p></div><div class="field"><h3>ノード</h3><div class="connections">${members.map(item => `<button class="connection" type="button" data-select="${escapeHtml(item.id)}"><span>${escapeHtml(item.id)}</span></button>`).join("")}</div></div>`;
      }
      return;
    }
    const inference = codexModel(node);
    const executionTarget = node.resource_lane === "codex_remote"
      ? "Codex CLI（共有実行枠）"
      : node.resource_lane;
    inspector.innerHTML = `<div class="eyebrow">NODE INSPECTOR / ${escapeHtml(lanes.find(lane => lane.id === node.lane).title)}</div><h2>${escapeHtml(node.id)}</h2><p>${escapeHtml(node.purpose)}</p><div class="inspect-badges"><span class="inspect-badge">${escapeHtml(mode(node))}</span>${node.human_review === "on_defect" ? '<span class="inspect-badge">検証違反は人間確認</span>' : ""}${node.wave ? `<span class="inspect-badge">DAG並列 ${escapeHtml(node.wave)}</span>` : ""}${node.parallel ? `<span class="inspect-badge">内部並列 ${escapeHtml(node.parallel)}</span>` : ""}</div><div class="field"><h3>目的 / Summary</h3><p>${escapeHtml(node.summary)}</p></div><div class="field"><h3>Input</h3><p>${escapeHtml(node.input)}</p></div><div class="field"><h3>Output</h3><p>${escapeHtml(node.output)}</p></div><div class="field"><h3>実行環境 / Execution</h3><p>${escapeHtml(node.handler)} · ${escapeHtml(executionTarget)} · 最大${escapeHtml(node.max_attempts)}回試行</p></div>${inference ? `<div class="field"><h3>推論モデル</h3><p>${escapeHtml(inference.model)} · 推論強度 ${escapeHtml(inference.effort)}</p></div>` : ""}<div class="field"><h3>Parallel / Scheduling</h3><p>${escapeHtml(node.wave ? `DAG並列 ${node.wave}。` : "")}${escapeHtml(node.parallel ? `内部並列 ${node.parallel}。` : "")}${node.join_from?.length ? `join_from: ${escapeHtml(node.join_from.join("、"))}。` : "前段の条件を満たした後に実行。"}</p></div><div class="field"><h3>Incoming</h3>${connectionMarkup(node, "in")}</div><div class="field"><h3>Outgoing</h3>${connectionMarkup(node, "out")}</div>`;
    inspector.scrollTop = 0;
  }
  inspector.addEventListener("click", event => {
    const target = event.target.closest("[data-select]");
    if (target) selectNode(target.dataset.select);
  });
  function selectNode(id) {
    if (state.collapsed.has(nodeById.get(id).lane)) state.collapsed.delete(nodeById.get(id).lane);
    if (!state.positions.has(id)) render();
    state.selected = id;
    state.hovered = null;
    updateEmphasis();
    showInspector();
    const pos = state.positions.get(id);
    if (pos) centerOn(pos.x + pos.w / 2, pos.y + pos.h / 2, Math.max(state.scale, .9));
  }

  function applyTransform() {
    world.style.transform = `translate(${state.tx}px,${state.ty}px) scale(${state.scale})`;
  }
  function centerOn(x, y, scale) {
    state.scale = Math.max(.12, Math.min(1.6, scale));
    state.tx = viewport.clientWidth / 2 - x * state.scale;
    state.ty = viewport.clientHeight / 2 - y * state.scale;
    applyTransform();
  }
  function fitAll() {
    state.focusedLane = null;
    world.querySelectorAll(".lane").forEach(lane => lane.classList.remove("focused"));
    const scale = Math.min((viewport.clientWidth - 30) / state.worldWidth,
      (viewport.clientHeight - 22) / state.worldHeight, 1);
    centerOn(state.worldWidth / 2, state.worldHeight / 2, scale);
    if (!state.selected) showInspector();
  }
  function focusLane(id) {
    if (state.collapsed.has(id)) { state.collapsed.delete(id); render(); }
    state.focusedLane = id;
    state.selected = null;
    state.hovered = null;
    const box = state.laneBoxes.get(id);
    const scale = Math.min((viewport.clientWidth - 90) / (box.width + 50), 1.1);
    const firstScreenCenter = box.y + viewport.clientHeight / (2 * scale) - 12;
    centerOn(box.x + box.width / 2, Math.min(box.y + box.height / 2, firstScreenCenter), scale);
    world.querySelectorAll(".lane").forEach(lane => lane.classList.toggle("focused", lane.dataset.lane === id));
    updateEmphasis();
    showInspector();
  }
  function zoomAt(factor, clientX, clientY) {
    const rect = viewport.getBoundingClientRect();
    const x = clientX - rect.left, y = clientY - rect.top;
    const worldX = (x - state.tx) / state.scale, worldY = (y - state.ty) / state.scale;
    state.scale = Math.max(.12, Math.min(1.6, state.scale * factor));
    state.tx = x - worldX * state.scale; state.ty = y - worldY * state.scale;
    applyTransform();
  }
  let drag = null;
  let suppressLaneClickUntil = 0;
  function finishPan(event) {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    viewport.classList.remove("dragging");
    if (moved) suppressLaneClickUntil = performance.now() + 350;
    const underPointer = document.elementFromPoint(event.clientX, event.clientY)?.closest(".node");
    state.hovered = underPointer?.dataset.node || null;
    updateEmphasis();
  }
  viewport.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("button, input, select, a")) return;
    drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      tx: state.tx, ty: state.ty, moved: false };
    state.hovered = null;
    updateEmphasis();
  });
  viewport.addEventListener("pointermove", event => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    if (!drag.moved) {
      drag.moved = true;
      viewport.classList.add("dragging");
      viewport.setPointerCapture(drag.pointerId);
    }
    event.preventDefault();
    state.tx = drag.tx + dx;
    state.ty = drag.ty + dy;
    applyTransform();
  });
  viewport.addEventListener("pointerup", finishPan);
  viewport.addEventListener("pointercancel", finishPan);
  viewport.addEventListener("wheel", event => {
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX, event.clientY);
  }, { passive: false });
  document.getElementById("zoom-in").addEventListener("click", () => {
    const rect = viewport.getBoundingClientRect(); zoomAt(1.25, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  document.getElementById("zoom-out").addEventListener("click", () => {
    const rect = viewport.getBoundingClientRect(); zoomAt(.8, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  document.getElementById("fit-all").addEventListener("click", fitAll);
  document.getElementById("show-all").addEventListener("click", () => {
    const all = Object.values(switches).every(input => input.checked);
    Object.values(switches).forEach(input => { input.checked = !all; });
    updateEmphasis();
  });
  Object.values(switches).forEach(input => input.addEventListener("change", updateEmphasis));
  document.getElementById("hop-toggle").addEventListener("click", () => {
    state.hops = state.hops === 1 ? 2 : 1;
    updateEmphasis();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { state.selected = null; state.hovered = null; updateEmphasis(); showInspector(); }
    if (event.key === "+" || event.key === "=") document.getElementById("zoom-in").click();
    if (event.key === "-") document.getElementById("zoom-out").click();
  });
  let observedSize = null;
  new ResizeObserver(() => {
    const size = `${viewport.clientWidth}x${viewport.clientHeight}`;
    if (observedSize === null) { observedSize = size; return; }
    if (observedSize === size) return;
    observedSize = size;
    if (state.selected) {
      const pos = state.positions.get(state.selected);
      if (pos) centerOn(pos.x + pos.w / 2, pos.y + pos.h / 2, state.scale);
    } else if (!state.focusedLane) fitAll();
    else focusLane(state.focusedLane);
  }).observe(viewport);
  render();
  fitAll();
  const query = new URLSearchParams(location.search);
  if (query.get("show") === "all") { Object.values(switches).forEach(input => { input.checked = true; }); updateEmphasis(); }
  if (query.has("focus") && state.laneBoxes.has(query.get("focus"))) focusLane(query.get("focus"));
  if (query.has("node") && nodeById.has(query.get("node"))) selectNode(query.get("node"));
  window.manuscriptDag = { nodes, edges, state, selectNode, focusLane, fitAll };
})();
