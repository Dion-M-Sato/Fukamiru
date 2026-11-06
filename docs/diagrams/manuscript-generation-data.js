// Generated from the current executable manuscript workflow.
// Do not edit by hand. Regenerate with scripts/generate_manuscript_graph.py.
window.MANUSCRIPT_GRAPH_V32 = {
  "name": "manuscript_generator",
  "version": "32",
  "nodes": [
    {
      "id": "prepare_note",
      "title": "Register source note and target paper",
      "handler": "authoring.prepare_note",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "prepare_rewrite_context",
      "title": "Build source lineage context",
      "handler": "authoring.prepare_rewrite_context",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "extract_manuscript_references",
      "title": "Extract source references",
      "handler": "authoring.extract_manuscript_references",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "decompose_memo",
      "title": "Decompose atomic propositions",
      "handler": "authoring.decompose_memo",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "classify_propositions",
      "title": "Classify proposition types",
      "handler": "authoring.classify_propositions",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "classify_verification_scopes",
      "title": "Classify atomic verification scopes",
      "handler": "authoring.classify_verification_scopes",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "review_manuscript_reasoning",
      "title": "Inventory reasoning assets",
      "handler": "authoring.review_manuscript_reasoning",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "formulate_research_question",
      "title": "Fix one research question",
      "handler": "authoring.formulate_research_question",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "build_claim_tree",
      "title": "Build claim hierarchy",
      "handler": "authoring.build_claim_tree",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "bind_claims_to_propositions",
      "title": "Bind claim wrappers to atomic propositions",
      "handler": "authoring.bind_claims_to_propositions",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "derive_atomic_evidence_obligations",
      "title": "Derive atomic evidence obligations",
      "handler": "authoring.derive_atomic_evidence_obligations",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "build_source_preservation_map",
      "title": "Map source propositions to claims",
      "handler": "authoring.build_source_preservation_map",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "plan_research_queries",
      "title": "Plan compact multilingual literature queries",
      "handler": "manuscript_rewrite.plan_research_queries",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "resolve_manuscript_evidence",
      "title": "Reuse local RAG passages before external literature discovery",
      "handler": "manuscript_rewrite.resolve_evidence",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_evidence_support",
      "title": "Review passages and read missing source context on demand",
      "handler": "manuscript_rewrite.validate_evidence_support",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "bounded_research_retry",
      "title": "Search only evidence gaps and explicit external obligations",
      "handler": "authoring.bounded_research_retry",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "resolve_reference_metadata",
      "title": "Resolve and deduplicate reference metadata",
      "handler": "manuscript_rewrite.resolve_reference_metadata",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [
        "extract_manuscript_references",
        "bounded_research_retry"
      ],
      "join_generation_source": "prepare_note"
    },
    {
      "id": "decide_evidence_realization",
      "title": "Lock assertion, attribution, qualification, or omission",
      "handler": "authoring.decide_evidence_realization",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_evidence_ready",
      "title": "Validate atomic evidence obligations",
      "handler": "authoring.validate_evidence_ready",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "ratify_research_problem",
      "title": "Ground the research problem",
      "handler": "authoring.ratify_research_problem",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "build_argument_graph",
      "title": "Build claim-evidence-warrant graph",
      "handler": "authoring.build_argument_graph",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "select_defense_obligations",
      "title": "Select material defenses",
      "handler": "authoring.select_defense_obligations",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "generate_hypotheses",
      "title": "Generate bounded hypotheses",
      "handler": "authoring.generate_hypotheses",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_argument_coverage",
      "title": "Validate argument-role coverage",
      "handler": "authoring.validate_argument_coverage",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "build_discourse_tree",
      "title": "Build rhetorical discourse tree",
      "handler": "authoring.build_discourse_tree",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "plan_manuscript_structure",
      "title": "Plan variable manuscript structure",
      "handler": "authoring.plan_manuscript_structure",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [
        "build_source_preservation_map",
        "build_discourse_tree"
      ],
      "join_generation_source": "derive_atomic_evidence_obligations"
    },
    {
      "id": "lock_section_contracts",
      "title": "Lock section contracts",
      "handler": "authoring.lock_section_contracts",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "start_paragraph_formation",
      "title": "Begin a fresh paragraph formation generation with locked claims",
      "handler": "authoring.start_paragraph_formation",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "resolve_enumeration_context",
      "title": "Identify analytical focus behind candidate lists",
      "handler": "authoring.resolve_enumeration_context",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "plan_semantic_paragraphs",
      "title": "Plan paragraphs around analytical development",
      "handler": "authoring.plan_semantic_paragraphs",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "plan_term_introductions",
      "title": "Plan first use of required terms",
      "handler": "authoring.plan_term_introductions",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "draft_rewrite",
      "title": "Draft analytical prose and record model inferences",
      "handler": "authoring.draft_rewrite",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "human_review": "on_defect",
      "join_from": [
        "resolve_enumeration_context",
        "plan_term_introductions"
      ],
      "join_generation_source": "start_paragraph_formation"
    },
    {
      "id": "reconcile_candidate_context",
      "title": "Reconcile canonical draft context",
      "handler": "authoring.reconcile_candidate_context",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_source_preservation",
      "title": "Validate source preservation",
      "handler": "authoring.validate_source_preservation",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "audit_structure",
      "title": "Audit claim and discourse integrity",
      "handler": "authoring.audit_structure",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "classify_audit_findings",
      "title": "Classify audit findings deterministically",
      "handler": "authoring.classify_audit_findings",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "join_draft_audits",
      "title": "Join source and structural audits",
      "handler": "authoring.join_draft_audits",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [
        "validate_source_preservation",
        "classify_audit_findings"
      ],
      "join_generation_source": "reconcile_candidate_context"
    },
    {
      "id": "repair_failed_sections",
      "title": "Repair failed sections",
      "handler": "authoring.repair_failed_sections",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "polish_prose",
      "title": "Recompose stalled prose without changing claims",
      "handler": "authoring.polish_prose",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "calibrate_manuscript_certainty",
      "title": "根拠の確実度に応じて解釈・仮説の断定を調整",
      "handler": "manuscript_rewrite.calibrate_certainty",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_draft_coherence",
      "title": "Validate discourse coherence",
      "handler": "authoring.validate_draft_coherence",
      "resource_lane": "codex_remote",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "repair_final_coherence",
      "title": "最終原稿の意味上の整合性を局所修復",
      "handler": "manuscript_rewrite.repair_final_coherence_v27",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "diagnose_japanese_prose",
      "title": "Diagnose Japanese prose and analytical progress",
      "handler": "authoring.diagnose_japanese_prose",
      "resource_lane": "codex_remote",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "redraft_japanese_paragraphs",
      "title": "Redraft Japanese paragraphs with section context",
      "handler": "authoring.redraft_japanese_paragraphs",
      "resource_lane": "codex_remote",
      "max_attempts": 5,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "review_japanese_sections",
      "title": "Review section analysis, Japanese flow and argument preservation",
      "handler": "authoring.review_japanese_sections",
      "resource_lane": "codex_remote",
      "max_attempts": 5,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "extract_proofreading_sentences",
      "title": "Extract stable Japanese sentences and work-title registry",
      "handler": "manuscript_rewrite.extract_proofreading_sentences_v29",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "generate_proofreading_candidates",
      "title": "Generate three independent llm-jp proofreading samples",
      "handler": "manuscript_rewrite.generate_proofreading_candidates_v29",
      "resource_lane": "local_llm_gpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "preprocess_proofreading_candidates",
      "title": "Normalize, deduplicate, protect, lint and create raw diffs",
      "handler": "manuscript_rewrite.preprocess_proofreading_candidates_v29",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "screen_proofreading_with_luna",
      "title": "Screen eligible changes with GPT-5.6 Luna",
      "handler": "manuscript_rewrite.screen_proofreading_with_luna_v29",
      "resource_lane": "codex_remote",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "rank_proofreading_candidates",
      "title": "Select clear Japanese improvements against the original",
      "handler": "authoring.rank_proofreading_candidates",
      "resource_lane": "codex_remote",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "select_proofreading_candidates",
      "title": "Separate clean safety status from candidate selection",
      "handler": "manuscript_rewrite.select_proofreading_candidates_v29",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "publish_proofreading_shadow",
      "title": "Publish safe candidate status and shadow previews",
      "handler": "manuscript_rewrite.publish_proofreading_shadow_v29",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "japanese_surface_quality_gate",
      "title": "Validate applied Japanese surface",
      "handler": "authoring.japanese_surface_quality_gate",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "reconcile_candidate_context_after_proofreading",
      "title": "Reconcile post-proofreading canonical candidate",
      "handler": "authoring.reconcile_candidate_context_after_proofreading",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "render_evidence_footnotes",
      "title": "Render evidence-linked footnotes and reconcile the final bibliography",
      "handler": "manuscript_rewrite.render_evidence_footnotes_v29",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "render_manuscript_references",
      "title": "Render references in the user's bibliography house style",
      "handler": "manuscript_rewrite.render_references",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "redraft_body_prose",
      "title": "Edit Japanese prose once per section within fixed HTML text slots",
      "handler": "authoring.redraft_body_prose",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "finalize_section_headings",
      "title": "Edit final Japanese section headings once against finished prose",
      "handler": "authoring.finalize_section_headings",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_reference_quality",
      "title": "Validate bibliography completeness, provenance and idempotency",
      "handler": "manuscript_rewrite.reference_quality_gate",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_footnoted_references",
      "title": "Validate the bibliography reconciled from evidence footnotes",
      "handler": "manuscript_rewrite.reference_quality_gate",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_footnoted_structure",
      "title": "Validate canonical structure after footnote rendering",
      "handler": "manuscript_rewrite.validate_rendered_structure",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_footnoted_coherence",
      "title": "Read-only re-audit of the footnoted final candidate",
      "handler": "manuscript_rewrite.validate_footnoted_coherence_v29",
      "resource_lane": "codex_remote",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "join_submission_validations",
      "title": "Join bibliography, reference, and deterministic structure checks",
      "handler": "authoring.join_submission_validations",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [
        "validate_reference_quality",
        "validate_footnoted_references",
        "validate_footnoted_structure",
        "validate_footnoted_coherence"
      ],
      "join_generation_source": "finalize_section_headings"
    },
    {
      "id": "freeze_final_candidate",
      "title": "最終候補とハッシュを固定",
      "handler": "manuscript_rewrite.freeze_final_candidate",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_discourse_introductions",
      "title": "初出語と引用目的の最終本文対応を検証",
      "handler": "manuscript_rewrite.validate_terms_v26",
      "resource_lane": "codex_remote",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "audit_final_findings",
      "title": "Audit final findings after Japanese and discourse validation",
      "handler": "manuscript_rewrite.audit_final_findings",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "validate_final_coherence",
      "title": "Verify final coherence after Japanese surface restoration",
      "handler": "manuscript_rewrite.final_judgement_v26",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 2,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "evaluate_human_revision_requirement",
      "title": "Evaluate human revision need",
      "handler": "authoring.evaluate_human_revision_requirement",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "finalize_authoring",
      "title": "Persist the frozen manuscript proposal",
      "handler": "authoring.finalize_authoring",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "submission_package",
      "title": "Write manuscript and audit manifest",
      "handler": "authoring.submission_package",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "human_approval",
      "title": "Request approval only when required",
      "handler": "authoring.human_approval",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    }
  ],
  "edges": [
    {
      "from": "prepare_note",
      "to": "prepare_rewrite_context",
      "condition": "success"
    },
    {
      "from": "prepare_note",
      "to": "extract_manuscript_references",
      "condition": "success"
    },
    {
      "from": "prepare_rewrite_context",
      "to": "decompose_memo",
      "condition": "success"
    },
    {
      "from": "decompose_memo",
      "to": "classify_propositions",
      "condition": "success"
    },
    {
      "from": "classify_propositions",
      "to": "classify_verification_scopes",
      "condition": "success"
    },
    {
      "from": "classify_verification_scopes",
      "to": "review_manuscript_reasoning",
      "condition": "success"
    },
    {
      "from": "review_manuscript_reasoning",
      "to": "formulate_research_question",
      "condition": "success"
    },
    {
      "from": "formulate_research_question",
      "to": "build_claim_tree",
      "condition": "success"
    },
    {
      "from": "build_claim_tree",
      "to": "bind_claims_to_propositions",
      "condition": "success"
    },
    {
      "from": "bind_claims_to_propositions",
      "to": "derive_atomic_evidence_obligations",
      "condition": "success"
    },
    {
      "from": "derive_atomic_evidence_obligations",
      "to": "build_source_preservation_map",
      "condition": "success"
    },
    {
      "from": "derive_atomic_evidence_obligations",
      "to": "plan_research_queries",
      "condition": "success"
    },
    {
      "from": "plan_research_queries",
      "to": "resolve_manuscript_evidence",
      "condition": "success"
    },
    {
      "from": "resolve_manuscript_evidence",
      "to": "validate_evidence_support",
      "condition": "success"
    },
    {
      "from": "validate_evidence_support",
      "to": "bounded_research_retry",
      "condition": "success"
    },
    {
      "from": "bounded_research_retry",
      "to": "resolve_reference_metadata",
      "condition": "success"
    },
    {
      "from": "extract_manuscript_references",
      "to": "resolve_reference_metadata",
      "condition": "success"
    },
    {
      "from": "resolve_reference_metadata",
      "to": "decide_evidence_realization",
      "condition": "success"
    },
    {
      "from": "decide_evidence_realization",
      "to": "validate_evidence_ready",
      "condition": "success"
    },
    {
      "from": "validate_evidence_ready",
      "to": "ratify_research_problem",
      "condition": "success"
    },
    {
      "from": "ratify_research_problem",
      "to": "build_argument_graph",
      "condition": "success"
    },
    {
      "from": "build_argument_graph",
      "to": "select_defense_obligations",
      "condition": "success"
    },
    {
      "from": "select_defense_obligations",
      "to": "generate_hypotheses",
      "condition": "success"
    },
    {
      "from": "generate_hypotheses",
      "to": "validate_argument_coverage",
      "condition": "success"
    },
    {
      "from": "validate_argument_coverage",
      "to": "build_discourse_tree",
      "condition": "success"
    },
    {
      "from": "build_discourse_tree",
      "to": "plan_manuscript_structure",
      "condition": "success"
    },
    {
      "from": "build_source_preservation_map",
      "to": "plan_manuscript_structure",
      "condition": "success"
    },
    {
      "from": "plan_manuscript_structure",
      "to": "lock_section_contracts",
      "condition": "success"
    },
    {
      "from": "lock_section_contracts",
      "to": "start_paragraph_formation",
      "condition": "success"
    },
    {
      "from": "start_paragraph_formation",
      "to": "resolve_enumeration_context",
      "condition": "success"
    },
    {
      "from": "start_paragraph_formation",
      "to": "plan_semantic_paragraphs",
      "condition": "success"
    },
    {
      "from": "plan_semantic_paragraphs",
      "to": "plan_term_introductions",
      "condition": "success"
    },
    {
      "from": "resolve_enumeration_context",
      "to": "draft_rewrite",
      "condition": "success"
    },
    {
      "from": "plan_term_introductions",
      "to": "draft_rewrite",
      "condition": "success"
    },
    {
      "from": "draft_rewrite",
      "to": "reconcile_candidate_context",
      "condition": "success"
    },
    {
      "from": "reconcile_candidate_context",
      "to": "validate_source_preservation",
      "condition": "success"
    },
    {
      "from": "reconcile_candidate_context",
      "to": "audit_structure",
      "condition": "success"
    },
    {
      "from": "validate_source_preservation",
      "to": "join_draft_audits",
      "condition": "always"
    },
    {
      "from": "audit_structure",
      "to": "classify_audit_findings",
      "condition": "success"
    },
    {
      "from": "classify_audit_findings",
      "to": "join_draft_audits",
      "condition": "always"
    },
    {
      "from": "join_draft_audits",
      "to": "repair_failed_sections",
      "condition": "failure"
    },
    {
      "from": "join_draft_audits",
      "to": "polish_prose",
      "condition": "success"
    },
    {
      "from": "repair_failed_sections",
      "to": "reconcile_candidate_context",
      "condition": "success"
    },
    {
      "from": "polish_prose",
      "to": "calibrate_manuscript_certainty",
      "condition": "success"
    },
    {
      "from": "calibrate_manuscript_certainty",
      "to": "validate_draft_coherence",
      "condition": "success"
    },
    {
      "from": "validate_draft_coherence",
      "to": "diagnose_japanese_prose",
      "condition": "success"
    },
    {
      "from": "validate_draft_coherence",
      "to": "diagnose_japanese_prose",
      "condition": "conditional_success"
    },
    {
      "from": "validate_draft_coherence",
      "to": "repair_final_coherence",
      "condition": "failure"
    },
    {
      "from": "diagnose_japanese_prose",
      "to": "redraft_japanese_paragraphs",
      "condition": "success"
    },
    {
      "from": "redraft_japanese_paragraphs",
      "to": "review_japanese_sections",
      "condition": "success"
    },
    {
      "from": "review_japanese_sections",
      "to": "redraft_japanese_paragraphs",
      "condition": "failure"
    },
    {
      "from": "review_japanese_sections",
      "to": "extract_proofreading_sentences",
      "condition": "success"
    },
    {
      "from": "repair_final_coherence",
      "to": "reconcile_candidate_context",
      "condition": "success"
    },
    {
      "from": "repair_final_coherence",
      "to": "reconcile_candidate_context",
      "condition": "conditional_success"
    },
    {
      "from": "reconcile_candidate_context",
      "to": "render_evidence_footnotes",
      "condition": "conditional_success"
    },
    {
      "from": "extract_proofreading_sentences",
      "to": "generate_proofreading_candidates",
      "condition": "success"
    },
    {
      "from": "generate_proofreading_candidates",
      "to": "preprocess_proofreading_candidates",
      "condition": "success"
    },
    {
      "from": "preprocess_proofreading_candidates",
      "to": "screen_proofreading_with_luna",
      "condition": "success"
    },
    {
      "from": "screen_proofreading_with_luna",
      "to": "rank_proofreading_candidates",
      "condition": "success"
    },
    {
      "from": "rank_proofreading_candidates",
      "to": "select_proofreading_candidates",
      "condition": "success"
    },
    {
      "from": "select_proofreading_candidates",
      "to": "publish_proofreading_shadow",
      "condition": "success"
    },
    {
      "from": "publish_proofreading_shadow",
      "to": "japanese_surface_quality_gate",
      "condition": "success"
    },
    {
      "from": "japanese_surface_quality_gate",
      "to": "reconcile_candidate_context_after_proofreading",
      "condition": "success"
    },
    {
      "from": "reconcile_candidate_context_after_proofreading",
      "to": "render_evidence_footnotes",
      "condition": "success"
    },
    {
      "from": "render_evidence_footnotes",
      "to": "render_manuscript_references",
      "condition": "success"
    },
    {
      "from": "render_manuscript_references",
      "to": "redraft_body_prose",
      "condition": "success"
    },
    {
      "from": "redraft_body_prose",
      "to": "finalize_section_headings",
      "condition": "success"
    },
    {
      "from": "finalize_section_headings",
      "to": "validate_reference_quality",
      "condition": "success"
    },
    {
      "from": "finalize_section_headings",
      "to": "validate_footnoted_references",
      "condition": "success"
    },
    {
      "from": "finalize_section_headings",
      "to": "validate_footnoted_structure",
      "condition": "success"
    },
    {
      "from": "finalize_section_headings",
      "to": "validate_footnoted_coherence",
      "condition": "success"
    },
    {
      "from": "validate_reference_quality",
      "to": "join_submission_validations",
      "condition": "always"
    },
    {
      "from": "validate_footnoted_references",
      "to": "join_submission_validations",
      "condition": "always"
    },
    {
      "from": "validate_footnoted_structure",
      "to": "join_submission_validations",
      "condition": "always"
    },
    {
      "from": "validate_footnoted_coherence",
      "to": "join_submission_validations",
      "condition": "always"
    },
    {
      "from": "join_submission_validations",
      "to": "freeze_final_candidate",
      "condition": "success"
    },
    {
      "from": "join_submission_validations",
      "to": "freeze_final_candidate",
      "condition": "conditional_success"
    },
    {
      "from": "join_submission_validations",
      "to": "repair_final_coherence",
      "condition": "failure"
    },
    {
      "from": "freeze_final_candidate",
      "to": "validate_discourse_introductions",
      "condition": "success"
    },
    {
      "from": "validate_discourse_introductions",
      "to": "audit_final_findings",
      "condition": "always"
    },
    {
      "from": "audit_final_findings",
      "to": "validate_final_coherence",
      "condition": "success"
    },
    {
      "from": "validate_final_coherence",
      "to": "evaluate_human_revision_requirement",
      "condition": "success"
    },
    {
      "from": "validate_final_coherence",
      "to": "evaluate_human_revision_requirement",
      "condition": "conditional_success"
    },
    {
      "from": "evaluate_human_revision_requirement",
      "to": "finalize_authoring",
      "condition": "success"
    },
    {
      "from": "finalize_authoring",
      "to": "submission_package",
      "condition": "success"
    },
    {
      "from": "submission_package",
      "to": "human_approval",
      "condition": "success"
    }
  ],
  "paragraph_argument_move_catalog": [
    {
      "id": "object_context",
      "label": "研究対象と背景",
      "group": "introduction",
      "description": "研究対象を示し、必要な社会的・歴史的背景に位置づける。"
    },
    {
      "id": "prior_research_position",
      "label": "先行研究の到達点",
      "group": "introduction",
      "description": "出典で確認できる先行研究の議論や到達点を整理する。"
    },
    {
      "id": "problematisation",
      "label": "問題化・研究上の問いの必要",
      "group": "introduction",
      "description": "既存の説明や資料から何が問題となるのかを具体的に示す。"
    },
    {
      "id": "research_question",
      "label": "研究の問い",
      "group": "introduction",
      "description": "論文全体が答える研究上の問いを明確に提示する。"
    },
    {
      "id": "provisional_answer",
      "label": "暫定的な答え・中心命題",
      "group": "introduction",
      "description": "問いへの暫定的な答え、または論文の中心命題を先に示す。"
    },
    {
      "id": "scope_and_materials",
      "label": "対象・資料・範囲",
      "group": "introduction",
      "description": "何を対象にし、どの資料と範囲を扱うかを定める。"
    },
    {
      "id": "method_or_analytic_axis",
      "label": "方法・分析軸",
      "group": "introduction",
      "description": "資料をどの方法・観点・分析軸から読むかを示す。"
    },
    {
      "id": "key_concept_or_distinction",
      "label": "中核概念・分析上の区別",
      "group": "introduction",
      "description": "論証に必要な概念や、混同しないための区別を定義する。"
    },
    {
      "id": "contribution_or_implication",
      "label": "学術的貢献・含意",
      "group": "introduction",
      "description": "問いに答えることで研究上どの理解を更新するかを示す。"
    },
    {
      "id": "roadmap",
      "label": "論文構成の予告",
      "group": "introduction",
      "description": "各節が何を明らかにするかを論証の順序に沿って案内する。"
    },
    {
      "id": "local_claim",
      "label": "節内の主張",
      "group": "body",
      "description": "段落または節で立証する個別の主張を提示する。"
    },
    {
      "id": "evidence_or_source_detail",
      "label": "根拠・資料の具体",
      "group": "body",
      "description": "主張を支える資料、記述、データ、出典の具体を示す。"
    },
    {
      "id": "concrete_case_or_example",
      "label": "具体例・事例",
      "group": "body",
      "description": "論点を具体化する事例や例を提示し、その位置づけを明らかにする。"
    },
    {
      "id": "case_analysis_or_close_reading",
      "label": "事例分析・精読",
      "group": "body",
      "description": "事例や資料の細部を読み、論証に関わる意味を説明する。"
    },
    {
      "id": "warrant_or_explanatory_mechanism",
      "label": "推論根拠・説明メカニズム",
      "group": "body",
      "description": "根拠から主張が導かれる理由、関係、作用の仕組みを説明する。"
    },
    {
      "id": "comparison_or_contrast",
      "label": "比較・対照",
      "group": "body",
      "description": "比較対象と比較軸を明確にし、共通点や差異の意味を論じる。"
    },
    {
      "id": "parallel_branch_contribution",
      "label": "複数枝の並置・未解決関係",
      "group": "body",
      "description": "同じ節に含まれる複数の論点枝の寄与を区別する。資料に関係の直接記述がなくても、前後の議論から筆者の解釈として関係を論じられる場合は本文で示す。"
    },
    {
      "id": "cross_branch_synthesis",
      "label": "根拠に基づく枝間統合",
      "group": "body",
      "description": "複数の論点枝を結ぶ資料上の関係、または論理を示した筆者の解釈から、節の結論を導く。外部事実や出典の主張を創作しない。"
    },
    {
      "id": "alternative_or_counterargument",
      "label": "対抗説明・反論",
      "group": "body",
      "description": "主張を脅かす根拠ある別解釈や反論を提示する。"
    },
    {
      "id": "response_or_rebuttal",
      "label": "応答・再反論",
      "group": "body",
      "description": "対抗説明・反論を検討し、主張を維持・修正する理由を示す。"
    },
    {
      "id": "qualification_or_scope",
      "label": "限定・適用条件",
      "group": "body",
      "description": "主張が成り立つ条件や、説明できる範囲を具体的に限定する。"
    },
    {
      "id": "intermediate_synthesis_or_bridge",
      "label": "小括・次段への接続",
      "group": "body",
      "description": "ここまでの分析から得た結果をまとめ、次の論点へつなぐ。"
    },
    {
      "id": "final_answer_to_research_question",
      "label": "研究の問いへの最終回答",
      "group": "conclusion",
      "description": "確立した本文の成果に基づいて、研究の問いに最終回答する。"
    },
    {
      "id": "synthesis_of_section_outputs",
      "label": "各節の成果の統合",
      "group": "conclusion",
      "description": "各節の結果を言い換えるだけでなく、全体の論証として統合する。"
    },
    {
      "id": "limitation_or_open_question",
      "label": "限界・未解決の問い",
      "group": "conclusion",
      "description": "証拠や範囲に基づく限界と、残る問いを具体的に示す。"
    }
  ],
  "diagram_version": "32",
  "source_ingestion_workflow": {
    "name": "research_source_ingestion",
    "version": "1",
    "nodes": [
      {
        "id": "register_asset",
        "title": "Register immutable SourceAsset",
        "handler": "research.register_source",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "extract_text",
        "title": "Extract text",
        "handler": "ingestion.extract_text",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "audit_ocr",
        "title": "Audit OCR integrity and page continuity",
        "handler": "ingestion.audit_ocr",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "infer_structure",
        "title": "Infer DocumentUnit and SourceSpan structure",
        "handler": "ingestion.infer_structure",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "build_chunks",
        "title": "Build tokenizer-measured retrieval chunks",
        "handler": "ingestion.build_chunks",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "index_chunks",
        "title": "Publish FTS5 retrieval chunks",
        "handler": "ingestion.index_chunks",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "publish_research_index",
        "title": "Publish semantic index for useful research sources",
        "handler": "research.publish_index",
        "resource_lane": "embedding_gpu",
        "max_attempts": 2,
        "join_from": [],
        "join_generation_source": null
      },
      {
        "id": "run_ocr",
        "title": "Run Mistral or NDLOCR-Lite OCR",
        "handler": "ingestion.run_ocr",
        "resource_lane": "ingestion_cpu",
        "max_attempts": 1,
        "join_from": [],
        "join_generation_source": null
      }
    ],
    "edges": [
      {
        "from": "register_asset",
        "to": "extract_text",
        "condition": "success"
      },
      {
        "from": "extract_text",
        "to": "audit_ocr",
        "condition": "success"
      },
      {
        "from": "audit_ocr",
        "to": "infer_structure",
        "condition": "success"
      },
      {
        "from": "infer_structure",
        "to": "build_chunks",
        "condition": "success"
      },
      {
        "from": "build_chunks",
        "to": "index_chunks",
        "condition": "success"
      },
      {
        "from": "index_chunks",
        "to": "publish_research_index",
        "condition": "success"
      },
      {
        "from": "build_chunks",
        "to": "run_ocr",
        "condition": "conditional_success"
      },
      {
        "from": "run_ocr",
        "to": "register_asset",
        "condition": "success"
      }
    ]
  },
  "research_policy": {
    "plan_research_queries": {
      "summary": "命題を短い日本語・英語のキーワード検索へ変換し、説明文や長すぎる検索語を除外します。NDLでは日本語、国際DBでは英語を優先し、原子命題自体は固定します。原メモの段落・注記・自己引用を渡し、書名と引用された概念を別々の検索語として保持します。",
      "input": "原子命題、原メモの段落・注記、書名・概念の手掛かり、固定した検証対象、出力言語",
      "output": "1〜6件の短い検索語、検証対象、資料種別、計画状態"
    },
    "resolve_manuscript_evidence": {
      "summary": "RAG有効時は生成済みの本文と索引を先に検索し、初回の外部検索は保留します。OCRや埋め込みは再生成しません。検索結果と成功した外部検索は実行単位でSQLiteに保存し、同じ検索を再実行しません。索引・資料・書誌情報が更新された場合はRAG検索を更新します。既知の書名確認は既存の速い経路を維持します。明示書名からローカル資料候補を探し、その資料内で概念と前後本文を読みます。巻号・叢書情報が付いた登録書名も照合し、巻や資料IDは混合しません。表記差による書名一致は候補の同定として記録し、書誌の同一性確認とは区別します。RAG無効時または追加調査では、OpenAlex・Crossref・NDL・Semantic Scholarの公式APIで未検索の検索語を上限付きで調査します。返却件数だけでは打ち切りません。429時は完了済み検索と未完了検索をSQLiteへ保存し、取得済み資料の検証を進め、追加調査でRetry-After後に未完了分を再開します。資料が一件も読めない場合は遅延再試行し、3回後も復旧しない場合は確認待ちにします。",
      "input": "検索計画、既存の検索チェックポイント、ローカルRAG・オンライン検索の設定",
      "output": "検索候補、資料種別、抄録・本文抜粋とページ位置、DB応答、再開用チェックポイント"
    },
    "validate_evidence_support": {
      "summary": "原メモの文脈と上限12件の候補を読み、命題への支持を検査します。RAG本文も語句一致だけでは排除せず、資料IDとページを保持します。PDFの文字順を保って書籍内の説明ページを探し、前後2～3ページを連続して読みます。再抽出した本文には原本ハッシュ・PDFページ・抽出ハッシュを付け、古い文字位置を流用しません。文字崩れのある日本語ページだけ既定3ページ・60秒以内でローカルOCRし、長い縦列を分割します。OCR修復文や文字崩れの疑いが残る範囲は画像確認待ちとし、根拠への自動昇格を拒否します。部分的な引用と不足事項を記録し、読解が必要と判定された資料だけ本文を追加取得します。引用が返らない未解決候補も読解対象とし、索引だけで資料を棄却しません。並列実行前に命題別の入力推計トークン枠を確保し、重要命題・明示的引用を優先します。初回検査と追加読解の枠を分け、既定で異なる3資料・追加2巡・命題180秒の受付期限を用います。不足事項に応じて同じ資料の概念と前後本文を6000字から12000字へ広げ、定義・条件・適用を分けます。解決、新情報なし、取得不可、入力・回数・時間予算を別々の理由として記録し、予算終了は未解決のまま後段へ渡します。既定の全命題合計は入力推計120000トークンで、再調査でも同じ配分と消費記録を引き継ぎます。出力・OCR課金はこの推計に含みません。300字条件は使いません。DOI・ISBNで同定できるRAG本文は再利用し、外部PDFも実行内で共有します。URLと書誌の一致も照合し、版の異なる資料は混同しません。全文を検索対象にし、時間・容量で止まった読解は未完了と記録します。暗号化されていても表示可能なPDFはRAG生成と同じ全ページ画像化・言語判定・NDLOCR-Lite／Mistral OCRで読解します。ローカル修復文の画像未確認状態は保持します。支持・推論の橋渡し・反証として引用照合できた資料だけ、別の永続Runへ引き継ぎます。そのRunは資料登録→抽出→OCR監査→構造化→Chunk生成→FTS→意味索引を実行し、原稿生成は完了を待ちません。PDFハッシュで重複登録を防ぎ、OCRは再実行しません。引用文が実際の抜粋に存在する場合だけ根拠に採用し、書誌情報だけでは支持としません。本文検査後に関連引用のない語句一致だけの候補はサジェストからも除外します。",
      "input": "固定した原子命題、抄録・本文付き候補と取得位置",
      "output": "引用照合済み根拠、未確認の義務、部分的な引用と不足事項、共有本文、命題別の消費記録・停止理由"
    },
    "bounded_research_retry": {
      "summary": "未解決の必須義務・対抗仮説を優先し、調査対象に選ばれたpreferred命題も含めて最大4件を一度だけ追加調査します。RAGで確認済みの義務は外部調査を省き、明示的な先行研究追加・対抗説・論文指定は残します。不足事項と読んだ資料の引用から検索語を更新し、初回RAGで使った未送信の検索語も利用します。短い検索語・同義語・書籍・一次資料を区別して公式DBを再調査し、なお不足するテーマだけRustXNG v0.30.0（Standard）の一般Web検索で日本語研究資料と事実の出典を補います。語句一致やスニペットだけを根拠にせず、取得した内容を同じ根拠検査へ渡します。未読書籍は関連性条件を満たせば機械判別参考文献の候補として保持します。同じ引用照合を行い、執筆には取得済み資料を引き継ぎます。未完了のDB検索も引き継ぎます。取得済み資料がある場合は一度待機して再開し、なお失敗するDBは未完了と明記して他の資料の検証を進めます。",
      "input": "未解決の証拠義務、既存の検索計画・調査結果",
      "output": "再評価済みの根拠と未確認事項、bounded_research_retry_report"
    }
  }
};
