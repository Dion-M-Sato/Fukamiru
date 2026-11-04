"""Literature discovery providers and Candidate Review persistence."""

from fukamiru.literature.service import (
    CrossrefProvider,
    LiteratureCandidate,
    LiteratureDiscoveryService,
    NDLSearchProvider,
    OAAcquisitionService,
    OpenAlexProvider,
    SemanticScholarProvider,
)

__all__ = [
    "CrossrefProvider",
    "LiteratureCandidate",
    "LiteratureDiscoveryService",
    "NDLSearchProvider",
    "OAAcquisitionService",
    "OpenAlexProvider",
    "SemanticScholarProvider",
]
