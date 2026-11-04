from pathlib import Path

import pytest
from pypdf import PdfWriter

from fukamiru.errors import FukamiruError
from fukamiru.knowledge.pdf_text_layer import (
    has_usable_text_layer,
    inspect_pdf_text_layer,
    require_pdf_text_layer,
)


def test_shared_threshold_rejects_empty_and_short_pdf_text() -> None:
    assert not has_usable_text_layer(0, 100)
    assert not has_usable_text_layer(1, 19)
    assert has_usable_text_layer(1, 20)
    assert not has_usable_text_layer(10, 49)
    assert has_usable_text_layer(10, 50)


def test_blank_pdf_is_detected_before_extraction(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as output:
        writer.write(output)

    result = inspect_pdf_text_layer(path)
    assert (result.page_count, result.visible_characters, result.minimum_characters) == (1, 0, 20)
    assert not result.usable
    with pytest.raises(FukamiruError, match="OCR_REQUIRED"):
        require_pdf_text_layer(path)
