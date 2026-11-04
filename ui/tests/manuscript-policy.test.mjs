import test from "node:test";
import assert from "node:assert/strict";

import {
  manuscriptFinalApprovalAllowed,
  manuscriptReferenceApprovalRequired,
  manuscriptResponseContractIncomplete,
  manuscriptSemanticResolutionRequired,
} from "../src/manuscript-policy.js";

test("V22 partial mandatory responses disable final approval", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      conditional_conditions: [{
        reason: "response_contract_incomplete",
        phase: "rewrite",
        unit_ids: ["P-2"],
      }],
    },
  };

  assert.equal(manuscriptResponseContractIncomplete(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), false);
});

test("V24 allows whole-proposal text approval while preserving unresolved flags", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 24,
      structure_plan_validation: { valid: false, valid_for_conditional_execution: true },
      paragraph_plan_validation: { valid: true, global_valid: true, fallback_used: true },
      paragraph_plan: { unresolved_boundaries: ["source boundary retained"] },
      approval_structure_lock: { lock_status: "locked" },
      conditional_conditions: [{ reason: "semantic_resolution_required" }],
      reference_approval: { final_revision_blocked: true },
      reference_quality_gate: { passed: false },
    },
  };

  assert.equal(manuscriptSemanticResolutionRequired(proposal), true);
  assert.equal(manuscriptReferenceApprovalRequired(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed({ ...proposal, validation: { valid: false } }), false);
});

test("V21 behavior ignores the V22 response-contract policy", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 21,
      conditional_conditions: [{ reason: "response_contract_incomplete" }],
    },
  };

  assert.equal(manuscriptResponseContractIncomplete(proposal), false);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), true);
});

test("V22 genuine semantic uncertainty requires human resolution before approval", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      conditional_conditions: [{
        reason: "semantic_resolution_required",
        category: "claim_causality",
      }],
    },
  };

  assert.equal(manuscriptSemanticResolutionRequired(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), false);
});

test("V22 language-review semantic uncertainty also blocks final approval", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      conditional_conditions: [{ reason: "language_review_required" }],
      language_review_requirements: [{
        kind: "citation_function",
        status: "semantic_analysis_unresolved",
        review_class: "human_semantic_judgement_required",
      }],
    },
  };

  assert.equal(manuscriptSemanticResolutionRequired(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), false);
});

test("V22 exposed semantic review items independently block final approval", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      semantic_review_items: [{
        review_id: "semantic-review-v22-fixture",
        review_class: "human_semantic_judgement_required",
        status: "pending_human_judgement",
      }],
    },
  };

  assert.equal(manuscriptSemanticResolutionRequired(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), false);
});

test("V22 permits only the two source-preservation review classes", () => {
  const proposalFor = (reviewClass) => ({
    validation: { valid: true },
    context: {
      workflow_version: 22,
      language_review_requirements: [{
        status: "mandatory_revision_unmet",
        review_class: reviewClass,
      }],
      reference_quality_gate: { passed: true, issues: [] },
    },
  });

  for (const reviewClass of [
    "human_semantic_judgement_required",
    "mandatory_revision_unmet",
    "unclassified_analysis_required",
  ]) {
    assert.equal(manuscriptFinalApprovalAllowed(proposalFor(reviewClass)), false);
  }
  for (const reviewClass of [
    "minor_lexical_source_preserved",
    "polish_candidate_source_preserved",
  ]) {
    assert.equal(manuscriptFinalApprovalAllowed(proposalFor(reviewClass)), true);
  }
});

test("V22 response-contract language requirement uses the response warning gate", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      language_review_requirements: [{
        status: "response_contract_incomplete",
        review_class: "response_contract_incomplete",
      }],
    },
  };

  assert.equal(manuscriptResponseContractIncomplete(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), false);
});

test("V22 minor lexical source preservation remains human-approvable", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      conditional_conditions: [{ reason: "language_review_required" }],
      language_review_requirements: [{
        kind: "contextual_lexicon",
        status: "candidates_inappropriate",
        review_class: "minor_lexical_source_preserved",
        disposition: "source_preserved",
      }],
      reference_quality_gate: { passed: true, issues: [] },
    },
  };

  assert.equal(manuscriptSemanticResolutionRequired(proposal), false);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), true);
});

test("V22 unresolved bibliography metadata blocks final approval", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      reference_quality_gate: { passed: false, issues: [] },
      reference_approval: { final_revision_blocked: true },
      reference_metadata_confirmation_items: [{ reference_id: "ref-1" }],
    },
  };

  assert.equal(manuscriptReferenceApprovalRequired(proposal), true);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), false);
});

test("V22 passed bibliography gate does not block final approval", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 22,
      reference_quality_gate: { passed: true, issues: [] },
      reference_approval: { final_revision_blocked: false },
      reference_metadata_confirmation_items: [],
      reference_entries: [],
      machine_reference_entries: [],
    },
  };

  assert.equal(manuscriptReferenceApprovalRequired(proposal), false);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), true);
});

test("V21 semantic conditions do not retroactively change approval behavior", () => {
  const proposal = {
    validation: { valid: true },
    context: {
      workflow_version: 21,
      conditional_conditions: [{ reason: "semantic_resolution_required" }],
    },
  };

  assert.equal(manuscriptSemanticResolutionRequired(proposal), false);
  assert.equal(manuscriptFinalApprovalAllowed(proposal), true);
});

test("canonical validation remains an independent approval requirement", () => {
  assert.equal(manuscriptFinalApprovalAllowed({
    validation: { valid: false },
    context: { workflow_version: 22 },
  }), false);
});
