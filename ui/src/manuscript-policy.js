export function manuscriptRewriteWorkflowVersion(proposal) {
  const context = proposal?.context || {};
  const policy = context.manuscript_workflow_policy || {};
  for (const value of [context.workflow_version, context.workflow?.version, policy.workflow_version]) {
    const parsed = Number.parseInt(String(value ?? ""), 10);
    if (Number.isFinite(parsed)) return parsed;
  }
  return /workflow-v22/i.test(String(policy.id || "")) ? 22 : 0;
}

export function manuscriptResponseContractIncomplete(proposal) {
  if (manuscriptRewriteWorkflowVersion(proposal) < 22) return false;
  const context = proposal?.context || {};
  const conditions = Array.isArray(context.conditional_conditions)
    ? context.conditional_conditions
    : [];
  const languageRequirements = Array.isArray(context.language_review_requirements)
    ? context.language_review_requirements
    : [];
  return conditions.some((item) => item?.reason === "response_contract_incomplete")
    || context.conditional_success?.reason === "response_contract_incomplete"
    || languageRequirements.some((item) =>
      item?.review_class === "response_contract_incomplete"
      && !["resolved", "no_revision_needed"].includes(item?.status));
}

export function manuscriptSemanticResolutionRequired(proposal) {
  if (manuscriptRewriteWorkflowVersion(proposal) < 22) return false;
  const context = proposal?.context || {};
  const conditions = Array.isArray(context.conditional_conditions)
    ? context.conditional_conditions
    : [];
  const languageRequirements = Array.isArray(context.language_review_requirements)
    ? context.language_review_requirements
    : [];
  const reviewItems = Array.isArray(context.semantic_review_items)
    ? context.semantic_review_items
    : [];
  return reviewItems.some((item) =>
    item?.review_class === "human_semantic_judgement_required"
    && item?.status !== "resolved")
    || conditions.some((item) => item?.reason === "semantic_resolution_required")
    || context.conditional_success?.reason === "semantic_resolution_required"
    || languageRequirements.some((item) =>
      !["resolved", "no_revision_needed"].includes(item?.status)
      && ![
        "minor_lexical_source_preserved",
        "polish_candidate_source_preserved",
        "response_contract_incomplete",
      ].includes(item?.review_class));
}

export function manuscriptReferenceApprovalRequired(proposal) {
  if (manuscriptRewriteWorkflowVersion(proposal) < 22) return false;
  const context = proposal?.context || {};
  const qualityGate = context.reference_quality_gate;
  const confirmationItems = Array.isArray(context.reference_metadata_confirmation_items)
    ? context.reference_metadata_confirmation_items
    : [];
  const references = [
    ...(Array.isArray(context.reference_entries) ? context.reference_entries : []),
    ...(Array.isArray(context.machine_reference_entries)
      ? context.machine_reference_entries
      : []),
  ];
  return context.reference_approval?.final_revision_blocked === true
    || !qualityGate
    || qualityGate.passed !== true
    || confirmationItems.length > 0
    || references.some((item) => item?.final_revision_blocked === true);
}

export function manuscriptFinalApprovalAllowed(proposal) {
  if (manuscriptRewriteWorkflowVersion(proposal) >= 24) {
    // V24 approves the reviewed text as a whole. Unverified claims and
    // bibliography remain explicitly conditional in the proposal audit.
    const context = proposal?.context || {};
    return proposal?.validation?.valid === true
      && (context.structure_plan_validation?.valid === true
        || context.structure_plan_validation?.valid_for_conditional_execution === true)
      && context.paragraph_plan_validation?.valid === true
      && context.paragraph_plan_validation?.global_valid === true
      && context.approval_structure_lock?.lock_status === "locked";
  }
  return proposal?.validation?.valid === true
    && !manuscriptResponseContractIncomplete(proposal)
    && !manuscriptSemanticResolutionRequired(proposal)
    && !manuscriptReferenceApprovalRequired(proposal);
}
