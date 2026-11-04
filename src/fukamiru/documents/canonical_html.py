"""Strict, deterministic validation for Fukamiru canonical HTML fragments."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from fukamiru.errors import CanonicalHTMLValidationError

BLOCK_TAGS = {
    "article",
    "section",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "blockquote",
    "ul",
    "ol",
    "li",
}
INLINE_TAGS = {"span", "em", "strong", "cite", "q", "a", "sup", "sub", "code"}
ALLOWED_TAGS = BLOCK_TAGS | INLINE_TAGS
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
HEADING_TAGS = {f"h{level}" for level in range(1, 7)}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    element_id: str | None = None


@dataclass(frozen=True)
class CanonicalHTMLReport:
    issues: tuple[ValidationIssue, ...]
    element_count: int
    block_count: int

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


class CanonicalHTMLValidator:
    """Validate an XML-compatible HTML5 fragment with one article root."""

    def validate(self, html: str) -> CanonicalHTMLReport:
        issues: list[ValidationIssue] = []
        try:
            root = ET.fromstring(html)
        except ET.ParseError as exc:
            return CanonicalHTMLReport(
                issues=(ValidationIssue("html.parse_error", str(exc)),),
                element_count=0,
                block_count=0,
            )

        elements = list(root.iter())
        if root.tag != "article":
            issues.append(ValidationIssue("html.root", "Root element must be <article>"))
        if root.tag == "article":
            self._require_id(root, issues)
            if not root.attrib.get("lang"):
                issues.append(
                    ValidationIssue(
                        "html.article_lang",
                        "The article root requires a lang attribute",
                        element_id=root.attrib.get("id"),
                    )
                )

        ids: dict[str, ET.Element] = {}
        for element in elements:
            element_id = element.attrib.get("id")
            if element.tag not in ALLOWED_TAGS:
                issues.append(
                    ValidationIssue(
                        "html.tag_not_allowed",
                        f"Tag <{element.tag}> is not allowed",
                        element_id=element_id,
                    )
                )
            self._validate_attributes(element, issues)
            if element.tag in BLOCK_TAGS:
                self._require_id(element, issues)
            if element_id:
                if not IDENTIFIER_PATTERN.fullmatch(element_id):
                    issues.append(
                        ValidationIssue(
                            "html.invalid_id",
                            f"Invalid stable id: {element_id}",
                            element_id=element_id,
                        )
                    )
                if element_id in ids:
                    issues.append(
                        ValidationIssue(
                            "html.duplicate_id",
                            f"Duplicate id: {element_id}",
                            element_id=element_id,
                        )
                    )
                ids[element_id] = element

        if root.tag == "article":
            self._validate_article_children(root, issues)
            for section in root.iter("section"):
                self._validate_section(section, root, issues)
        self._validate_inline_structure(root, issues)
        self._validate_cross_references(elements, ids, issues)
        return CanonicalHTMLReport(
            issues=tuple(issues),
            element_count=len(elements),
            block_count=sum(element.tag in BLOCK_TAGS for element in elements),
        )

    def assert_valid(self, html: str) -> CanonicalHTMLReport:
        report = self.validate(html)
        if not report.valid:
            summary = "; ".join(f"{issue.code}: {issue.message}" for issue in report.issues)
            raise CanonicalHTMLValidationError(summary)
        return report

    @staticmethod
    def _require_id(element: ET.Element, issues: list[ValidationIssue]) -> None:
        if not element.attrib.get("id"):
            issues.append(
                ValidationIssue(
                    "html.missing_id",
                    f"Block element <{element.tag}> requires a stable id",
                )
            )

    @staticmethod
    def _validate_attributes(element: ET.Element, issues: list[ValidationIssue]) -> None:
        allowed = {"id", "lang", "dir", "class", "role", "aria-label"}
        if element.tag == "a":
            allowed |= {"href", "title"}
        for attribute in element.attrib:
            lowered = attribute.lower()
            if lowered.startswith("on") or lowered == "style":
                issues.append(
                    ValidationIssue(
                        "html.unsafe_attribute",
                        f"Attribute {attribute} is not allowed",
                        element_id=element.attrib.get("id"),
                    )
                )
            elif attribute not in allowed and not attribute.startswith("data-"):
                issues.append(
                    ValidationIssue(
                        "html.attribute_not_allowed",
                        f"Attribute {attribute} is not canonical",
                        element_id=element.attrib.get("id"),
                    )
                )

    @staticmethod
    def _validate_article_children(root: ET.Element, issues: list[ValidationIssue]) -> None:
        for child in root:
            if child.tag not in {"section"}:
                issues.append(
                    ValidationIssue(
                        "html.article_child",
                        "The article root may contain section elements only",
                        element_id=child.attrib.get("id"),
                    )
                )

    @staticmethod
    def _section_depth(section: ET.Element, root: ET.Element) -> int:
        def walk(node: ET.Element, depth: int) -> int | None:
            for child in node:
                if child is section:
                    return depth
                found = walk(child, depth + (1 if child.tag == "section" else 0))
                if found is not None:
                    return found
            return None

        return walk(root, 1) or 1

    def _validate_section(
        self,
        section: ET.Element,
        root: ET.Element,
        issues: list[ValidationIssue],
    ) -> None:
        children = list(section)
        if not children or children[0].tag not in HEADING_TAGS:
            issues.append(
                ValidationIssue(
                    "html.section_heading",
                    "Each section must begin with a heading",
                    element_id=section.attrib.get("id"),
                )
            )
            return
        expected_level = min(self._section_depth(section, root) + 1, 6)
        actual_level = int(children[0].tag[1])
        if actual_level != expected_level:
            issues.append(
                ValidationIssue(
                    "html.heading_level",
                    (
                        f"Expected <h{expected_level}> at this section depth, "
                        f"found <{children[0].tag}>"
                    ),
                    element_id=children[0].attrib.get("id"),
                )
            )
        for child in children[1:]:
            if child.tag in HEADING_TAGS:
                issues.append(
                    ValidationIssue(
                        "html.extra_heading",
                        "A section may have only its leading heading",
                        element_id=child.attrib.get("id"),
                    )
                )

    @staticmethod
    def _validate_inline_structure(root: ET.Element, issues: list[ValidationIssue]) -> None:
        for parent in root.iter():
            if parent.tag in INLINE_TAGS:
                for child in parent:
                    if child.tag in BLOCK_TAGS:
                        issues.append(
                            ValidationIssue(
                                "html.block_in_inline",
                                f"Block <{child.tag}> cannot appear inside <{parent.tag}>",
                                element_id=child.attrib.get("id"),
                            )
                        )

    @staticmethod
    def _validate_cross_references(
        elements: Iterable[ET.Element],
        ids: dict[str, ET.Element],
        issues: list[ValidationIssue],
    ) -> None:
        for element in elements:
            if element.tag != "a":
                continue
            href = element.attrib.get("href", "")
            if href.startswith("#") and href[1:] not in ids:
                issues.append(
                    ValidationIssue(
                        "html.broken_xref",
                        f"Cross-reference target does not exist: {href}",
                        element_id=element.attrib.get("id"),
                    )
                )


def structural_block_fragments(html: str) -> list[tuple[str, str, str]]:
    """Return stable id, normalized block kind, and a deterministic fragment."""
    root = ET.fromstring(html)
    fragments: list[tuple[str, str, str]] = []
    for element in root.iter():
        if element.tag not in BLOCK_TAGS:
            continue
        element_id = element.attrib["id"]
        if element.tag in {"article", "section", "ul", "ol"}:
            attributes = " ".join(
                f'{name}="{value}"' for name, value in sorted(element.attrib.items())
            )
            fragment = f"<{element.tag} {attributes}>"
            block_kind = "list" if element.tag in {"ul", "ol"} else element.tag
        else:
            fragment = ET.tostring(element, encoding="unicode", short_empty_elements=False)
            if element.tag in HEADING_TAGS:
                block_kind = "heading"
            elif element.tag == "p":
                block_kind = "paragraph"
            elif element.tag in {"ul", "ol", "li"}:
                block_kind = "list"
            else:
                block_kind = element.tag
        fragments.append((element_id, block_kind, fragment))
    return fragments
