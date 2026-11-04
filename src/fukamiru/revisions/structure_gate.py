"""Heading integrity independent of model judgements and paragraph lengths."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from fukamiru.revisions.coherence import ManuscriptCoherenceService


def heading_edit_issue(source: str, candidate: str, tag: str) -> str | None:
    before = ManuscriptCoherenceService.heading(source, tag)
    after = ManuscriptCoherenceService.heading(candidate, tag)
    if after and (re.search(r"[\r\n]", candidate) or re.search(r"。\s*\S", after[1])):
        return "heading.body_contamination"
    if before and (not after or before[0] != after[0]):
        return "heading.level_changed"
    if not before and after:
        return "heading.unplanned_creation"
    return None


def heading_issues(source_html: str, candidate_html: str) -> list[dict[str, str]]:
    # The existing terminal-section policy explicitly nests future work under
    # the conclusion. Compare against that authorized hierarchy, not stale input.
    source_html, _, _ = ManuscriptCoherenceService.normalize_terminal_sections(source_html)
    before = {
        e.get("id"): ("".join(e.itertext()).strip(), e.tag)
        for e in ET.fromstring(source_html).iter()
        if e.get("id")
    }
    issues = []
    for element in ET.fromstring(candidate_html).iter():
        if element.tag not in {"p", "h1", "h2", "h3", "h4", "h5", "h6"}:
            continue
        text = "".join(element.itertext()).strip()
        old, tag = before.get(element.get("id"), (text, element.tag))
        code = heading_edit_issue(old, text, tag)
        old_heading = ManuscriptCoherenceService.heading(old, tag)
        new_heading = ManuscriptCoherenceService.heading(text, element.tag)
        if old_heading and new_heading and old_heading[0] != new_heading[0]:
            code = "heading.level_changed"
        if code:
            issues.append({"block_id": str(element.get("id")), "code": code})
    return issues
