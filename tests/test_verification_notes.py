from xml.etree import ElementTree as ET

import pytest

from fukamiru.authoring.verification_notes import remove_excerpts
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import prose_redraft as prose

NOTE = "ただし、原典と『対象作品』の照合はできていない。"
HTML = (
    '<article id="D" lang="ja"><section id="S"><h2 id="H">比較</h2>'
    '<p id="P">標本内の作品を比較すると、配置が異なる。' + NOTE + '</p>'
    '<p id="Q">保護した人間の文。原典は未確認である。</p></section></article>'
)


def test_verification_status_moves_to_addressed_review_without_changing_analysis_or_human_text():
    result, items = remove_excerpts(
        HTML, [{"paragraph_id": "P", "editorial_only": [NOTE]}], {"Q"}
    )
    assert ET.fromstring(result).find(".//*[@id='P']").text == (
        "標本内の作品を比較すると、配置が異なる。"
    )
    assert ET.tostring(ET.fromstring(result).find(".//*[@id='Q']")) == ET.tostring(
        ET.fromstring(HTML).find(".//*[@id='Q']")
    )
    assert items[0]["sentence_id"] == "P"
    assert items[0]["section_id"] == "S"
    assert items[0]["reason"] == NOTE
    assert items[0]["category"] == "verification_status"
    assert prose.manifest(HTML, {"Q"}) == prose.manifest(result, {"Q"})
    assert remove_excerpts(result, [{"paragraph_id": "P", "editorial_only": []}], {"Q"}) == (
        result, []
    )


@pytest.mark.parametrize("excerpt", ["存在しない確認文。", "『対象作品』の照合は済んだ。", ""])
def test_unmatched_or_fabricated_verification_text_stops_instead_of_silent_fallback(excerpt):
    with pytest.raises(NonRetryableFukamiruError):
        remove_excerpts(HTML, [{"paragraph_id": "P", "editorial_only": [excerpt]}], {"Q"})


def test_extraction_cannot_remove_locked_source_title_or_note():
    html = HTML.replace(NOTE, '<cite data-reference-id="R">『原典』</cite>' + NOTE)
    with pytest.raises(NonRetryableFukamiruError, match="editable slot"):
        remove_excerpts(html, [{"paragraph_id": "P", "editorial_only": ["『原典』"]}], {"Q"})


def test_verification_only_paragraph_is_invisible_but_retains_stable_dom_id():
    html = HTML.replace("標本内の作品を比較すると、配置が異なる。", "")
    result, items = remove_excerpts(html, [{"paragraph_id": "P", "editorial_only": [NOTE]}], {"Q"})
    assert "".join(ET.fromstring(result).find(".//*[@id='P']").itertext()) == ""
    assert len(items) == 1
    assert prose.manifest(html, {"Q"}) == prose.manifest(result, {"Q"})


def test_reader_review_omits_note_labels_but_keeps_numbers_in_analysis():
    html = HTML.replace(NOTE, '<sup id="FNREF-X"><a href="#H" role="doc-noteref">9</a></sup>')
    assert prose.reader_paragraph_text(html)["P"] == "標本内の作品を比較すると、配置が異なる。"
    assert "1980年" in prose.reader_paragraph_text(html.replace("標本内", "1980年の標本内"))["P"]
