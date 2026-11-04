"""Immutable revision services with human-edit protection."""

from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.collocation import (
    CollocationFrame,
    CollocationRepository,
    CollocationScorer,
    CorpusCollocationScorer,
    LLMJPBaseScorer,
    extract_collocation_frames,
    frame_from_dict,
    generate_probes,
    score_payload,
    valid_candidate,
)
from fukamiru.revisions.discourse import ManuscriptDiscourseService
from fukamiru.revisions.enumeration import ManuscriptEnumerationService
from fukamiru.revisions.html_import import HTMLImportService
from fukamiru.revisions.japanese_polish import (
    CandidateApplicationService,
    CandidateCorpusScorer,
    CandidateHardVeto,
    JapaneseCandidateGenerator,
    JapanesePolishTargetDetector,
    PolishTarget,
    SectionCandidateSelector,
    WordingCandidate,
)
from fukamiru.revisions.manuscript_import import ManuscriptImportService
from fukamiru.revisions.manuscript_policy import (
    MANUSCRIPT_WORKFLOW_V22_POLICY,
    manuscript_workflow_v22_policy,
)
from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
from fukamiru.revisions.references import (
    REFERENCE_RESOLUTION_POLICY_V22,
    SJLLF_JA_REFERENCE_RULESET,
    JapaneseReferenceService,
)
from fukamiru.revisions.rewrite import ManuscriptRewriteService
from fukamiru.revisions.service import ProtectedBlockError, RevisionService

__all__ = [
    "HTMLImportService",
    "CandidateApplicationService",
    "CollocationFrame",
    "CollocationRepository",
    "CollocationScorer",
    "CorpusCollocationScorer",
    "CandidateCorpusScorer",
    "CandidateHardVeto",
    "JapaneseCandidateGenerator",
    "JapanesePolishTargetDetector",
    "ManuscriptImportService",
    "MANUSCRIPT_WORKFLOW_V22_POLICY",
    "ManuscriptCoherenceService",
    "ManuscriptDiscourseService",
    "ManuscriptEnumerationService",
    "ManuscriptReasoningRepository",
    "JapaneseReferenceService",
    "LLMJPBaseScorer",
    "ManuscriptRewriteService",
    "PolishTarget",
    "ProtectedBlockError",
    "RevisionService",
    "REFERENCE_RESOLUTION_POLICY_V22",
    "SectionCandidateSelector",
    "SJLLF_JA_REFERENCE_RULESET",
    "WordingCandidate",
    "manuscript_workflow_v22_policy",
    "extract_collocation_frames",
    "frame_from_dict",
    "generate_probes",
    "score_payload",
    "valid_candidate",
]
