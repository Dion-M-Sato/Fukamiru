"""HTML output preserves source semantics and needs no external renderer."""

import base64
import hashlib
import re

from fukamiru.documents.html_blocks import canonical_html_blocks
from fukamiru.documents.html_preview import document_response_headers, render_document_html


def test_standalone_output_preserves_citations_notes_and_protected_markup():
    source = (
        '<article id="document" lang="en"><section id="section">'
        '<h1 id="heading">Fictional research</h1>'
        '<p id="protected" data-human-protected="true"><cite>Example Book</cite>'
        '<sup><a href="#note">1</a></sup></p>'
        '<aside id="note">A fictional note.</aside></section></article>'
    )
    rendered = render_document_html(source, '<Example & "title">')
    assert source in rendered
    assert '<title>&lt;Example &amp; &quot;title&quot;&gt;</title>' in rendered
    assert "--workspace: #172713" in rendered
    assert "--font-editorial:" in rendered
    assert "cite { font-style: normal; }" in rendered
    assert '<link rel="stylesheet"' not in rendered
    assert '<script src=' not in rendered


def test_standalone_and_http_policies_allow_only_the_shared_stylesheet():
    rendered = render_document_html('<p id="text">Text</p>')
    css = re.search(r"<style>(.*?)</style>", rendered, re.S)[1]
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    headers = document_response_headers()
    assert f"'sha256-{digest}'" in headers["Content-Security-Policy"]
    assert "script-src 'none'" in headers["Content-Security-Policy"]
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "private, no-store"


def test_full_html_input_keeps_its_body_without_nesting_another_document():
    source = '<html><head><title>Old</title></head><body><article>Body</article></body></html>'
    output = render_document_html(source, "Current")
    assert output.count("<html") == 1
    assert "<article>Body</article>" in output
    assert "<title>Current</title>" in output


def test_plain_block_export_keeps_heading_paragraph_and_footnote_order():
    assert canonical_html_blocks(
        '<article><h2>Heading</h2><p>First<br>Second</p>'
        '<ol><li>Note</li></ol></article>'
    ) == [
        {"type": "h2", "text": "Heading"},
        {"type": "paragraph", "text": "First\nSecond"},
        {"type": "ol1", "text": "Note"},
    ]
