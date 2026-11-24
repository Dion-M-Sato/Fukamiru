"""Reading order, complete context, provenance and damaged-source admission."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pymupdf
import pytest

from fukamiru.knowledge.page_reading import native_pdf_pages, page_passages, text_issues
from fukamiru.knowledge.pdf_layout import PDFLayoutAnalyzer, PositionedText
from fukamiru.revisions.research_validation import grounded_support


def book(pages):
    return {"source_asset_id": "asset", "document_unit_id": "doc", "content_sha256": "sha",
            "bibliography": {}, "pages": [
                {"number": n, "text": text, "extraction_method": "pdf_text_stream",
                 "source_span_ids": [f"old-span-{n}"]}
                for n, text in enumerate(pages, 1)]}


def test_vertical_glyphs_are_columns_not_interleaved_horizontal_rows():
    columns = ["労働は生命を維持する。", "仕事は世界を作り出す。"]
    spans = [PositionedText(c, x, 30 + i * 10, x + 8, 40 + i * 10, 10, "OCR")
             for x, text in zip([200, 180], columns, strict=True) for i, c in enumerate(text)]
    lines = PDFLayoutAnalyzer._visual_lines(spans, page_width=300)
    assert [line.text for line in lines] == columns


def test_horizontal_single_glyphs_keep_left_to_right_order():
    text = "横書きの日本語も存在する。"
    spans = [PositionedText(c, 20 + i * 10, 30, 30 + i * 10, 40, 10, "OCR")
             for i, c in enumerate(text)]
    lines = PDFLayoutAnalyzer._visual_lines(spans, page_width=300)
    assert "".join(line.text for line in lines) == text


def test_real_searchable_vertical_pdf_is_not_saved_as_interleaved_rows(tmp_path):
    path = tmp_path / "vertical.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=400)
        for x, text in [(220, "労働は生命を維持する。"), (200, "仕事は世界を作り出す。")]:
            for i, char in enumerate(text):
                page.insert_text((x, 40 + i * 12), char, fontsize=10, fontname="japan")
        pdf.save(path)
    document = PDFLayoutAnalyzer().analyze(path, source_asset_id="vertical")
    assert "労働は生命を維持する。" in document.pages[0].text
    assert "仕事は世界を作り出す。" in document.pages[0].text
    pages = native_pdf_pages(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert "労働は生命を維持する。" in pages[0]["text"]
    with pytest.raises(ValueError, match="hash_mismatch"):
        native_pdf_pages(path, "wrong-source-hash")


def test_body_definition_beats_dense_index_and_keeps_following_limitations():
    read = book([
        "概念索引 自律性 I-10 I-20 I-30 場 I-40 I-50 I-60",
        "この節では文化生産について説明する。",
        "自律性とは、場に固有の規範を保つことである。場の関係を説明する。",
        "ただし経済的な要求がなくなることを意味しない。これが議論の条件である。",
    ])
    result = page_passages(read, ["場", "自律性"], limit=6000, context_radius=1)
    assert result["citation"]["pdf_pages"] == [2, 3, 4]
    assert "ただし経済的" in result["excerpt"]
    assert "概念索引" not in result["excerpt"]
    assert "source_span_id" not in result["citation"]
    assert result["content_sha256"] == "sha"
    assert result["extraction_sha256"]


def test_blank_pages_do_not_consume_neighbouring_context():
    result = page_passages(book([
        "", "労働とは、生命を維持する活動力である。仕事とは異なる。",
        "仕事とは、世界を作る活動力である。活動とは多数性に対応する。",
        "三つの活動力の条件をここで説明する。これらは同一ではない。",
    ]), ["労働", "仕事", "活動"], limit=6000, context_radius=1)
    assert result["citation"]["pdf_pages"] == [2, 3, 4]


def test_index_only_is_unresolved_and_budget_is_honoured():
    assert not page_passages(book(["概念索引 労働 I-10 I-20"]), ["労働"],
                             limit=6000, context_radius=1)
    read = book(["労働とは生命のための活動である。" * 100])
    result = page_passages(read, ["労働"], limit=200, context_radius=1)
    assert len(result["excerpt"]) <= 200
    assert not result["excerpt_complete"]


def test_ocr_repair_is_never_evidence_until_image_confirmation():
    read = book(["労働とは生命を維持する活動である。条件を説明する。"])
    read["pages"][0].update(requires_image_review=True,
                            extraction_method="local_ocr_review_candidate")
    result = page_passages(read, ["労働"], limit=6000, context_radius=1)
    source = {"candidate_key": "C", "title": "資料", "excerpt": result["excerpt"],
              "provenance": {"source_level": "full_text", "content_acquisition": result}}
    verdict = {"status": "supports_subclaim", "rationale": "定義の明記",
               "citations": [{"candidate_key": "C", "quote": result["excerpt"]}]}
    assert not grounded_support(verdict, [source])
    clean = deepcopy(source)
    clean["provenance"]["content_acquisition"] = {}
    assert grounded_support(verdict, [clean])


def test_language_is_not_mistaken_for_writing_direction_or_ocr_accuracy():
    assert not text_issues("労働とは生命を維持する活動である。")
    assert not text_issues("国際連合安全保障理事会について述べる。")
    assert text_issues("漢字誤認識鰤難破壊翻機疑罐綴構読誤文字。これは本文である。")
    assert not text_issues("中华人民共和国汉字长句用于验证语言文字方向完全不同")


def test_uncertain_sentence_does_not_hide_clean_context_and_cannot_be_quoted():
    clean = "仕事とは世界を作り出す活動である。"
    damaged = "鰤罐綴鵬鱗鬱繭が誤認識された文章。"
    read = book([damaged + clean])
    result = page_passages(read, ["仕事"], limit=6000, context_radius=1)
    source = {"candidate_key": "C", "excerpt": result["excerpt"], "provenance": {
        "content_acquisition": result}}

    def verdict(quote):
        return {"status": "supports_subclaim", "rationale": "説明の引用", "citations": [
            {"candidate_key": "C", "quote": quote}]}

    assert grounded_support(verdict(clean), [source])
    assert not grounded_support(verdict(damaged), [source])
    assert not grounded_support(verdict(damaged + clean), [source])


def test_local_recovery_caps_pages_caches_results_and_preserves_original(tmp_path, monkeypatch):
    import fukamiru.authoring.passage_recovery as recovery

    source = tmp_path / "source.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=120, height=200)
        pdf.new_page(width=120, height=200)
        pdf.save(source)
    known = book(["鰤罐綴鵬鬱が崩れた本文。"] * 2)
    known.update(source_path=str(source),
                 content_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    selected = page_passages(known, ["本文"], limit=6000, context_radius=1)
    calls = []

    class Engine:
        def __init__(self, **kwargs):
            assert kwargs["timeout_seconds"] == 10

        def process(self, path, language):
            with pymupdf.open(path) as pdf:
                calls.append((len(pdf), language))
            return SimpleNamespace(pages=[SimpleNamespace(
                text="回復した本文の候補。", metadata={})])

    monkeypatch.setattr(recovery, "NDLOCRLiteEngine", Engine)
    settings = SimpleNamespace(
        paths=SimpleNamespace(runtime_root=tmp_path / "runtime"),
        translation=SimpleNamespace(
            academic_research_ocr_pages=1, academic_research_ocr_seconds=10),
        ocr=SimpleNamespace(ndlocr_command=str(tmp_path / "missing")),
    )
    old_directory = settings.paths.runtime_root / "source_reads" / recovery.digest({
        "sha256": known["content_sha256"], "pages": [selected["pages"][0]["number"]],
        "policy": "local-page-ocr-v1", "dpi": 200,
        "command": settings.ocr.ndlocr_command, "seconds": 10,
    })
    old_directory.mkdir(parents=True)
    (old_directory / "candidates.json").write_text(json.dumps([
        {"number": selected["pages"][0]["number"], "text": "旧モデルの候補。"},
    ]), encoding="utf-8")
    first = recovery.recover_pages(known, selected, settings)
    second = recovery.recover_pages(known, selected, settings)
    assert calls == [(1, "ja")]
    assert known["pages"][0]["text"] == "鰤罐綴鵬鬱が崩れた本文。"
    assert first["pages"][0]["requires_image_review"]
    assert not first["pages"][1].get("requires_image_review")
    assert first == second
