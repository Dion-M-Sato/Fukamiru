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
      "title": "Build claim hierarchy and check concept fit",
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
      "title": "Build argument graph and check concept premises",
      "handler": "authoring.build_argument_graph",
      "resource_lane": "codex_remote",
      "max_attempts": 3,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "select_defense_obligations",
      "title": "Select material defenses and check concept fit",
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
      "title": "Validate concepts and lock section contracts",
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
      "title": "Freeze source IDs and section proofreading context",
      "handler": "authoring.llmjp33b_prepare",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "redraft_japanese_paragraphs",
      "title": "Diagnose and propose contextual Japanese corrections with LLM-jp 33B BF16 high",
      "handler": "authoring.llmjp33b_propose",
      "resource_lane": "local_llm_gpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null,
      "inference": {
        "model": "LLM-jp-4.1 33B BF16",
        "effort": "high"
      }
    },
    {
      "id": "review_japanese_sections",
      "title": "Independently review Japanese corrections with LLM-jp 33B BF16 high",
      "handler": "authoring.llmjp33b_review",
      "resource_lane": "local_llm_gpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null,
      "inference": {
        "model": "LLM-jp-4.1 33B BF16",
        "effort": "high"
      }
    },
    {
      "id": "japanese_surface_quality_gate",
      "title": "Apply source-verified, reviewed corrections and preserve protected HTML",
      "handler": "authoring.llmjp33b_apply",
      "resource_lane": "ingestion_cpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null
    },
    {
      "id": "reconcile_candidate_context_after_proofreading",
      "title": "Reconcile context and review citation predicates with LLM-jp 33B high",
      "handler": "authoring.llmjp33b_reconcile",
      "resource_lane": "local_llm_gpu",
      "max_attempts": 1,
      "join_from": [],
      "join_generation_source": null,
      "inference": {
        "model": "LLM-jp-4.1 33B BF16",
        "effort": "high"
      }
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
      "title": "Edit prose, route verification notes, and check paragraph terms and connectives",
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
      "title": "Audit final findings and contextual concept fit",
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
    },
    {
      "from": "human_approval",
      "to": "start_paragraph_formation",
      "condition": "failure"
    },
    {
      "from": "review_japanese_sections",
      "to": "japanese_surface_quality_gate",
      "condition": "success"
    }
  ],
  "reader_brief_excerpt_policy": {
    "summary": "固定した論証と資料から本文を起草し、解釈・出典・推論記録を保持します。起草前の監査で抜粋が原文と一致しない場合、抜粋だけを一度訂正させます。監査判断・理由・対象ID・指摘件数は変更できず、訂正後も連続した原文との完全一致を検査します。不一致なら停止します。元の監査と訂正を記録します。人名の頭文字を文末と誤認せず、原文と同じ文全体を帰属監査へ渡します。不明判定は引き続き停止します。本文の根拠の扱いを固定資料の支援役割と照合し、不一致を対象節の修正指示へ返します。資料の能力や検証状態は昇格させません。保存済みの監査がある抜粋不一致、人名の頭文字直後の文切れ、書誌情報を事実の根拠にできない同一入力の停止に限り、再開要求を受けて履歴を保持し、原因ごとに一度だけ処理を再開します。起草・改稿の厳格な根拠検査を通過した時点で、その本文世代の根拠対応を固定します。",
    "input": "固定した節契約・資料、原文と文章候補、監査の抜粋・判断",
    "output": "本文候補、原文と一致する監査抜粋、元の判断と訂正履歴"
  },
  "evidence_identity_policy": {
    "summary": "起草・改稿の厳格な根拠検査後に、その本文世代の主張・文・出典の対応を固定し、統合や後続の校正での対応変更を拒否します。前世代の基準と検査記録は履歴に残します。旧版が残した前世代の基準で停止した場合は、直前の完了済み起草結果との完全一致、固定資料の不変、元の基準との一致、新しい対応の厳格な再検査を確認した場合だけ、基準とその検査記録を更新して一度再開します。修正前の入力とエラーを保持します。本文・主張・出典・検証状態は変更せず、校正後の同じエラーはこの復旧の対象外です。",
    "input": "厳格な根拠検査済みの本文世代、主張・文・出典の対応、固定資料",
    "output": "同一世代の根拠対応の照合結果、前世代の比較基準と復旧の履歴"
  },
  "candidate_generation_policy": {
    "summary": "固定した節の主張・資料・人間の回答を保って、新しい本文世代を開始します。前世代の段落と出典の対応、校正・脚注・提出検査の派生結果を引き継ぎません。元の値は保存済み入力に保持し、その場所とハッシュを履歴へ記録します。全文起草の再開時にも同じ初期化を行います。旧対応の持ち越しによる脚注停止は、元の失敗入力を保持し、現本文の厳格な根拠検査と脚注計画を再確認できる場合だけ、一度本文統合へ戻します。構造・出典・日本語の監査と脚注検査を再実行します。",
    "input": "固定済み節契約・資料・保護された本文・人間の回答、前世代の派生結果",
    "output": "新しい本文世代、前世代の結果の保存場所とハッシュ、通常の監査経路"
  },
  "empty_citation_projection_policy": {
    "summary": "出典対応がないと確定した段落も投影済みとして記録し、推敲で節の候補資料を再配分しません。既存の帰属と脚注の厳格な検査は維持します。旧版で校正後の脚注が余分な対応により停止した場合は、起草の根拠対応が完全一致し、既存の引用を全て保ち、追加対応が引用なしの停止段落だけであることと、現本文の厳格な脚注計画を確認できる場合だけ一度校正後統合へ戻します。本文・資料・回答・校正結果と元の失敗入力は保持します。資料・場面が未確認という留保は、不確実性の指摘を残して後段の本文修正へ渡し、残る場合は段落を示した人間レビューとします。内部作業ラベルとは区別し、未確認を検証済みへ変更しません。この留保だけで停止した旧版は、根拠対応と脚注計画が有効な場合に、再開要求を受けて、元入力のまま一度だけ処理を再開します。",
    "input": "厳格な起草の根拠対応、全段落の引用有無、校正後の本文と保存済み結果",
    "output": "引用なしを含む段落対応、余分な投影の限定復旧と元入力の履歴"
  },
  "submission_revalidation_policy": {
    "summary": "人間の回答による再生成依頼を本文世代をまたいで保持します。同じRunの未承認候補を置き換える際は、旧本文・確認内容・ハッシュを履歴へ保存し、固定済みの新本文を改めて人間レビューへ渡します。承認済み候補は置き換えません。旧版が再生成依頼の印を初期化して最終保存で停止した場合は、保存入力と履歴のハッシュ、同じ資料・回答、送信済み回答に対応する修正依頼と未承認候補を照合し、印だけを復元して一度最終保存へ戻します。本文と検査結果は変更しません。レビューでは情報要求の対象範囲を引き継ぎ、送信済み回答と対象・要求文が一致する重複表示を除きます。保存済みの質問・回答・版と閉じた履歴は保持し、入力済みの現在の下書きは隠しません。別の対象や新しい要求は人間の確認へ残します。",
    "input": "固定済み新候補、著者の再生成依頼、未承認の旧候補と回答履歴",
    "output": "旧候補の不変の履歴、新しい提出候補と人間レビュー"
  },
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
  "review_policy": {
    "generate_proofreading_candidates": {
      "purpose": "文IDに対応する局所修正を生成",
      "summary": "元の文ID・段落ID・原文ハッシュをモデルの応答と照合し、全文を生成させず局所置換だけを受け取ります。置換IDと位置はプログラムが確定し、常体・敬体、時制・否定・モダリティ、引用・書名を検査します。適合する置換を組み合わせ、不適合な置換だけを原文と対象段落付きで人間確認へ保存します。三つの独立標本と変更なしの結果もSQLiteへ記録し、途中再開で二重生成しません。",
      "input": "文ID・元文ID・段落ID・原文ハッシュ、原文、文体条件",
      "output": "文IDを保持した局所置換と候補ID、採用・保留の置換ID、原文対応の履歴"
    },
    "preprocess_proofreading_candidates": {
      "purpose": "局所修正の原文対応と文体を検査",
      "summary": "局所置換からの再構成を照合し、検査後の無記録な変更や文体変更を候補選択へ流しません。",
      "input": "原文ID・ハッシュ、候補、局所置換の保存記録",
      "output": "対応と文体を検査した候補、または人間確認"
    },
    "rank_proofreading_candidates": {
      "purpose": "原文と局所修正候補を比較して選択",
      "summary": "Solは原文と検査済み候補を比較し、文IDごとに候補IDだけを選びます。本文の書き直しは行いません。",
      "input": "文ID、原文、検査済み候補IDと本文",
      "output": "文IDと選択候補ID、または原文維持"
    },
    "publish_proofreading_shadow": {
      "purpose": "IDを保って検証済み局所修正を反映",
      "summary": "適用直前に原文・局所置換履歴・ID対応を再検査します。保護されたインライン構造を平文化せず、段落の属性・IDを保ちます。表層処理が記録外の変更を加えた段落は戻し、元Runの履歴は変更しません。",
      "input": "選択候補ID、元のHTML、文・段落ID、原文ハッシュ、局所置換履歴",
      "output": "IDを維持した検証済みHTMLと適用・保留履歴"
    },
    "classify_audit_findings": {
      "summary": "監査の主張ID・文IDは実際の節契約と初稿の所属から節へ対応づけます。複数節に属する指摘は各節へ展開し、元の主張IDと修正対象の文IDを保持します。所属不明の指摘を捨てず停止し、節の検査範囲と修復・人間確認の区別を維持します。",
      "input": "監査指摘、節契約、初稿の主張・文ID",
      "output": "正しい対象節と原指摘を保つ監査分類"
    },
    "render_evidence_footnotes": {
      "summary": "脚注の書誌不足は検査を維持して停止します。一次の出版社・図書館情報を確認した運用者の書誌補正は、元の文献の同一性と厳格な書式検査を満たす場合だけ適用します。元入力・確認元・補正内容を履歴に残し、停止した脚注工程を一度だけ再開します。書名だけへの脚注は、単独・複数を問わず各書名の閉じ括弧『』の直前へ置きます。本文改稿と再描画でもその位置と脚注ID・番号を保ちます。参考文献用の書名抽出では脚注番号を除外し、番号付きの偽の書名を追加しません。保存済みの脚注生成前入力から、別Runで後半だけを実行できます。元Run・旧HTML・回答を保持し、新Runでは脚注以降の検証と提出保存をやり直します。既定の2試行が同一入力・同一エラーなら、失敗履歴を残して最新試行だけを再開します。人間の過剰要求判断とは別の経路で、本文・論証・主張の支持判定を変更しません。",
      "input": "保存済み入力、一次情報で確認した書誌、確認元URL",
      "output": "厳格に検査した脚注、元入力を含む補正履歴"
    },
    "redraft_body_prose": {
      "summary": "一回の本文改稿と読者評価の後、確認不足・出典照合・理論適用の未検証を本文から除き、段落IDと原文を付けて人間確認へ保存します。元段落への差し戻し後にも検査するため、確認文を本文へ戻しません。対象範囲・条件・否定・反証は命題として保ち、書名・引用・脚注を変更しません。確認事項の抽出に失敗した場合は停止し、未処理の本文を完了扱いにしません。その後、引用の有無を問わず節をまたぐ全段落境界を点検します。中心語の引き継ぎと、付加・対比・理由などに合う接続表現を別々に判定します。文中の『も』『だけでなく』だけでは段落間の付加を明示したと扱いません。問題が残る次段落の冒頭文だけを一度修正し、全境界と意味保存を独立に再検査します。保護部分と第二文以降は固定し、意味が変わる修正は採用せず、人間確認へ回します。概念の意味・対象への適用根拠・論証での役割を独立に検査します。必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。上流の生成は最大2回修復し、その都度独立に再検査します。不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。本文から除いた確認不足の条件は、人間確認へ保存します。内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。根拠カード内の不適切な概念・誤った適用も検査対象です。research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や新しい分析対象を作らないことを確認します。引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。引用符への出現だけで概念を適切・保持必須にはしません。内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。修復前後の報告書を上書きせず、失敗履歴も保持します。保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。",
      "input": "改稿・差し戻し後のHTML、保護対象、段落ごとの内容 原文の命題・出典、候補概念、節の文脈",
      "output": "確認文を除いたHTML、全境界の中心語・接続表現の判定、修正前後、人間確認事項 概念ごとの判定・保持語・修復前後・再検査記録"
    },
    "human_approval": {
      "summary": "中央で対象選択・全体コメント・承認を行い、右ペインでHTML本文と一問一答を確認します。質問ID・回答・「LLMの過剰要求である」の判断をSQLiteに保存し、回答を反映する再生成は段落形成の新しい世代から開始します。過剰と判断された同じ要求は再提示を抑制します。人間の判断を資料の根拠や出典確認済みの状態へ昇格させません。未送信の下書きは生成へ渡しません。既存の推敲・再監査にも同じ人間の判断を渡します。一貫性監査の人間判断・追加調査の指摘も質問にし、HTMLの段落と対応付けます。",
      "input": "HTML原稿、質問IDごとの回答・過剰要求の判断、全体コメント",
      "output": "承認記録、または人間の回答を持つ新しい段落形成・修正・再レビュー"
    },
    "build_claim_tree": {
      "summary": "概念の意味・対象への適用根拠・論証での役割を独立に検査します。必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。上流の生成は最大2回修復し、その都度独立に再検査します。不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。本文から除いた確認不足の条件は、人間確認へ保存します。内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。根拠カード内の不適切な概念・誤った適用も検査対象です。research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や新しい分析対象を作らないことを確認します。引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。引用符への出現だけで概念を適切・保持必須にはしません。内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。修復前後の報告書を上書きせず、失敗履歴も保持します。保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。",
      "input": " 原文の命題・出典、候補概念、節の文脈",
      "output": " 概念ごとの判定・保持語・修復前後・再検査記録"
    },
    "build_argument_graph": {
      "summary": "概念の意味・対象への適用根拠・論証での役割を独立に検査します。必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。上流の生成は最大2回修復し、その都度独立に再検査します。不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。本文から除いた確認不足の条件は、人間確認へ保存します。内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。根拠カード内の不適切な概念・誤った適用も検査対象です。research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や新しい分析対象を作らないことを確認します。引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。引用符への出現だけで概念を適切・保持必須にはしません。内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。修復前後の報告書を上書きせず、失敗履歴も保持します。保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。",
      "input": " 原文の命題・出典、候補概念、節の文脈",
      "output": " 概念ごとの判定・保持語・修復前後・再検査記録"
    },
    "select_defense_obligations": {
      "summary": "概念の意味・対象への適用根拠・論証での役割を独立に検査します。必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。上流の生成は最大2回修復し、その都度独立に再検査します。不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。本文から除いた確認不足の条件は、人間確認へ保存します。内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。根拠カード内の不適切な概念・誤った適用も検査対象です。research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や新しい分析対象を作らないことを確認します。引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。引用符への出現だけで概念を適切・保持必須にはしません。内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。修復前後の報告書を上書きせず、失敗履歴も保持します。保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。",
      "input": " 原文の命題・出典、候補概念、節の文脈",
      "output": " 概念ごとの判定・保持語・修復前後・再検査記録"
    },
    "lock_section_contracts": {
      "summary": "概念の意味・対象への適用根拠・論証での役割を独立に検査します。必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。上流の生成は最大2回修復し、その都度独立に再検査します。不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。本文から除いた確認不足の条件は、人間確認へ保存します。内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。根拠カード内の不適切な概念・誤った適用も検査対象です。research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や新しい分析対象を作らないことを確認します。引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。引用符への出現だけで概念を適切・保持必須にはしません。内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。修復前後の報告書を上書きせず、失敗履歴も保持します。保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。",
      "input": " 原文の命題・出典、候補概念、節の文脈",
      "output": " 概念ごとの判定・保持語・修復前後・再検査記録"
    },
    "diagnose_japanese_prose": {
      "purpose": "原文と校正文脈を固定",
      "summary": "CPUで節・段落・文ID・原文ハッシュと前後段落を固定します。診断は次の修正生成と一体で行います。保護ブロックとインライン構造を自動変更の対象から除きます。",
      "input": "原文HTML、文・段落ID、ハッシュ、節と隣接段落、保存済みの修正案・採否",
      "output": "ID付き局所修正、独立した採否、原文対応の履歴と人間確認"
    },
    "redraft_japanese_paragraphs": {
      "purpose": "節を読んで診断と局所修正を提案",
      "summary": "LLM-jp-4.1 33B BF16 highが、対象文そのものと前後段落を読み、一度だけ必要な局所修正を提案します。1文3候補・Luna検査・Sol順位付けは使いません。専門語や頻度だけを理由に書き換えず、中心語・接続・文体・意味を保ちます。",
      "input": "原文HTML、文・段落ID、ハッシュ、節と隣接段落、保存済みの修正案・採否",
      "output": "ID付き局所修正、独立した採否、原文対応の履歴と人間確認"
    },
    "review_japanese_sections": {
      "purpose": "原文と候補を独立に読み採否を確認",
      "summary": "別のLLM-jp 33B BF16 high呼出しで原文・候補段落・置換IDを照合し、必要性・自然さ・意味・文体・前後関係を確認します。確認側では再改稿せず、問題のある案は棄却または人間確認へ回します。",
      "input": "原文HTML、文・段落ID、ハッシュ、節と隣接段落、保存済みの修正案・採否",
      "output": "ID付き局所修正、独立した採否、原文対応の履歴と人間確認"
    },
    "audit_final_findings": {
      "summary": "概念の意味・対象への適用根拠・論証での役割を独立に検査します。必要な専門語を保持し、言い回しの問題と根拠のない前提を区別します。上流の生成は最大2回修復し、その都度独立に再検査します。不変の候補や前に拒否した候補への回帰は停止し、本文での前提の問題は上流修復へ返します。必須語も妥当性を確認し、生成済み契約だけで概念を正当化しません。原典の未確認だけで専門語を除去せず、必要な定義の説明は語を保って修復します。本文から除いた確認不足の条件は、人間確認へ保存します。内部の根拠カードでは確認範囲の記録を保ち、本文と役割を区別します。根拠カード内の不適切な概念・誤った適用も検査対象です。research_gapと採否理由も検査し、編集上の省略から標本代表性の義務や新しい分析対象を作らないことを確認します。引用された語は候補ごとに判定し、適切な専門概念の保持指定漏れを防ぎます。引用符への出現だけで概念を適切・保持必須にはしません。内部監査はreview_concept_fit_v32、GPT-5.6 Sol mediumです。再開では完全一致する受理済み検査だけを再利用し、不正な応答を受理しません。提示済みのfinding_id・obligation_idも根拠IDとして照合し、未提示IDは拒否します。原典以外の外部根拠の未解決や掲載条件の未確定も、本文から除いた原文を人間確認へ保存します。全体の形式修正後も引用語が別フィールドへ混入する場合は、最大8フィールドを各1回だけ個別照合し、全体を再検証します。根拠IDが不正な応答には、誤IDと使用可能IDを具体的に示して既定1回の形式修正を求め、意味の指摘は保持します。修復前後の報告書を上書きせず、失敗履歴も保持します。保存時のJSONキー順に依存しない単位IDの順で、検査と再開を照合します。保存済みの根拠から別Runで論証以降を再生成でき、元Runの成果物を保持します。自動脚注番号を意味検査用の本文から除き、書名中の本来の数字は保持します。HTMLと注の位置は変えず、固定済みHTMLの最終監査だけを別Runで再実行できます。元Runの本文・監査・確認事項は履歴として保持します。",
      "input": " 原文の命題・出典、候補概念、節の文脈",
      "output": " 概念ごとの判定・保持語・修復前後・再検査記録"
    },
    "japanese_surface_quality_gate": {
      "purpose": "原文と採否を照合して反映",
      "summary": "CPUが原文ハッシュ・一意な置換元・範囲・受け身と使役・文体・書名・数値・保護HTMLを再検査し、独立確認を通過した置換だけ反映します。未確認事項と保留は段落ID付きで人間確認へ送り、本文へ留保を追加しません。",
      "input": "原文HTML、文・段落ID、ハッシュ、節と隣接段落、保存済みの修正案・採否",
      "output": "ID付き局所修正、独立した採否、原文対応の履歴と人間確認"
    },
    "reconcile_candidate_context_after_proofreading": {
      "purpose": "校正後の対応情報と出典述語を確認",
      "summary": "節・根拠・引用の対応をCPUで照合し、引用述語の帰属判定もLLM-jp 33B highで行います。未確定・不整合を通過扱いにせず、指定した校正区間の最後まで検査します。",
      "input": "原文HTML、文・段落ID、ハッシュ、節と隣接段落、保存済みの修正案・採否",
      "output": "ID付き局所修正、独立した採否、原文対応の履歴と人間確認"
    }
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
      "summary": "未解決の必須義務・対抗仮説を優先し、調査対象に選ばれたpreferred命題も含めて最大4件を一度だけ追加調査します。RAGで確認済みの義務は外部調査を省き、明示的な先行研究追加・対抗説・論文指定は残します。不足事項と読んだ資料の引用から検索語を更新し、初回RAGで使った未送信の検索語も利用します。短い検索語・同義語・書籍・一次資料を区別して公式DBを再調査し、なお不足するテーマだけRustXNG v0.30.0（Standard）の一般Web検索で日本語研究資料と事実の出典を補います。語句一致やスニペットだけを根拠にせず、取得した内容を同じ根拠検査へ渡します。未読書籍は関連性条件を満たせば機械判別参考文献の候補として保持します。同じ引用照合を行い、執筆には取得済み資料を引き継ぎます。未完了のDB検索も引き継ぎます。取得済み資料がある場合は一度待機して再開し、なお失敗するDBは未完了と明記して他の資料の検証を進めます。論文・書籍・一次資料・書誌を区別して保存し、本文取得の有無と検証状況は別に保持します。",
      "input": "未解決の証拠義務、既存の検索計画・調査結果",
      "output": "再評価済みの根拠と未確認事項、bounded_research_retry_report"
    }
  },
  "proofreading_profile": "llmjp33b_high"
};
