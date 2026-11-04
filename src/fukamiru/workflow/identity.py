"""Stable public identity for the manuscript generation workflow."""

MANUSCRIPT_GENERATOR = "manuscript_generator"
LEGACY_AUTHORING = "authoring"
MANUSCRIPT_GENERATOR_NAMES = frozenset({MANUSCRIPT_GENERATOR, LEGACY_AUTHORING})


def is_manuscript_generator_workflow(name: str | None) -> bool:
    """Include persisted runs created before the public workflow rename."""

    return name in MANUSCRIPT_GENERATOR_NAMES
