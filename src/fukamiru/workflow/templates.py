"""Initial workflow definitions; handlers are resolved by the worker registry."""

from fukamiru.workflow.graph import EdgeSpec, NodeSpec, RetryPolicy, WorkflowDefinition


def ingestion_workflow_v1() -> WorkflowDefinition:
    node_names = [
        ("locate_source", "Locate source path"),
        ("register_asset", "Register immutable SourceAsset"),
        ("extract_text", "Extract text or request OCR"),
        ("audit_ocr", "Audit OCR integrity and page continuity"),
        ("infer_structure", "Infer DocumentUnit and SourceSpan structure"),
        ("build_chunks", "Build tokenizer-measured retrieval chunks"),
        ("index_chunks", "Publish vector index snapshot"),
    ]
    nodes = [
        NodeSpec(id=node_id, handler=f"ingestion.{node_id}", title=title)
        for node_id, title in node_names
    ]
    edges = [
        EdgeSpec(source=source[0], target=target[0])
        for source, target in zip(node_names, node_names[1:], strict=False)
    ]
    return WorkflowDefinition(
        name="ingestion",
        version="1",
        title="Source ingestion",
        entry_node="locate_source",
        nodes=nodes,
        edges=edges,
    )


def ingestion_workflow() -> WorkflowDefinition:
    """Route image-only PDFs to language-specific OCR before RAG ingestion."""
    nodes = [
        NodeSpec(id="locate_source", handler="ingestion.locate_source", title="Locate source path"),
        NodeSpec(
            id="inspect_text_layer",
            handler="ingestion.inspect_text_layer",
            title="Inspect PDF text layer and create OCR job",
        ),
        NodeSpec(
            id="run_ocr",
            handler="ingestion.run_ocr",
            title="Run Mistral or NDLOCR-Lite OCR",
        ),
        NodeSpec(
            id="register_asset",
            handler="ingestion.register_asset",
            title="Register immutable SourceAsset",
        ),
        NodeSpec(id="extract_text", handler="ingestion.extract_text", title="Extract text"),
        NodeSpec(
            id="audit_ocr",
            handler="ingestion.audit_ocr",
            title="Audit OCR integrity and page continuity",
        ),
        NodeSpec(
            id="infer_structure",
            handler="ingestion.infer_structure",
            title="Infer DocumentUnit and SourceSpan structure",
        ),
        NodeSpec(
            id="build_chunks",
            handler="ingestion.build_chunks",
            title="Build tokenizer-measured retrieval chunks",
        ),
        NodeSpec(
            id="index_chunks",
            handler="ingestion.index_chunks",
            title="Publish FTS5 retrieval chunks",
        ),
    ]
    return WorkflowDefinition(
        name="ingestion",
        version="2",
        title="Language-routed source ingestion",
        entry_node="locate_source",
        nodes=nodes,
        edges=[
            EdgeSpec(source=source.id, target=target.id)
            for source, target in zip(nodes, nodes[1:], strict=False)
        ]
        + [
            EdgeSpec(
                source="build_chunks",
                target="run_ocr",
                condition="conditional_success",
                label="Invalid PDF text layer: recreate OCR from an image-only copy",
            )
        ],
    )


def research_source_ingestion_workflow() -> WorkflowDefinition:
    """Continue the normal RAG nodes from an already completed, useful OCR read."""
    base = ingestion_workflow()
    ids = {"register_asset", "extract_text", "audit_ocr", "infer_structure",
           "build_chunks", "index_chunks"}
    nodes = [node for node in base.nodes if node.id in ids]
    nodes[0] = nodes[0].model_copy(update={"handler": "research.register_source"})
    nodes.append(NodeSpec(id="publish_research_index", handler="research.publish_index",
                          title="Publish semantic index for useful research sources",
                          resource_lane="embedding_gpu", retry=RetryPolicy(max_attempts=2)))
    edges = [EdgeSpec(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)]
    nodes.append(next(node for node in base.nodes if node.id == "run_ocr"))
    edges.extend([
        EdgeSpec(source="build_chunks", target="run_ocr", condition="conditional_success"),
        EdgeSpec(source="run_ocr", target="register_asset"),
    ])
    return WorkflowDefinition(
        name="research_source_ingestion", version="1", title="Useful research source ingestion",
        entry_node="register_asset", nodes=nodes,
        edges=edges,
    )


def translation_workflow_v4() -> WorkflowDefinition:
    nodes = [
        NodeSpec(
            id="ensure_ingested",
            handler="translation.ensure_ingested",
            title="Ensure source is ingested",
        ),
        NodeSpec(
            id="prepare_context",
            handler="translation.prepare_context",
            title="Prepare RAG and terminology context",
        ),
        NodeSpec(
            id="gaperon_source_inspection",
            handler="translation.gaperon_source_inspection",
            title="Inspect French source with Gaperon",
            resource_lane="local_llm_gpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="draft_translation",
            handler="translation.draft",
            title="Draft translation",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=3),
        ),
        NodeSpec(
            id="semantic_validation",
            handler="translation.semantic_validation",
            title="Validate meaning and modality",
        ),
        NodeSpec(
            id="apply_human_revision",
            handler="translation.apply_human_revision",
            title="Persist human-corrected translation revision",
        ),
        NodeSpec(
            id="repair_translation",
            handler="translation.repair",
            title="Repair constrained defects",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=5),
        ),
        NodeSpec(
            id="human_review",
            handler="translation.human_review",
            title="Human review",
            resource_lane="codex_remote",
            human_review="always",
        ),
        NodeSpec(
            id="japanese_diagnostic",
            handler="translation.japanese_diagnostic",
            title="Diagnose restricted Japanese post-edit needs",
        ),
        NodeSpec(
            id="japanese_context",
            handler="translation.japanese_context",
            title="Prepare Japanese context RAG",
        ),
        NodeSpec(
            id="llm_jp_post_edit",
            handler="translation.llm_jp_post_edit",
            title="Run restricted llm-jp post-edit",
            resource_lane="local_llm_gpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="post_edit_validation",
            handler="translation.post_edit_validation",
            title="Validate Japanese surface quality and post-edit invariants",
        ),
        NodeSpec(
            id="finalize_translation",
            handler="translation.finalize",
            title="Finalize approved HTML",
        ),
    ]
    edges = [
        EdgeSpec(source="ensure_ingested", target="gaperon_source_inspection"),
        EdgeSpec(source="gaperon_source_inspection", target="prepare_context"),
        EdgeSpec(source="prepare_context", target="draft_translation"),
        EdgeSpec(source="draft_translation", target="semantic_validation"),
        EdgeSpec(source="apply_human_revision", target="semantic_validation"),
        EdgeSpec(source="semantic_validation", target="japanese_diagnostic", condition="success"),
        EdgeSpec(source="semantic_validation", target="repair_translation", condition="failure"),
        EdgeSpec(source="repair_translation", target="semantic_validation"),
        EdgeSpec(source="japanese_diagnostic", target="japanese_context"),
        EdgeSpec(source="japanese_context", target="llm_jp_post_edit"),
        EdgeSpec(source="llm_jp_post_edit", target="post_edit_validation"),
        EdgeSpec(source="post_edit_validation", target="human_review"),
        EdgeSpec(source="human_review", target="finalize_translation", condition="approved"),
        EdgeSpec(
            source="human_review",
            target="apply_human_revision",
            condition="needs_review",
            label="human revision requested",
        ),
    ]
    return WorkflowDefinition(
        name="translation",
        version="4",
        title="Research translation",
        entry_node="ensure_ingested",
        nodes=nodes,
        edges=edges,
    )


def translation_workflow_v5() -> WorkflowDefinition:
    """Layout-first translation for newly created runs.

    Version 4 remains available so queued or human-waiting runs can be resumed with
    the exact graph they were created from.
    """

    previous = translation_workflow_v4()
    return WorkflowDefinition(
        name="translation",
        version="5",
        title="Layout-first research translation",
        entry_node=previous.entry_node,
        nodes=previous.nodes,
        edges=previous.edges,
    )


def translation_workflow_v6() -> WorkflowDefinition:
    """Evidence-grounded, layout-first translation for newly created runs."""

    previous = translation_workflow_v5()
    inserted_nodes = [
        NodeSpec(
            id="research_trigger_detection",
            handler="translation.research_trigger_detection",
            title="Detect scholars, research and concept-bearing phrases",
        ),
        NodeSpec(
            id="academic_evidence_resolution",
            handler="translation.academic_evidence_resolution",
            title="Resolve primary academic evidence and scoped term decisions",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="translation_compliance_validation",
            handler="translation.compliance_validation",
            title="Validate forbidden translations, person names and decisions",
        ),
    ]
    replaced_edges = [
        edge
        for edge in previous.edges
        if (edge.source, edge.target)
        not in {
            ("gaperon_source_inspection", "prepare_context"),
            ("draft_translation", "semantic_validation"),
            ("repair_translation", "semantic_validation"),
        }
    ]
    replaced_edges.extend(
        [
            EdgeSpec(
                source="gaperon_source_inspection",
                target="research_trigger_detection",
            ),
            EdgeSpec(
                source="research_trigger_detection",
                target="academic_evidence_resolution",
            ),
            EdgeSpec(source="academic_evidence_resolution", target="prepare_context"),
            EdgeSpec(source="draft_translation", target="translation_compliance_validation"),
            EdgeSpec(
                source="translation_compliance_validation",
                target="semantic_validation",
                condition="success",
            ),
            EdgeSpec(
                source="translation_compliance_validation",
                target="repair_translation",
                condition="failure",
            ),
            EdgeSpec(source="repair_translation", target="translation_compliance_validation"),
        ]
    )
    return WorkflowDefinition(
        name="translation",
        version="6",
        title="Evidence-grounded layout-first research translation",
        entry_node=previous.entry_node,
        nodes=[*previous.nodes, *inserted_nodes],
        edges=replaced_edges,
    )


def translation_workflow() -> WorkflowDefinition:
    """Use a CPU lane for the database-only human review checkpoint."""
    previous = translation_workflow_v6()
    nodes = [
        node.model_copy(update={"resource_lane": "ingestion_cpu"})
        if node.id == "human_review"
        else node
        for node in previous.nodes
    ]
    return previous.model_copy(update={"version": "7", "nodes": nodes})


def corpus_sync_workflow_v1() -> WorkflowDefinition:
    nodes = [
        NodeSpec(
            id="scan_sources",
            handler="corpus.scan_sources",
            title="Scan managed source files",
        ),
        NodeSpec(
            id="ingest_sources",
            handler="corpus.ingest_sources",
            title="Ingest changed sources",
        ),
        NodeSpec(
            id="build_semantic_index",
            handler="corpus.build_semantic_index",
            title="Build BGE-M3 FAISS snapshot",
            resource_lane="embedding_gpu",
            retry=RetryPolicy(max_attempts=2),
        ),
    ]
    return WorkflowDefinition(
        name="corpus_sync",
        version="1",
        title="Managed corpus synchronization",
        entry_node="scan_sources",
        nodes=nodes,
        edges=[
            EdgeSpec(source="scan_sources", target="ingest_sources"),
            EdgeSpec(source="ingest_sources", target="build_semantic_index"),
        ],
    )


def corpus_sync_workflow() -> WorkflowDefinition:
    """Return OCR Markdown to the source queue while ingestion continues."""
    nodes = [
        NodeSpec(
            id="scan_sources",
            handler="corpus.scan_sources",
            title="Scan managed source files",
        ),
        NodeSpec(
            id="ingest_ready_sources",
            handler="corpus.ingest_ready_sources",
            title="Ingest sources and return OCR Markdown in parallel",
        ),
        NodeSpec(
            id="run_ocr_jobs",
            handler="corpus.run_ocr_jobs",
            title="Finish remaining Mistral and NDLOCR-Lite jobs",
        ),
        NodeSpec(
            id="ingest_ocr_outputs",
            handler="corpus.ingest_ocr_outputs",
            title="Ingest remaining verified OCR outputs",
        ),
        NodeSpec(
            id="build_semantic_index",
            handler="corpus.build_semantic_index",
            title="Build BGE-M3 FAISS snapshot",
            resource_lane="embedding_gpu",
            retry=RetryPolicy(max_attempts=2),
        ),
    ]
    return WorkflowDefinition(
        name="corpus_sync",
        version="2",
        title="Language-routed managed corpus synchronization",
        entry_node="scan_sources",
        nodes=nodes,
        edges=[
            EdgeSpec(source=source.id, target=target.id)
            for source, target in zip(nodes, nodes[1:], strict=False)
        ],
    )


def manuscript_rewrite_workflow_v2() -> WorkflowDefinition:
    """Generate an evidence-aware proposal without mutating the base Revision."""

    nodes = [
        NodeSpec(
            id="prepare_rewrite_context",
            handler="manuscript_rewrite.prepare_context",
            title="Prepare manuscript, outline and human review context",
        ),
        NodeSpec(
            id="review_manuscript_reasoning",
            handler="manuscript_rewrite.review_reasoning",
            title="Evaluate manuscript logic and research needs",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="resolve_manuscript_evidence",
            handler="manuscript_rewrite.resolve_evidence",
            title="Resolve local RAG and online academic evidence",
        ),
        NodeSpec(
            id="draft_rewrite",
            handler="manuscript_rewrite.draft",
            title="Draft bounded manuscript edits",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="polish_manuscript_japanese",
            handler="manuscript_rewrite.polish_japanese",
            title="Polish Japanese with local llm-jp",
            resource_lane="local_llm_gpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="persist_rewrite_proposal",
            handler="manuscript_rewrite.persist_proposal",
            title="Validate and persist the rewrite proposal",
        ),
    ]
    return WorkflowDefinition(
        name="manuscript_rewrite",
        version="2",
        title="Reasoned, evidence-aware manuscript revision",
        entry_node="prepare_rewrite_context",
        nodes=nodes,
        edges=[
            EdgeSpec(source="prepare_rewrite_context", target="review_manuscript_reasoning"),
            EdgeSpec(
                source="review_manuscript_reasoning",
                target="resolve_manuscript_evidence",
            ),
            EdgeSpec(source="resolve_manuscript_evidence", target="draft_rewrite"),
            EdgeSpec(source="draft_rewrite", target="polish_manuscript_japanese"),
            EdgeSpec(source="polish_manuscript_japanese", target="persist_rewrite_proposal"),
        ],
    )


def manuscript_rewrite_workflow_v3() -> WorkflowDefinition:
    """Add an explicit pre-persistence Japanese surface-quality gate."""

    previous = manuscript_rewrite_workflow_v2()
    surface_gate = NodeSpec(
        id="japanese_surface_quality_gate",
        handler="manuscript_rewrite.surface_quality_gate",
        title="Restore blocks that fail Japanese surface quality checks",
    )
    edges = [
        edge
        for edge in previous.edges
        if (edge.source, edge.target) != ("polish_manuscript_japanese", "persist_rewrite_proposal")
    ]
    edges.extend(
        [
            EdgeSpec(
                source="polish_manuscript_japanese",
                target="japanese_surface_quality_gate",
            ),
            EdgeSpec(
                source="japanese_surface_quality_gate",
                target="persist_rewrite_proposal",
            ),
        ]
    )
    nodes = [
        *[node for node in previous.nodes if node.id != "persist_rewrite_proposal"],
        surface_gate,
        next(node for node in previous.nodes if node.id == "persist_rewrite_proposal"),
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="3",
        title=previous.title,
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v4() -> WorkflowDefinition:
    """Plan and verify whole-document coherence around bounded prose editing."""

    previous = manuscript_rewrite_workflow_v3()
    plan = NodeSpec(
        id="plan_manuscript_structure",
        handler="manuscript_rewrite.plan_structure",
        title="Plan whole-document argument, chronology and example warrants",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    pre_polish = NodeSpec(
        id="validate_rewrite_coherence",
        handler="manuscript_rewrite.validate_coherence",
        title="Validate and repair section and document coherence",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    final = NodeSpec(
        id="validate_final_coherence",
        handler="manuscript_rewrite.validate_coherence",
        title="Verify final coherence after Japanese surface restoration",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    removed = {
        ("review_manuscript_reasoning", "resolve_manuscript_evidence"),
        ("draft_rewrite", "polish_manuscript_japanese"),
        ("japanese_surface_quality_gate", "persist_rewrite_proposal"),
    }
    edges = [edge for edge in previous.edges if (edge.source, edge.target) not in removed]
    edges.extend(
        [
            EdgeSpec(
                source="review_manuscript_reasoning",
                target="plan_manuscript_structure",
            ),
            EdgeSpec(
                source="plan_manuscript_structure",
                target="resolve_manuscript_evidence",
            ),
            EdgeSpec(source="draft_rewrite", target="validate_rewrite_coherence"),
            EdgeSpec(
                source="validate_rewrite_coherence",
                target="polish_manuscript_japanese",
            ),
            EdgeSpec(
                source="validate_rewrite_coherence",
                target="persist_rewrite_proposal",
                condition="conditional_success",
                label="persist the rewritten draft with research conditions",
            ),
            EdgeSpec(
                source="japanese_surface_quality_gate",
                target="validate_final_coherence",
            ),
            EdgeSpec(
                source="validate_final_coherence",
                target="persist_rewrite_proposal",
            ),
            EdgeSpec(
                source="validate_final_coherence",
                target="persist_rewrite_proposal",
                condition="conditional_success",
                label="persist the rewritten draft with research conditions",
            ),
        ]
    )
    previous_nodes = {node.id: node for node in previous.nodes}
    nodes = [
        previous_nodes["prepare_rewrite_context"],
        previous_nodes["review_manuscript_reasoning"],
        plan,
        previous_nodes["resolve_manuscript_evidence"],
        previous_nodes["draft_rewrite"],
        pre_polish,
        previous_nodes["polish_manuscript_japanese"],
        previous_nodes["japanese_surface_quality_gate"],
        final,
        previous_nodes["persist_rewrite_proposal"],
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="4",
        title="Planned, evidence-aware and coherence-gated manuscript revision",
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v5() -> WorkflowDefinition:
    """Compose semantic paragraphs and resolve contextual predicates before polish."""

    previous = manuscript_rewrite_workflow_v4()
    paragraph_plan = NodeSpec(
        id="plan_semantic_paragraphs",
        handler="manuscript_rewrite.plan_paragraphs",
        title="Compose sentence units into coherent semantic paragraphs",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    predicate_resolution = NodeSpec(
        id="resolve_manuscript_predicate_relations",
        handler="manuscript_rewrite.resolve_predicate_relations",
        title="Resolve omitted arguments and lexicalize discourse relations",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    removed = {
        ("resolve_manuscript_evidence", "draft_rewrite"),
        ("draft_rewrite", "validate_rewrite_coherence"),
    }
    edges = [edge for edge in previous.edges if (edge.source, edge.target) not in removed]
    edges.extend(
        [
            EdgeSpec(
                source="resolve_manuscript_evidence",
                target="plan_semantic_paragraphs",
            ),
            EdgeSpec(source="plan_semantic_paragraphs", target="draft_rewrite"),
            EdgeSpec(
                source="draft_rewrite",
                target="resolve_manuscript_predicate_relations",
            ),
            EdgeSpec(
                source="resolve_manuscript_predicate_relations",
                target="validate_rewrite_coherence",
            ),
        ]
    )
    previous_nodes = {node.id: node for node in previous.nodes}
    nodes = [
        previous_nodes["prepare_rewrite_context"],
        previous_nodes["review_manuscript_reasoning"],
        previous_nodes["plan_manuscript_structure"],
        previous_nodes["resolve_manuscript_evidence"],
        paragraph_plan,
        previous_nodes["draft_rewrite"],
        predicate_resolution,
        previous_nodes["validate_rewrite_coherence"],
        previous_nodes["polish_manuscript_japanese"],
        previous_nodes["japanese_surface_quality_gate"],
        previous_nodes["validate_final_coherence"],
        previous_nodes["persist_rewrite_proposal"],
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="5",
        title="Discourse-planned and semantically lexicalized manuscript revision",
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v6() -> WorkflowDefinition:
    """Require fail-closed review, evidence, coherence and zero-rejection gates."""

    previous = manuscript_rewrite_workflow_v5()
    return WorkflowDefinition(
        name=previous.name,
        version="6",
        title="Corpus-audited and fail-closed manuscript revision",
        entry_node=previous.entry_node,
        nodes=list(previous.nodes),
        edges=list(previous.edges),
    )


def manuscript_rewrite_workflow_v7() -> WorkflowDefinition:
    """Require complete discourse graphs and independently checked model contracts."""

    previous = manuscript_rewrite_workflow_v6()
    return WorkflowDefinition(
        name=previous.name,
        version="7",
        title="Adversarially validated corpus-audited manuscript revision",
        entry_node=previous.entry_node,
        nodes=list(previous.nodes),
        edges=list(previous.edges),
    )


def manuscript_rewrite_workflow_v8() -> WorkflowDefinition:
    """Generate N-best wording candidates and select them independently."""

    previous = manuscript_rewrite_workflow_v7()
    detector = NodeSpec(
        id="detect_japanese_polish_targets",
        handler="manuscript_rewrite.detect_polish_targets",
        title="Detect fixed-offset Japanese wording targets",
        resource_lane="ingestion_cpu",
        retry=RetryPolicy(max_attempts=2),
    )
    selector = NodeSpec(
        id="select_japanese_polish_candidates",
        handler="manuscript_rewrite.select_polish_candidates",
        title="Hard-veto and corpus-rank candidates across each section",
        resource_lane="ingestion_cpu",
        retry=RetryPolicy(max_attempts=2),
    )
    removed = {
        ("validate_rewrite_coherence", "polish_manuscript_japanese"),
        ("polish_manuscript_japanese", "japanese_surface_quality_gate"),
    }
    edges = [edge for edge in previous.edges if (edge.source, edge.target) not in removed]
    edges.extend(
        [
            EdgeSpec(
                source="validate_rewrite_coherence",
                target="detect_japanese_polish_targets",
            ),
            EdgeSpec(
                source="detect_japanese_polish_targets",
                target="polish_manuscript_japanese",
            ),
            EdgeSpec(
                source="polish_manuscript_japanese",
                target="select_japanese_polish_candidates",
            ),
            EdgeSpec(
                source="select_japanese_polish_candidates",
                target="japanese_surface_quality_gate",
            ),
        ]
    )
    previous_nodes = {node.id: node for node in previous.nodes}
    generator = previous_nodes["polish_manuscript_japanese"].model_copy(
        update={
            "title": "Generate constrained llm-jp wording candidates",
        }
    )
    nodes = [
        previous_nodes["prepare_rewrite_context"],
        previous_nodes["review_manuscript_reasoning"],
        previous_nodes["plan_manuscript_structure"],
        previous_nodes["resolve_manuscript_evidence"],
        previous_nodes["plan_semantic_paragraphs"],
        previous_nodes["draft_rewrite"],
        previous_nodes["resolve_manuscript_predicate_relations"],
        previous_nodes["validate_rewrite_coherence"],
        detector,
        generator,
        selector,
        previous_nodes["japanese_surface_quality_gate"],
        previous_nodes["validate_final_coherence"],
        previous_nodes["persist_rewrite_proposal"],
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="8",
        title="Independently ranked N-best Japanese manuscript revision",
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v9() -> WorkflowDefinition:
    """Lock one-shot discourse boundaries before all text-only revision stages."""

    previous = manuscript_rewrite_workflow_v8()
    nodes = [
        (
            node.model_copy(
                update={
                    "title": "Annotate and lock one-shot semantic boundaries",
                    "retry": RetryPolicy(max_attempts=1),
                }
            )
            if node.id == "plan_semantic_paragraphs"
            else node
        )
        for node in previous.nodes
    ]
    edges = [
        edge
        for edge in previous.edges
        if not (
            edge.source == "validate_rewrite_coherence"
            and edge.target == "persist_rewrite_proposal"
            and edge.condition == "conditional_success"
        )
    ]
    edges.append(
        EdgeSpec(
            source="validate_rewrite_coherence",
            target="detect_japanese_polish_targets",
            condition="conditional_success",
            label="finish text-only quality gates before conditional persistence",
        )
    )
    return WorkflowDefinition(
        name=previous.name,
        version="9",
        title="One-shot discourse-locked Japanese manuscript revision",
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v10() -> WorkflowDefinition:
    """Normalize scholarly terminal sections before the one-shot boundary lock."""

    previous = manuscript_rewrite_workflow_v9()
    return WorkflowDefinition(
        name=previous.name,
        version="10",
        title="Terminal-structure-aware Japanese manuscript revision",
        entry_node=previous.entry_node,
        nodes=previous.nodes,
        edges=previous.edges,
    )


def manuscript_rewrite_workflow_v11() -> WorkflowDefinition:
    """Finalize coherence monotonically and freeze the persistence candidate."""

    previous = manuscript_rewrite_workflow_v10()
    return WorkflowDefinition(
        name=previous.name,
        version="11",
        title="Monotonic final-quality Japanese manuscript revision",
        entry_node=previous.entry_node,
        nodes=previous.nodes,
        edges=previous.edges,
    )


def manuscript_rewrite_workflow_v12() -> WorkflowDefinition:
    """Revalidate repairs, qualify evidence, and pause for real coherence review."""

    previous = manuscript_rewrite_workflow_v11()
    return WorkflowDefinition(
        name=previous.name,
        version="12",
        title="Evidence-qualified reviewable Japanese manuscript revision",
        entry_node=previous.entry_node,
        nodes=previous.nodes,
        edges=previous.edges,
    )


def manuscript_rewrite_workflow_v13() -> WorkflowDefinition:
    """Resolve introductions and enumerations before immutable paragraph planning."""

    previous = manuscript_rewrite_workflow_v12()
    enumeration = NodeSpec(
        id="resolve_enumeration_context",
        handler="manuscript_rewrite.resolve_enumeration_context",
        title="Resolve item roles and implicit context in manuscript enumerations",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    edges = [
        edge
        for edge in previous.edges
        if (edge.source, edge.target) != ("resolve_manuscript_evidence", "plan_semantic_paragraphs")
    ]
    edges.extend(
        [
            EdgeSpec(
                source="resolve_manuscript_evidence",
                target="resolve_enumeration_context",
            ),
            EdgeSpec(
                source="resolve_enumeration_context",
                target="plan_semantic_paragraphs",
            ),
        ]
    )
    prior = {node.id: node for node in previous.nodes}
    nodes = [
        prior["prepare_rewrite_context"],
        prior["review_manuscript_reasoning"],
        prior["plan_manuscript_structure"],
        prior["resolve_manuscript_evidence"],
        enumeration,
        prior["plan_semantic_paragraphs"],
        prior["draft_rewrite"],
        prior["resolve_manuscript_predicate_relations"],
        prior["validate_rewrite_coherence"],
        prior["detect_japanese_polish_targets"],
        prior["polish_manuscript_japanese"],
        prior["select_japanese_polish_candidates"],
        prior["japanese_surface_quality_gate"],
        prior["validate_final_coherence"],
        prior["persist_rewrite_proposal"],
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="13",
        title="Context-resolved Japanese scholarly manuscript revision",
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v16() -> WorkflowDefinition:
    """Render references before the final immutable-artifact coherence gate."""

    previous = manuscript_rewrite_workflow_v13()
    extract = NodeSpec(
        id="extract_manuscript_references",
        handler="manuscript_rewrite.extract_references",
        title="Extract Japanese bibliography records",
        resource_lane="ingestion_cpu",
        retry=RetryPolicy(max_attempts=2),
    )
    resolve = NodeSpec(
        id="resolve_reference_metadata",
        handler="manuscript_rewrite.resolve_reference_metadata",
        title="Resolve bibliography identities and missing metadata",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=2),
    )
    render = NodeSpec(
        id="render_manuscript_references",
        handler="manuscript_rewrite.render_references",
        title="Render Japanese references with source-tracked fields",
        resource_lane="ingestion_cpu",
        retry=RetryPolicy(max_attempts=2),
    )
    quality = NodeSpec(
        id="validate_reference_quality",
        handler="manuscript_rewrite.reference_quality_gate",
        title="Validate bibliography completeness, provenance and idempotency",
        resource_lane="ingestion_cpu",
        retry=RetryPolicy(max_attempts=2),
    )
    removed = {
        ("prepare_rewrite_context", "review_manuscript_reasoning"),
        ("resolve_manuscript_evidence", "resolve_enumeration_context"),
        ("japanese_surface_quality_gate", "validate_final_coherence"),
        ("validate_final_coherence", "persist_rewrite_proposal"),
    }
    edges = [edge for edge in previous.edges if (edge.source, edge.target) not in removed]
    edges.extend(
        [
            EdgeSpec(
                source="prepare_rewrite_context",
                target="extract_manuscript_references",
            ),
            EdgeSpec(
                source="extract_manuscript_references",
                target="review_manuscript_reasoning",
            ),
            EdgeSpec(
                source="resolve_manuscript_evidence",
                target="resolve_reference_metadata",
            ),
            EdgeSpec(
                source="resolve_reference_metadata",
                target="resolve_enumeration_context",
            ),
            EdgeSpec(
                source="japanese_surface_quality_gate",
                target="render_manuscript_references",
            ),
            EdgeSpec(
                source="render_manuscript_references",
                target="validate_reference_quality",
            ),
            EdgeSpec(
                source="validate_reference_quality",
                target="validate_final_coherence",
            ),
            EdgeSpec(
                source="validate_final_coherence",
                target="persist_rewrite_proposal",
            ),
            EdgeSpec(
                source="validate_final_coherence",
                target="persist_rewrite_proposal",
                condition="conditional_success",
                label="persist the final immutable draft with research conditions",
            ),
        ]
    )
    prior = {node.id: node for node in previous.nodes}
    nodes = [
        prior["prepare_rewrite_context"],
        extract,
        prior["review_manuscript_reasoning"],
        prior["plan_manuscript_structure"],
        prior["resolve_manuscript_evidence"],
        resolve,
        prior["resolve_enumeration_context"],
        prior["plan_semantic_paragraphs"],
        prior["draft_rewrite"],
        prior["resolve_manuscript_predicate_relations"],
        prior["validate_rewrite_coherence"],
        prior["detect_japanese_polish_targets"],
        prior["polish_manuscript_japanese"],
        prior["select_japanese_polish_candidates"],
        prior["japanese_surface_quality_gate"],
        render,
        quality,
        prior["validate_final_coherence"],
        prior["persist_rewrite_proposal"],
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="16",
        title="Immutable-final-gate Japanese scholarly manuscript revision",
        entry_node=previous.entry_node,
        nodes=nodes,
        edges=edges,
    )


def manuscript_rewrite_workflow_v17() -> WorkflowDefinition:
    """Pre-commit and monotonically validate cross-process argument planning."""

    previous = manuscript_rewrite_workflow_v16()
    return WorkflowDefinition(
        name=previous.name,
        version="17",
        title="Pre-commit-safe Japanese scholarly manuscript revision",
        entry_node=previous.entry_node,
        nodes=previous.nodes,
        edges=previous.edges,
    )


def manuscript_rewrite_workflow_v18() -> WorkflowDefinition:
    """Repair manuscript structure under consistent cross-process contracts."""

    previous = manuscript_rewrite_workflow_v17()
    return WorkflowDefinition(
        name=previous.name,
        version="18",
        title="Contract-consistent Japanese scholarly manuscript revision",
        entry_node=previous.entry_node,
        nodes=previous.nodes,
        edges=previous.edges,
    )


def manuscript_rewrite_workflow_v19() -> WorkflowDefinition:
    previous = manuscript_rewrite_workflow_v18()
    additions = [
        NodeSpec(
            id="plan_research_queries",
            handler="manuscript_rewrite.plan_research_queries",
            title="Plan source-specific research queries",
            resource_lane="codex_remote",
        ),
        NodeSpec(
            id="validate_evidence_support",
            handler="manuscript_rewrite.validate_evidence_support",
            title="Validate claim support and labelled inference",
            resource_lane="codex_remote",
        ),
    ]
    edges = []
    for edge in previous.edges:
        source, target = edge.source, edge.target
        if source == "plan_manuscript_structure" and target == "resolve_manuscript_evidence":
            target = "plan_research_queries"
        elif source == "resolve_manuscript_evidence":
            target = "validate_evidence_support"
        elif source == "draft_rewrite":
            target = "validate_rewrite_coherence"
        elif source == "resolve_manuscript_predicate_relations":
            target = "detect_japanese_polish_targets"
        elif source == "validate_rewrite_coherence":
            target = "resolve_manuscript_predicate_relations"
        edges.append(edge.model_copy(update={"source": source, "target": target}))
    edges.extend(
        [
            EdgeSpec(source="plan_research_queries", target="resolve_manuscript_evidence"),
            EdgeSpec(source="validate_evidence_support", target="resolve_reference_metadata"),
        ]
    )
    nodes = [
        node.model_copy(update={"resource_lane": "codex_remote"})
        if node.id == "persist_rewrite_proposal"
        else node
        for node in previous.nodes
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="19",
        title="Source-grounded and generation-contract-safe manuscript revision",
        entry_node=previous.entry_node,
        nodes=[*nodes, *additions],
        edges=edges,
    )


def manuscript_rewrite_workflow_v20() -> WorkflowDefinition:
    previous = manuscript_rewrite_workflow_v19()
    calibration = NodeSpec(
        id="calibrate_manuscript_certainty",
        handler="manuscript_rewrite.calibrate_certainty",
        title="根拠の確実度に応じて解釈・仮説の断定を調整",
        resource_lane="codex_remote",
        retry=RetryPolicy(max_attempts=1),
    )
    edges = [
        edge.model_copy(update={"target": calibration.id})
        if edge.source == "draft_rewrite"
        else edge
        for edge in previous.edges
    ]
    edges.append(EdgeSpec(source=calibration.id, target="validate_rewrite_coherence"))
    return previous.model_copy(
        update={
            "version": "20",
            "nodes": [*previous.nodes, calibration],
            "edges": edges,
            "title": "Checkpointed, subclaim-grounded manuscript revision",
        }
    )


def manuscript_rewrite_workflow_v21() -> WorkflowDefinition:
    previous = manuscript_rewrite_workflow_v20()
    additions = [
        NodeSpec(
            id="plan_term_introductions",
            handler="manuscript_rewrite.plan_terms",
            title="初出語の導入理由・引用の論証上の役割を計画",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=1),
        ),
        NodeSpec(
            id="analyze_japanese_structure",
            handler="manuscript_rewrite.analyze_japanese",
            title="MeCab・係り受け解析と曖昧箇所の確認",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=1),
        ),
        NodeSpec(
            id="validate_discourse_introductions",
            handler="manuscript_rewrite.validate_terms",
            title="初出語と引用目的の最終本文対応を検証",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=1),
        ),
    ]
    redirects = {
        "plan_semantic_paragraphs": "plan_term_introductions",
        "detect_japanese_polish_targets": "analyze_japanese_structure",
        "persist_rewrite_proposal": "validate_discourse_introductions",
    }
    edges = [
        e.model_copy(update={"target": redirects.get(e.target, e.target)}) for e in previous.edges
    ]
    edges.extend(EdgeSpec(source=source, target=target) for target, source in redirects.items())
    return previous.model_copy(
        update={
            "version": "21",
            "nodes": [*previous.nodes, *additions],
            "edges": edges,
            "title": "Parsed Japanese and discourse-led revision",
        }
    )


def manuscript_rewrite_workflow_v22() -> WorkflowDefinition:
    """Keep the released V22 graph independently available for existing runs."""
    previous = manuscript_rewrite_workflow_v21()
    return previous.model_copy(
        update={
            "version": "22",
            "title": "Version-isolated parsed Japanese and discourse-led revision",
        }
    )


def manuscript_rewrite_workflow_v23() -> WorkflowDefinition:
    """Opt-in V23: parallel discourse inference with V22 quality contracts."""
    return WorkflowDefinition.model_validate(
        {
            **manuscript_rewrite_workflow_v22().model_dump(),
            "version": "23",
            "title": "Checkpointed parallel discourse revision",
        }
    )


def manuscript_rewrite_workflow_v24() -> WorkflowDefinition:
    """V24: bounded edits, conditional review, and parallel analysis."""
    return WorkflowDefinition.model_validate(
        {
            **manuscript_rewrite_workflow_v23().model_dump(),
            "version": "24",
            "title": "Conditionally reviewable parallel manuscript revision",
        }
    )


def manuscript_rewrite_workflow_v25() -> WorkflowDefinition:
    """Run local analysis and query planning on CPU, escalating only as needed."""
    previous = manuscript_rewrite_workflow_v24()
    nodes = [
        node.model_copy(update={"resource_lane": "ingestion_cpu"})
        if node.id in {"analyze_japanese_structure", "plan_research_queries"}
        else node
        for node in previous.nodes
    ]
    return previous.model_copy(update={"version": "25", "nodes": nodes})


def manuscript_rewrite_workflow_v26() -> WorkflowDefinition:
    """Freeze the rendered candidate before read-only final audits and judgement."""
    previous = manuscript_rewrite_workflow_v25()
    additions = [
        NodeSpec(
            id="validate_rendered_structure",
            handler="manuscript_rewrite.validate_rendered_structure",
            title="描画後の正本構造を決定的に検証",
            resource_lane="ingestion_cpu",
        ),
        NodeSpec(
            id="repair_final_coherence",
            handler="manuscript_rewrite.repair_final_coherence",
            title="最終原稿の意味上の整合性を局所修復",
            resource_lane="codex_remote",
        ),
        NodeSpec(
            id="freeze_final_candidate",
            handler="manuscript_rewrite.freeze_final_candidate",
            title="最終候補とハッシュを固定",
            resource_lane="ingestion_cpu",
        ),
        NodeSpec(
            id="audit_final_findings",
            handler="manuscript_rewrite.audit_final_findings",
            title="重大指摘と未解決事項を再監査",
            resource_lane="codex_remote",
        ),
    ]
    replaced_sources = {
        "validate_reference_quality",
        "validate_final_coherence",
        "validate_discourse_introductions",
    }
    edges = [edge for edge in previous.edges if edge.source not in replaced_sources]
    edges.extend(
        [
            EdgeSpec(source="validate_reference_quality", target="validate_rendered_structure"),
            EdgeSpec(source="validate_rendered_structure", target="repair_final_coherence"),
            EdgeSpec(source="repair_final_coherence", target="freeze_final_candidate"),
            EdgeSpec(
                source="repair_final_coherence",
                target="freeze_final_candidate",
                condition="conditional_success",
            ),
            EdgeSpec(source="freeze_final_candidate", target="validate_discourse_introductions"),
            EdgeSpec(source="validate_discourse_introductions", target="audit_final_findings"),
            EdgeSpec(source="audit_final_findings", target="validate_final_coherence"),
            EdgeSpec(source="validate_final_coherence", target="persist_rewrite_proposal"),
            EdgeSpec(
                source="validate_final_coherence",
                target="persist_rewrite_proposal",
                condition="conditional_success",
            ),
        ]
    )
    handler_overrides = {
        "validate_rewrite_coherence": "manuscript_rewrite.validate_coherence_v26",
        "validate_final_coherence": "manuscript_rewrite.final_judgement_v26",
        "validate_discourse_introductions": "manuscript_rewrite.validate_terms_v26",
        "persist_rewrite_proposal": "manuscript_rewrite.persist_proposal_v26",
    }
    nodes = [
        node.model_copy(
            update={
                "handler": handler_overrides.get(node.id, node.handler),
                "resource_lane": (
                    "ingestion_cpu"
                    if node.id in {"validate_final_coherence", "persist_rewrite_proposal"}
                    else node.resource_lane
                ),
            }
        )
        for node in previous.nodes
    ]
    return WorkflowDefinition(
        name=previous.name,
        version="26",
        title="Frozen-candidate manuscript revision",
        entry_node=previous.entry_node,
        nodes=[*nodes, *additions],
        edges=edges,
    )


def manuscript_rewrite_workflow_v27() -> WorkflowDefinition:
    """Run coherence reads concurrently with resumable cross-section supplements."""
    previous = manuscript_rewrite_workflow_v26()
    handler_overrides = {
        "validate_rewrite_coherence": "manuscript_rewrite.validate_coherence_v27",
        "repair_final_coherence": "manuscript_rewrite.repair_final_coherence_v27",
    }
    nodes = [
        node.model_copy(update={"handler": handler_overrides.get(node.id, node.handler)})
        for node in previous.nodes
    ]
    return previous.model_copy(
        update={
            "version": "27",
            "title": "Resumable parallel-coherence manuscript revision",
            "nodes": nodes,
        }
    )


def manuscript_rewrite_workflow_v28() -> WorkflowDefinition:
    """Generate context-locked phrase alternatives and apply only proven improvements."""
    previous = manuscript_rewrite_workflow_v27()
    return previous.model_copy(
        update={
            "version": "28",
            "title": "Context-locked candidate manuscript revision",
        }
    )


def manuscript_rewrite_workflow_v29() -> WorkflowDefinition:
    """Screen prose, then render and re-audit evidence-linked footnotes."""

    previous = manuscript_rewrite_workflow_v28()
    replacements = {
        "resolve_manuscript_predicate_relations": NodeSpec(
            id="extract_proofreading_sentences",
            handler="manuscript_rewrite.extract_proofreading_sentences_v29",
            title="Extract stable Japanese sentences and work-title registry",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        "analyze_japanese_structure": NodeSpec(
            id="generate_proofreading_candidates",
            handler="manuscript_rewrite.generate_proofreading_candidates_v29",
            title="Generate three full-sentence llm-jp alternatives",
            resource_lane="local_llm_gpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        "detect_japanese_polish_targets": NodeSpec(
            id="preprocess_proofreading_candidates",
            handler="manuscript_rewrite.preprocess_proofreading_candidates_v29",
            title="Normalize, deduplicate, protect, lint and create raw diffs",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        "polish_manuscript_japanese": NodeSpec(
            id="screen_proofreading_with_luna",
            handler="manuscript_rewrite.screen_proofreading_with_luna_v29",
            title="Screen eligible changes with GPT-5.6 Luna",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=2),
        ),
        "select_japanese_polish_candidates": NodeSpec(
            id="select_proofreading_candidates",
            handler="manuscript_rewrite.select_proofreading_candidates_v29",
            title="Separate clean safety status from candidate selection",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        "japanese_surface_quality_gate": NodeSpec(
            id="publish_proofreading_shadow",
            handler="manuscript_rewrite.publish_proofreading_shadow_v29",
            title="Publish safe candidate status and shadow previews",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        "render_manuscript_references": NodeSpec(
            id="render_manuscript_references",
            handler="manuscript_rewrite.render_references",
            title="Render references in the user's bibliography house style",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
    }
    additions = [
        NodeSpec(
            id="render_evidence_footnotes",
            handler="manuscript_rewrite.render_evidence_footnotes_v29",
            title="Render evidence-linked footnotes and reconcile the final bibliography",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="validate_footnoted_structure",
            handler="manuscript_rewrite.validate_rendered_structure",
            title="Validate canonical structure after footnote rendering",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="validate_footnoted_references",
            handler="manuscript_rewrite.reference_quality_gate",
            title="Validate the bibliography reconciled from evidence footnotes",
            resource_lane="ingestion_cpu",
            retry=RetryPolicy(max_attempts=2),
        ),
        NodeSpec(
            id="validate_footnoted_coherence",
            handler="manuscript_rewrite.validate_footnoted_coherence_v29",
            title="Read-only re-audit of the footnoted final candidate",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=2),
        ),
    ]
    id_map = {old_id: node.id for old_id, node in replacements.items()}
    nodes = [replacements.get(node.id, node) for node in previous.nodes]
    edges = [
        edge.model_copy(
            update={
                "source": id_map.get(edge.source, edge.source),
                "target": (
                    "render_evidence_footnotes"
                    if edge.source == "repair_final_coherence"
                    and edge.target == "freeze_final_candidate"
                    else id_map.get(edge.target, edge.target)
                ),
            }
        )
        for edge in previous.edges
    ]
    edges.extend(
        [
            EdgeSpec(
                source="render_evidence_footnotes",
                target="validate_footnoted_references",
            ),
            EdgeSpec(
                source="validate_footnoted_references",
                target="validate_footnoted_structure",
            ),
            EdgeSpec(
                source="validate_footnoted_structure",
                target="validate_footnoted_coherence",
            ),
            EdgeSpec(
                source="validate_footnoted_coherence",
                target="freeze_final_candidate",
            ),
            EdgeSpec(
                source="validate_footnoted_coherence",
                target="freeze_final_candidate",
                condition="conditional_success",
            ),
        ]
    )
    return previous.model_copy(
        update={
            "version": "29",
            "title": "Evidence-footnoted Japanese proofreading production shadow",
            "nodes": [*nodes, *additions],
            "edges": edges,
        }
    )


def manuscript_rewrite_workflow() -> WorkflowDefinition:
    """Use V29 for new runs while preserving historical workflow versions."""
    return manuscript_rewrite_workflow_v29()


def authoring_workflow(*, version: str = "32") -> WorkflowDefinition:
    """One Fukamiru-native DAG from source note through submission review."""

    if version not in {"30", "31", "32"}:
        raise ValueError(f"Unsupported authoring workflow version: {version}")
    structure_aware = version in {"31", "32"}

    v29_nodes = {node.id: node for node in manuscript_rewrite_workflow_v29().nodes}

    def authoring_node(
        node_id: str,
        title: str,
        lane: str = "ingestion_cpu",
        attempts: int = 1,
        *,
        join_from: list[str] | None = None,
        join_generation_source: str | None = None,
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            handler=f"authoring.{node_id}",
            title=title,
            resource_lane=lane,
            retry=RetryPolicy(max_attempts=attempts),
            join_from=join_from or [],
            join_generation_source=join_generation_source,
        )

    custom = {
        node.id: node
        for node in (
            authoring_node("prepare_note", "Register source note and target paper", attempts=2),
            authoring_node("prepare_rewrite_context", "Build source lineage context"),
            authoring_node("extract_manuscript_references", "Extract source references"),
            authoring_node("decompose_memo", "Decompose atomic propositions", "codex_remote", 3),
            authoring_node(
                "classify_propositions", "Classify proposition types", "codex_remote", 3
            ),
            authoring_node(
                "classify_verification_scopes",
                "Classify atomic verification scopes",
            ),
            authoring_node(
                "review_manuscript_reasoning",
                "Inventory reasoning assets",
                "codex_remote",
                3,
            ),
            authoring_node(
                "formulate_research_question", "Fix one research question", "codex_remote", 3
            ),
            authoring_node("build_claim_tree", "Build claim hierarchy", "codex_remote", 3),
            authoring_node(
                "bind_claims_to_propositions",
                "Bind claim wrappers to atomic propositions",
            ),
            authoring_node(
                "derive_atomic_evidence_obligations",
                "Derive atomic evidence obligations",
            ),
            authoring_node("build_source_preservation_map", "Map source propositions to claims"),
            authoring_node(
                "resolve_reference_metadata",
                "Resolve and deduplicate reference metadata",
                join_from=["extract_manuscript_references", "bounded_research_retry"],
                join_generation_source="prepare_note",
            ).model_copy(update={"handler": "manuscript_rewrite.resolve_reference_metadata"}),
            authoring_node(
                "decide_evidence_realization",
                "Lock assertion, attribution, qualification, or omission",
            ),
            authoring_node(
                "bounded_research_retry",
                "Retry unresolved evidence and rival searches once",
                "codex_remote",
                1,
            ),
            authoring_node(
                "validate_evidence_ready",
                "Validate atomic evidence obligations",
            ),
            authoring_node(
                "ratify_research_problem", "Ground the research problem", "codex_remote", 3
            ),
            authoring_node(
                "build_argument_graph", "Build claim-evidence-warrant graph", "codex_remote", 3
            ).model_copy(update={"human_review": "never"}),
            authoring_node(
                "select_defense_obligations", "Select material defenses", "codex_remote", 3
            ),
            authoring_node("generate_hypotheses", "Generate bounded hypotheses", "codex_remote", 3),
            authoring_node("validate_argument_coverage", "Validate argument-role coverage"),
            authoring_node(
                "build_discourse_tree", "Build rhetorical discourse tree", "codex_remote", 3
            ),
            authoring_node(
                "plan_manuscript_structure",
                "Plan variable manuscript structure",
                "codex_remote",
                3,
                join_from=["build_source_preservation_map", "build_discourse_tree"],
                join_generation_source="derive_atomic_evidence_obligations",
            ),
            authoring_node("lock_section_contracts", "Lock section contracts", "codex_remote", 3),
            *((authoring_node(
                "start_paragraph_formation",
                "Begin a fresh paragraph formation generation with locked claims",
            ),) if structure_aware else ()),
            authoring_node(
                "resolve_enumeration_context",
                "Identify analytical focus behind candidate lists"
                if structure_aware else "Resolve enumerations and contrasts",
            ),
            authoring_node(
                "plan_semantic_paragraphs",
                "Plan paragraphs around analytical development"
                if structure_aware else "Plan semantic paragraph moves",
                "codex_remote",
                3,
            ),
            authoring_node("plan_term_introductions", "Plan first use of required terms"),
            authoring_node(
                "draft_rewrite",
                "Draft analytical prose and record model inferences"
                if structure_aware else "Draft from the locked argument and discourse plan",
                "codex_remote",
                3,
                join_from=["resolve_enumeration_context", "plan_term_introductions"],
                join_generation_source=(
                    "start_paragraph_formation" if structure_aware else "lock_section_contracts"
                ),
            ).model_copy(update={"human_review": "on_defect" if structure_aware else "never"}),
            authoring_node("reconcile_candidate_context", "Reconcile canonical draft context"),
            authoring_node("validate_source_preservation", "Validate source preservation"),
            authoring_node(
                "audit_structure", "Audit claim and discourse integrity", "codex_remote"
            ),
            authoring_node(
                "classify_audit_findings", "Classify audit findings deterministically"
            ),
            authoring_node(
                "join_draft_audits",
                "Join source and structural audits",
                join_from=[
                    "validate_source_preservation",
                    "classify_audit_findings" if structure_aware else "audit_structure",
                ],
                join_generation_source="reconcile_candidate_context",
            ),
            authoring_node("repair_failed_sections", "Repair failed sections", "codex_remote", 3),
            authoring_node(
                "polish_prose",
                "Recompose stalled prose without changing claims"
                if structure_aware else "Apply local prose corrections",
                "codex_remote", 3,
            ),
            authoring_node(
                "validate_draft_coherence", "Validate discourse coherence", "codex_remote", 2
            ),
            authoring_node(
                "diagnose_japanese_prose",
                "Diagnose Japanese prose and analytical progress"
                if structure_aware else "Diagnose Japanese vocabulary and syntax",
                "codex_remote",
                2,
            ),
            authoring_node(
                "redraft_japanese_paragraphs",
                "Redraft Japanese paragraphs with section context",
                "codex_remote",
                5,
            ),
            authoring_node(
                "review_japanese_sections",
                "Review section analysis, Japanese flow and argument preservation"
                if structure_aware else "Review Japanese section flow and argument preservation",
                "codex_remote",
                5,
            ),
            authoring_node("japanese_surface_quality_gate", "Validate applied Japanese surface"),
            authoring_node(
                "rank_proofreading_candidates",
                "Select clear Japanese improvements against the original",
                "codex_remote",
                2,
            ),
            authoring_node(
                "reconcile_candidate_context_after_proofreading",
                "Reconcile post-proofreading canonical candidate",
            ),
            *((authoring_node(
                "redraft_body_prose",
                "Edit Japanese prose once per section within fixed HTML text slots",
                "codex_remote",
                1,
            ),) if structure_aware else ()),
            *((authoring_node(
                "finalize_section_headings",
                "Edit final Japanese section headings once against finished prose",
                "codex_remote",
                1,
            ),) if structure_aware else ()),
            authoring_node(
                "join_submission_validations",
                "Join bibliography, reference, and deterministic structure checks"
                if structure_aware else
                "Join bibliography, reference, structure, and coherence gates",
                join_from=[
                    "validate_reference_quality",
                    "validate_footnoted_references",
                    "validate_footnoted_structure",
                    "validate_footnoted_coherence",
                ],
                join_generation_source=(
                    "finalize_section_headings"
                    if structure_aware else "render_manuscript_references"
                ),
            ),
            authoring_node(
                "audit_final_findings",
                "Audit final findings after Japanese and discourse validation",
            ).model_copy(update={"handler": "manuscript_rewrite.audit_final_findings"}),
            authoring_node("evaluate_human_revision_requirement", "Evaluate human revision need"),
            authoring_node("finalize_authoring", "Persist the frozen manuscript proposal"),
            authoring_node("submission_package", "Write manuscript and audit manifest"),
            authoring_node("human_approval", "Request approval only when required"),
        )
    }

    reused_ids = {
        "plan_research_queries",
        "resolve_manuscript_evidence",
        "validate_evidence_support",
        "calibrate_manuscript_certainty",
        "repair_final_coherence",
        "extract_proofreading_sentences",
        "generate_proofreading_candidates",
        "preprocess_proofreading_candidates",
        "screen_proofreading_with_luna",
        "select_proofreading_candidates",
        "publish_proofreading_shadow",
        "render_evidence_footnotes",
        "render_manuscript_references",
        "validate_reference_quality",
        "validate_footnoted_references",
        "validate_footnoted_structure",
        "validate_footnoted_coherence",
        "freeze_final_candidate",
        "validate_discourse_introductions",
        "validate_final_coherence",
    }
    if not structure_aware:
        custom.pop("ratify_research_problem")
        custom.pop("select_defense_obligations")
    nodes_by_id = {**{node_id: v29_nodes[node_id] for node_id in reused_ids}, **custom}
    if version == "32":
        for node_id, title in {
            "plan_research_queries": "Plan compact multilingual literature queries",
            "resolve_manuscript_evidence": (
                "Reuse local RAG passages before external literature discovery"
            ),
            "validate_evidence_support": (
                "Review passages and read missing source context on demand"
            ),
            "bounded_research_retry": "Search only evidence gaps and explicit external obligations",
        }.items():
            nodes_by_id[node_id] = nodes_by_id[node_id].model_copy(update={"title": title})
    if structure_aware:
        node = nodes_by_id["generate_proofreading_candidates"]
        nodes_by_id["generate_proofreading_candidates"] = node.model_copy(update={
            "title": "Generate three independent llm-jp proofreading samples",
        })
    ordered_ids = [
        "prepare_note",
        "prepare_rewrite_context",
        "extract_manuscript_references",
        "decompose_memo",
        "classify_propositions",
        "classify_verification_scopes",
        "review_manuscript_reasoning",
        "formulate_research_question",
        "build_claim_tree",
        "bind_claims_to_propositions",
        "derive_atomic_evidence_obligations",
        "build_source_preservation_map",
        "plan_research_queries",
        "resolve_manuscript_evidence",
        "validate_evidence_support",
        "bounded_research_retry",
        "resolve_reference_metadata",
        "decide_evidence_realization",
        "validate_evidence_ready",
        *(("ratify_research_problem",) if structure_aware else ()),
        "build_argument_graph",
        *(("select_defense_obligations",) if structure_aware else ()),
        "generate_hypotheses",
        "validate_argument_coverage",
        "build_discourse_tree",
        "plan_manuscript_structure",
        "lock_section_contracts",
        *(("start_paragraph_formation",) if structure_aware else ()),
        "resolve_enumeration_context",
        "plan_semantic_paragraphs",
        "plan_term_introductions",
        "draft_rewrite",
        "reconcile_candidate_context",
        "validate_source_preservation",
        "audit_structure",
        *(("classify_audit_findings",) if structure_aware else ()),
        "join_draft_audits",
        "repair_failed_sections",
        "polish_prose",
        "calibrate_manuscript_certainty",
        "validate_draft_coherence",
        "repair_final_coherence",
        *(
            (
                "diagnose_japanese_prose",
                "redraft_japanese_paragraphs",
                "review_japanese_sections",
            ) if structure_aware else ()
        ),
        "extract_proofreading_sentences",
        "generate_proofreading_candidates",
        "preprocess_proofreading_candidates",
        "screen_proofreading_with_luna",
        "rank_proofreading_candidates",
        "select_proofreading_candidates",
        "publish_proofreading_shadow",
        "japanese_surface_quality_gate",
        "reconcile_candidate_context_after_proofreading",
        "render_evidence_footnotes",
        "render_manuscript_references",
        *(("redraft_body_prose",) if structure_aware else ()),
        *(("finalize_section_headings",) if structure_aware else ()),
        "validate_reference_quality",
        "validate_footnoted_references",
        "validate_footnoted_structure",
        "validate_footnoted_coherence",
        "join_submission_validations",
        "freeze_final_candidate",
        "validate_discourse_introductions",
        "audit_final_findings",
        "validate_final_coherence",
        "evaluate_human_revision_requirement",
        "finalize_authoring",
        "submission_package",
        "human_approval",
    ]
    edge_rows = [
        ("prepare_note", "prepare_rewrite_context", "success"),
        ("prepare_note", "extract_manuscript_references", "success"),
        ("prepare_rewrite_context", "decompose_memo", "success"),
        ("decompose_memo", "classify_propositions", "success"),
        ("classify_propositions", "classify_verification_scopes", "success"),
        ("classify_verification_scopes", "review_manuscript_reasoning", "success"),
        ("review_manuscript_reasoning", "formulate_research_question", "success"),
        ("formulate_research_question", "build_claim_tree", "success"),
        ("build_claim_tree", "bind_claims_to_propositions", "success"),
        ("bind_claims_to_propositions", "derive_atomic_evidence_obligations", "success"),
        ("derive_atomic_evidence_obligations", "build_source_preservation_map", "success"),
        ("derive_atomic_evidence_obligations", "plan_research_queries", "success"),
        ("plan_research_queries", "resolve_manuscript_evidence", "success"),
        ("resolve_manuscript_evidence", "validate_evidence_support", "success"),
        ("validate_evidence_support", "bounded_research_retry", "success"),
        ("bounded_research_retry", "resolve_reference_metadata", "success"),
        ("extract_manuscript_references", "resolve_reference_metadata", "success"),
        ("resolve_reference_metadata", "decide_evidence_realization", "success"),
        ("decide_evidence_realization", "validate_evidence_ready", "success"),
        (
            "validate_evidence_ready",
            "ratify_research_problem" if structure_aware else "build_argument_graph",
            "success",
        ),
        *(
            ([("ratify_research_problem", "build_argument_graph", "success")])
            if structure_aware
            else ()
        ),
        (
            "build_argument_graph",
            "select_defense_obligations" if structure_aware else "generate_hypotheses",
            "success",
        ),
        *(
            ([("select_defense_obligations", "generate_hypotheses", "success")])
            if structure_aware
            else ()
        ),
        ("generate_hypotheses", "validate_argument_coverage", "success"),
        ("validate_argument_coverage", "build_discourse_tree", "success"),
        ("build_discourse_tree", "plan_manuscript_structure", "success"),
        ("build_source_preservation_map", "plan_manuscript_structure", "success"),
        ("plan_manuscript_structure", "lock_section_contracts", "success"),
        *(([("lock_section_contracts", "start_paragraph_formation", "success"),
            ("start_paragraph_formation", "resolve_enumeration_context", "success"),
            ("start_paragraph_formation", "plan_semantic_paragraphs", "success")])
          if structure_aware else [
              ("lock_section_contracts", "resolve_enumeration_context", "success"),
              ("lock_section_contracts", "plan_semantic_paragraphs", "success"),
          ]),
        ("plan_semantic_paragraphs", "plan_term_introductions", "success"),
        ("resolve_enumeration_context", "draft_rewrite", "success"),
        ("plan_term_introductions", "draft_rewrite", "success"),
        ("draft_rewrite", "reconcile_candidate_context", "success"),
        ("reconcile_candidate_context", "validate_source_preservation", "success"),
        ("reconcile_candidate_context", "audit_structure", "success"),
        ("validate_source_preservation", "join_draft_audits", "always"),
        *(
            [
                ("audit_structure", "classify_audit_findings", "success"),
                ("classify_audit_findings", "join_draft_audits", "always"),
            ]
            if structure_aware
            else [("audit_structure", "join_draft_audits", "always")]
        ),
        ("join_draft_audits", "repair_failed_sections", "failure"),
        ("join_draft_audits", "polish_prose", "success"),
        ("repair_failed_sections", "reconcile_candidate_context", "success"),
        ("polish_prose", "calibrate_manuscript_certainty", "success"),
        ("calibrate_manuscript_certainty", "validate_draft_coherence", "success"),
        (
            "validate_draft_coherence",
            "diagnose_japanese_prose" if structure_aware else "extract_proofreading_sentences",
            "success",
        ),
        (
            "validate_draft_coherence",
            "diagnose_japanese_prose" if structure_aware else "extract_proofreading_sentences",
            "conditional_success",
        ),
        ("validate_draft_coherence", "repair_final_coherence", "failure"),
        *(
            [
                ("diagnose_japanese_prose", "redraft_japanese_paragraphs", "success"),
                ("redraft_japanese_paragraphs", "review_japanese_sections", "success"),
                ("review_japanese_sections", "redraft_japanese_paragraphs", "failure"),
                ("review_japanese_sections", "extract_proofreading_sentences", "success"),
            ] if structure_aware else []
        ),
        ("repair_final_coherence", "reconcile_candidate_context", "success"),
        ("repair_final_coherence", "reconcile_candidate_context", "conditional_success"),
        (
            "reconcile_candidate_context",
            "render_evidence_footnotes",
            "conditional_success",
        ),
        ("extract_proofreading_sentences", "generate_proofreading_candidates", "success"),
        ("generate_proofreading_candidates", "preprocess_proofreading_candidates", "success"),
        ("preprocess_proofreading_candidates", "screen_proofreading_with_luna", "success"),
        ("screen_proofreading_with_luna", "rank_proofreading_candidates", "success"),
        ("rank_proofreading_candidates", "select_proofreading_candidates", "success"),
        ("select_proofreading_candidates", "publish_proofreading_shadow", "success"),
        ("publish_proofreading_shadow", "japanese_surface_quality_gate", "success"),
        (
            "japanese_surface_quality_gate",
            "reconcile_candidate_context_after_proofreading",
            "success",
        ),
        ("reconcile_candidate_context_after_proofreading", "render_evidence_footnotes", "success"),
        ("render_evidence_footnotes", "render_manuscript_references", "success"),
        *(([("render_manuscript_references", "redraft_body_prose", "success")])
          if structure_aware else []),
        *(([("redraft_body_prose", "finalize_section_headings", "success")])
          if structure_aware else []),
        *(("finalize_section_headings" if structure_aware else "render_manuscript_references",
           target, "success") for target in (
            "validate_reference_quality", "validate_footnoted_references",
            "validate_footnoted_structure", "validate_footnoted_coherence",
        )),
        ("validate_reference_quality", "join_submission_validations", "always"),
        ("validate_footnoted_references", "join_submission_validations", "always"),
        ("validate_footnoted_structure", "join_submission_validations", "always"),
        ("validate_footnoted_coherence", "join_submission_validations", "always"),
        ("join_submission_validations", "freeze_final_candidate", "success"),
        (
            "join_submission_validations",
            "freeze_final_candidate",
            "conditional_success",
        ),
        ("join_submission_validations", "repair_final_coherence", "failure"),
        ("freeze_final_candidate", "validate_discourse_introductions", "success"),
        ("validate_discourse_introductions", "audit_final_findings", "always"),
        ("audit_final_findings", "validate_final_coherence", "success"),
        ("validate_final_coherence", "evaluate_human_revision_requirement", "success"),
        ("validate_final_coherence", "evaluate_human_revision_requirement", "conditional_success"),
        ("evaluate_human_revision_requirement", "finalize_authoring", "success"),
        ("finalize_authoring", "submission_package", "success"),
        ("submission_package", "human_approval", "success"),
    ]
    edges = [
        EdgeSpec(source=source, target=target, condition=condition)
        for source, target, condition in edge_rows
    ]
    sequence = [
        nodes_by_id[node_id].model_copy(
            update={
                "input_json_contract": f"authoring-context-v{version}",
                "output_json_contract": f"authoring-context-v{version}",
            }
        )
        for node_id in ordered_ids
    ]
    return WorkflowDefinition(
        name="authoring" if version == "30" else "manuscript_generator",
        version=version,
        title="Manuscript Generator",
        entry_node="prepare_note",
        nodes=sequence,
        edges=edges,
    )


def authoring_workflow_v30() -> WorkflowDefinition:
    """Keep the exact V30 node graph available for historical Run resume."""

    return authoring_workflow(version="30")


def authoring_workflow_v31_legacy() -> WorkflowDefinition:
    """Resume V31 runs persisted before the workflow identity changed."""

    return authoring_workflow(version="31").model_copy(update={"name": "authoring"})
