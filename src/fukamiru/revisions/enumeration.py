"""Application-owned detection of enumerations that need discourse context."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fukamiru.revisions.coherence import ManuscriptCoherenceService

_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
_QUOTED_ITEM_RE = re.compile(r"(?:『[^』]{1,120}』|「[^」]{1,120}」)")
_QUOTED_SEQUENCE_RE = re.compile(
    r"(?:『[^』]{1,120}』|「[^」]{1,120}」)"
    r"(?:(?:\s*[,、・／/]?\s*|(?:および|及び|ならびに|並びに|と|や))"
    r"(?:『[^』]{1,120}』|「[^」]{1,120}」))+"
)
_NOUN = r"(?:[一-龥々〆ヶァ-ヴーA-Za-z0-9]+|ざまぁ|おたく|マンガ)"
_NOUN_PHRASE = _NOUN + r"(?:の" + _NOUN + r")*"
_COORDINATION_RE = re.compile(
    r"(?<![一-龥々〆ヶぁ-んァ-ヴーA-Za-z0-9])(?P<sequence>"
    + _NOUN_PHRASE
    + r"(?:(?:、|および|及び|ならびに|並びに|と|や)"
    + _NOUN_PHRASE
    + r")+)"
    r"(?=(?:を|が|は|に|へ|で|も|から|として|など|等|について|[。！？!?]|$))"
)
_SEPARATOR_RE = re.compile(r"\s*(、|および|及び|ならびに|並びに|と|や)\s*")


def _stable_id(*parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()
    return f"enumeration-{digest[:24]}"


class ManuscriptEnumerationService:
    """Inventory coordination at first mention without count or length thresholds."""

    @classmethod
    def inventory(cls, canonical_html: str) -> list[dict[str, Any]]:
        structure = ManuscriptCoherenceService.semantic_structure(canonical_html)
        records: list[dict[str, Any]] = []
        previously_seen = ""
        for block in structure.get("blocks", []):
            if not isinstance(block, dict):
                continue
            if not block.get("rewritable_as_argument", True):
                continue
            if block.get("is_heading") or block.get("inline_protected"):
                previously_seen += " " + str(block.get("text") or "")
                continue
            if str(block.get("tag") or "") not in {"p", "blockquote", "li"}:
                continue
            block_id = str(block.get("id") or "")
            section_id = str(block.get("section_id") or "semantic:preface")
            block_text = str(block.get("text") or "")
            for sentence_index, match in enumerate(_SENTENCE_RE.finditer(block_text)):
                sentence = match.group(0).strip()
                if not sentence:
                    continue
                spans: list[tuple[int, int, str, list[str], str]] = []
                for sequence_match in _QUOTED_SEQUENCE_RE.finditer(sentence):
                    sequence = sequence_match.group(0)
                    items = _QUOTED_ITEM_RE.findall(sequence)
                    if len(items) >= 2:
                        spans.append(
                            (
                                sequence_match.start(),
                                sequence_match.end(),
                                sequence,
                                items,
                                "quoted_coordination",
                            )
                        )
                for sequence_match in _COORDINATION_RE.finditer(sentence):
                    sequence = sequence_match.group("sequence").strip()
                    if any(start <= sequence_match.start() < end for start, end, *_ in spans):
                        continue
                    items = [item.strip(" 、") for item in _SEPARATOR_RE.split(sequence)[::2]]
                    items = [item for item in items if item]
                    if len(items) >= 2:
                        spans.append(
                            (
                                sequence_match.start(),
                                sequence_match.end(),
                                sequence,
                                items,
                                "nominal_coordination",
                            )
                        )
                for start, end, sequence, items, kind in sorted(spans):
                    first_mentions = [item not in previously_seen for item in items]
                    records.append(
                        {
                            "candidate_id": _stable_id(block_id, match.start() + start, sequence),
                            "section_id": section_id,
                            "block_id": block_id,
                            "sentence_index": sentence_index,
                            "start_offset": match.start() + start,
                            "end_offset": match.start() + end,
                            "surface": sequence,
                            "items": items,
                            "kind": kind,
                            "separator": "・" if "・" in sequence else "、",
                            "first_mention_by_item": first_mentions,
                            "requires_context_resolution": any(first_mentions),
                        }
                    )
            previously_seen += " " + block_text
        return records
