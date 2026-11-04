"""Standalone HTML document display using the workbench's shared design tokens."""

from __future__ import annotations

import base64
import hashlib
import re
from html import escape
from pathlib import Path

_STYLES = Path(__file__).resolve().parents[3] / "ui" / "src"


def _stylesheet() -> str:
    return "\n".join(
        (_STYLES / name).read_text(encoding="utf-8")
        for name in ("theme.css", "document.css")
    )


def _policy(css: str) -> str:
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    return (
        "default-src 'none'; script-src 'none'; "
        f"style-src 'sha256-{digest}'; "
        "img-src data:; base-uri 'none'; form-action 'none'; object-src 'none'"
    )


def document_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": _policy(_stylesheet()) + "; frame-ancestors 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def render_document_html(source: str, title: str = "原稿") -> str:
    """Add display styles without rewriting canonical IDs, citations or source bytes."""
    css = _stylesheet()
    body = re.search(r"<body\b[^>]*>(.*?)</body\s*>", source, re.I | re.S)
    content = body[1] if body else source
    return (
        '<!doctype html>\n<html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(_policy(css), quote=True)}">'
        f"<title>{escape(title)}</title><style>{css}</style></head>"
        f'<body><main class="document-output">{content}</main></body></html>\n'
    )
