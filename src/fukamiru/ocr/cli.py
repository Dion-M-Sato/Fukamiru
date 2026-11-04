"""Command-line interface for language-routed PDF OCR."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError
from fukamiru.ocr.service import SUPPORTED_OCR_LANGUAGES, PDFOCRService


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="PDFを言語別OCRでUTF-8テキストへ変換します")
    value.add_argument("input", type=Path, help="PDFまたはPDFを含むディレクトリ")
    value.add_argument("--lang", required=True, choices=sorted(SUPPORTED_OCR_LANGUAGES))
    value.add_argument("--output", type=Path)
    value.add_argument("--recursive", action="store_true")
    value.add_argument("--force-ocr", action="store_true")
    value.add_argument("--overwrite", action="store_true")
    value.add_argument("--format", choices=("txt", "md"), default="txt")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    source = args.input.expanduser().resolve()
    if source.is_file():
        files = [source]
        base = source.parent
    elif source.is_dir():
        files = sorted(source.rglob("*.pdf") if args.recursive else source.glob("*.pdf"))
        base = source
    else:
        print(f"ERROR: 入力が見つかりません: {source}", file=sys.stderr)
        return 2
    if not files:
        print("ERROR: PDFが見つかりません", file=sys.stderr)
        return 2
    settings = load_settings()
    service = PDFOCRService(settings)
    failures = 0
    for pdf in files:
        relative_parent = pdf.parent.relative_to(base) if source.is_dir() else Path()
        output_dir = args.output / relative_parent if args.output else pdf.parent
        try:
            result = service.process_file(
                pdf,
                language=args.lang,
                output_dir=output_dir,
                force_ocr=args.force_ocr,
                overwrite=args.overwrite,
                output_format=args.format,
            )
            label = "SKIP" if result["status"] == "skipped" else "OK"
            print(f"{label}: {pdf} -> {result['text_path']}")
        except FukamiruError as exc:
            failures += 1
            print(f"ERROR: {pdf}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
