from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError
from fukamiru.ocr.engines.mistral import MistralOCREngine
from fukamiru.ocr.engines.ndlocr_lite import NDLOCRLiteEngine
from fukamiru.ocr.models import OCRPage, OCRResult
from fukamiru.ocr.service import (
    PDFOCRService,
    _detect_residual_text_language,
    detect_pdf_language,
    normalize_language,
    routed_engine,
)


def _write_pdf(path: Path, text: str | None = None) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text:
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


class _FakeEngine:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def process(self, source: Path, language: str) -> OCRResult:
        self.calls.append(self.name)
        return OCRResult(
            source=source,
            source_sha256="0" * 64,
            language=language,
            engine=self.name,
            pages=(OCRPage(1, f"raw {language} OCR"),),
        )


class PDFOCRTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        local = self.root / "local.toml"
        local.write_text(
            "[paths]\n"
            f'state_db = "{(self.root / "state.db").as_posix()}"\n'
            f'knowledge_db = "{(self.root / "knowledge.db").as_posix()}"\n'
            f'runtime_root = "{(self.root / "runtime").as_posix()}"\n'
            f'corpus_root = "{(self.root / "corpus").as_posix()}"\n'
            f'ocr_root = "{(self.root / "ocr").as_posix()}"\n'
            f'knowledge_artifacts_root = "{(self.root / "kb").as_posix()}"\n'
            f'faiss_root = "{(self.root / "faiss").as_posix()}"\n',
            encoding="utf-8",
        )
        self.settings = load_settings(
            config_path=local,
            defaults_path=Path(__file__).resolve().parents[1] / "config" / "defaults.toml",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_language_router_is_explicit(self) -> None:
        self.assertEqual(routed_engine("fr"), "mistral")
        self.assertEqual(routed_engine("en"), "mistral")
        self.assertEqual(routed_engine("ja"), "ndlocr_lite")
        for language in ("ru", "zh", "ko", "de"):
            self.assertEqual(routed_engine(language), "mistral")
        self.assertEqual(normalize_language("zh-Hant"), "zh")
        self.assertEqual(normalize_language("de-DE"), "de")
        self.assertEqual(normalize_language("rus"), "ru")
        with self.assertRaisesRegex(FukamiruError, "ja、en、fr、ru、zh、ko、de"):
            routed_engine("es")

    def test_pdf_declared_language_is_high_confidence(self) -> None:
        source = self.root / "declared.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer._root_object[NameObject("/Lang")] = TextStringObject("ja-JP")
        with source.open("wb") as handle:
            writer.write(handle)

        decision = detect_pdf_language(source)

        self.assertTrue(decision.routable)
        self.assertEqual(decision.language, "ja")
        self.assertEqual(decision.method, "pdf_declared_language")
        self.assertGreaterEqual(decision.confidence, 0.90)
        self.assertEqual(decision.evidence["signals"][0]["source"], "catalog_lang")

    def test_blank_pdf_language_remains_unresolved(self) -> None:
        source = self.root / "english-name-is-not-evidence.pdf"
        _write_pdf(source)

        decision = detect_pdf_language(source)

        self.assertFalse(decision.routable)
        self.assertEqual(decision.language, "und")
        self.assertEqual(decision.method, "insufficient_language_evidence")

    def test_unsupported_declared_language_does_not_fall_through(self) -> None:
        source = self.root / "declared-spanish.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer._root_object[NameObject("/Lang")] = TextStringObject("es-ES")
        with source.open("wb") as handle:
            writer.write(handle)

        decision = detect_pdf_language(source)

        self.assertFalse(decision.routable)
        self.assertEqual(decision.method, "unsupported_pdf_language_metadata")

    def test_new_declared_languages_route_to_mistral(self) -> None:
        for tag, language in (("ru-RU", "ru"), ("zh-Hant", "zh"), ("ko-KR", "ko"), ("de-DE", "de")):
            with self.subTest(tag=tag):
                source = self.root / f"{language}.pdf"
                writer = PdfWriter()
                writer.add_blank_page(width=300, height=300)
                writer._root_object[NameObject("/Lang")] = TextStringObject(tag)
                with source.open("wb") as handle:
                    writer.write(handle)
                decision = detect_pdf_language(source)
                self.assertEqual(decision.language, language)
                self.assertTrue(decision.routable)
                self.assertEqual(routed_engine(decision.language), "mistral")

    def test_residual_text_detector_routes_only_strong_supported_signals(self) -> None:
        samples = {
            "ja": "これは日本語で書かれた研究資料です。歴史資料の分析と解釈を扱います。",
            "en": (
                "This is the record of the study and the evidence from the archive "
                "with the context of the original source."
            ),
            "fr": (
                "Le document de la recherche est dans les archives et les résultats "
                "de cette étude sont utiles pour une analyse précise."
            ),
            "ru": (
                "Это исследование и его результаты основаны на данных из архива. "
                "В статье мы рассматриваем новые источники и методы для анализа."
            ),
            "zh": "本文研究历史资料中的语言现象，并分析不同文献之间的联系与变化。",
            "ko": "이 연구는 역사 자료를 분석하고 문헌에 나타난 언어의 변화를 검토합니다.",
            "de": (
                "Die Untersuchung der historischen Quellen und die Analyse der Daten "
                "ist für das Verständnis der Entwicklung von Bedeutung."
            ),
        }

        decisions = {
            language: _detect_residual_text_language(text) for language, text in samples.items()
        }

        self.assertEqual(
            {key: value.language for key, value in decisions.items()},
            {
                "ja": "ja",
                "en": "en",
                "fr": "fr",
                "ru": "ru",
                "zh": "zh",
                "ko": "ko",
                "de": "de",
            },
        )
        self.assertTrue(all(decision.routable for decision in decisions.values()))

    def test_ambiguous_or_short_residual_text_waits_for_review(self) -> None:
        for text in (
            "中文", "歴史資料分析", "und der", "и в", "the und de la", "Историја и култура"
        ):
            with self.subTest(text=text):
                self.assertEqual(_detect_residual_text_language(text).language, "und")

    def test_existing_text_layer_uses_direct_extract(self) -> None:
        source = self.root / "text.pdf"
        _write_pdf(source, "Existing searchable text layer with enough characters.")
        calls: list[str] = []
        service = PDFOCRService(self.settings, engine_factory=lambda name: _FakeEngine(name, calls))
        result = service.process_file(source, language="fr", output_dir=self.root / "out")
        self.assertEqual(calls, ["direct"])
        self.assertEqual(result["engine"], "direct")
        self.assertIn("--- PAGE 1 ---", Path(result["text_path"]).read_text(encoding="utf-8"))

    def test_force_ocr_routes_text_pdf_to_requested_engine(self) -> None:
        source = self.root / "force.pdf"
        _write_pdf(source, "Existing searchable text layer with enough characters.")
        calls: list[str] = []
        service = PDFOCRService(self.settings, engine_factory=lambda name: _FakeEngine(name, calls))
        service.process_file(source, language="ja", output_dir=self.root / "out", force_ocr=True)
        self.assertEqual(calls, ["ndlocr_lite"])

    def test_existing_output_is_skipped_before_engine_call(self) -> None:
        source = self.root / "scan.pdf"
        _write_pdf(source)
        output = self.root / "out"
        output.mkdir()
        (output / "scan.txt").write_text("already done", encoding="utf-8")
        calls: list[str] = []
        result = PDFOCRService(
            self.settings, engine_factory=lambda name: _FakeEngine(name, calls)
        ).process_file(source, language="en", output_dir=output)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(calls, [])

    def test_mistral_requires_key_before_client_creation(self) -> None:
        source = self.root / "scan.pdf"
        _write_pdf(source)
        called = False

        def factory(_key: str) -> object:
            nonlocal called
            called = True
            return object()

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(FukamiruError, "MISTRAL_API_KEY"),
        ):
            MistralOCREngine(client_factory=factory).process(source, "fr")
        self.assertFalse(called)

    def test_mistral_preserves_french_characters_and_page_index(self) -> None:
        source = self.root / "french.pdf"
        _write_pdf(source)
        response = SimpleNamespace(
            pages=[
                SimpleNamespace(
                    index=0,
                    markdown="cœur, œuvre, élève, où, ça, æther",
                    dimensions=None,
                )
            ]
        )
        client = SimpleNamespace(ocr=SimpleNamespace(process=lambda **_kwargs: response))
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-only"}, clear=True):
            result = MistralOCREngine(client_factory=lambda _key: client).process(source, "fr")
        self.assertEqual(result.pages[0].page_number, 1)
        self.assertEqual(result.pages[0].text, "cœur, œuvre, élève, où, ça, æther")

    def test_ndlocr_json_is_converted_without_text_rewriting(self) -> None:
        source = self.root / "scan.pdf"
        _write_pdf(source)

        def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
            output = Path(args[args.index("--output") + 1])
            (output / "scan.json").write_text(
                json.dumps(
                    {
                        "pdfinfo": {"page_count": 1},
                        "contents": [
                            [{"text": "舊字體ゝ。", "isVertical": True, "confidence": 0.8}]
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            process = MagicMock()
            process.__enter__.return_value = process
            process.communicate.return_value = ("", "")
            process.returncode = 0
            return process

        with (
            patch(
                "fukamiru.ocr.engines.ndlocr_lite.subprocess.Popen", side_effect=fake_run,
            ) as child,
            patch("fukamiru.ocr.engines.ndlocr_lite._reserve_windows_cpu_for_server"),
            patch("fukamiru.ocr.engines.ndlocr_lite._windows_process_cpu_seconds"),
        ):
            result = NDLOCRLiteEngine(command="ndlocr-lite").process(source, "ja")
        self.assertEqual(child.call_count, 1)
        self.assertEqual(child.call_args.kwargs["encoding"], "utf-8")
        self.assertIn("--json-only", child.call_args.args[0])
        self.assertEqual(result.pages[0].text, "舊字體ゝ。")
        self.assertTrue(result.pages[0].metadata["lines"][0]["isVertical"])


if __name__ == "__main__":
    unittest.main()
