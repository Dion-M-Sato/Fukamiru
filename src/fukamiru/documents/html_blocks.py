"""Read canonical document blocks in source order."""

import re
from html.parser import HTMLParser


class _CanonicalBlockParser(HTMLParser):
    _CAPTURE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str]] = []
        self._capture_tag: str | None = None
        self._capture_depth = 0
        self._text: list[str] = []
        self._lists: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"ul", "ol"}:
            self._lists.append(tag)
        if self._capture_tag is not None:
            if tag == "br":
                self._text.append("\n")
                return
            self._capture_depth += 1
            return
        if tag in self._CAPTURE_TAGS:
            self._capture_tag = tag
            self._capture_depth = 1
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._capture_tag is not None:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                text = re.sub(r"[ \t\f\v]+", " ", "".join(self._text))
                text = re.sub(r" *\n *", "\n", text).strip()
                if text:
                    kind = self._capture_tag
                    if kind in {"h5", "h6"}:
                        kind = "h4"
                    elif kind == "p":
                        kind = "paragraph"
                    elif kind == "blockquote":
                        kind = "quote"
                    elif kind == "li":
                        kind = "ol1" if self._lists and self._lists[-1] == "ol" else "li1"
                    self.blocks.append({"type": kind, "text": text})
                self._capture_tag = None
                self._text = []
        if tag in {"ul", "ol"} and self._lists:
            self._lists.pop()

    def handle_data(self, data: str) -> None:
        if self._capture_tag is not None:
            self._text.append(data)


def canonical_html_blocks(source: str) -> list[dict[str, str]]:
    parser = _CanonicalBlockParser()
    parser.feed(source)
    parser.close()
    return parser.blocks
