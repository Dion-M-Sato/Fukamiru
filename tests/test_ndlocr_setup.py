from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fukamiru.ocr.passage_segments import recognizer_arguments
from scripts.patch_ndlocr_json_only import patch_module

PDF_OUTPUT = '''\
def process(args):
    for pdf in ["input.pdf"]:
        output_pdf = getattr(args, "pdf_output", None)
        if not output_pdf:
            output_pdf = "output.pdf"
        embed_text_layer_pdf(output_pdf)
        print("Total PDF calculation time:", 0)
'''


def test_json_only_patch_suppresses_pdf_and_is_idempotent(tmp_path):
    module = tmp_path / "ocr.py"
    module.write_text(PDF_OUTPUT, encoding="utf-8")
    assert patch_module(module)
    once = module.read_bytes()
    assert not patch_module(module)
    assert module.read_bytes() == once
    embed = Mock()
    namespace = {"embed_text_layer_pdf": embed}
    exec(compile(module.read_text(encoding="utf-8"), str(module), "exec"), namespace)
    namespace["process"](SimpleNamespace(json_only=True))
    embed.assert_not_called()
    namespace["process"](SimpleNamespace(json_only=False))
    embed.assert_called_once_with("output.pdf")


@pytest.mark.parametrize("source", [
    "def unknown(): pass\n",
    PDF_OUTPUT.replace("embed_text_layer_pdf", "different_output"),
    PDF_OUTPUT.replace('print("Total PDF calculation time:", 0)', "pass"),
])
def test_json_only_patch_rejects_unknown_upstream_without_writing(tmp_path, source):
    module = tmp_path / "ocr.py"
    module.write_text(source, encoding="utf-8")
    before = module.read_bytes()
    with pytest.raises(RuntimeError):
        patch_module(module)
    assert module.read_bytes() == before


def test_passage_recognition_uses_installed_manifest_and_cached_weight(monkeypatch, tmp_path):
    package = tmp_path / "site-packages"
    weight = tmp_path / "models" / "current-recognizer.onnx"
    resolve = Mock(return_value=weight)
    monkeypatch.setitem(sys.modules, "ocr", SimpleNamespace(__file__=str(package / "ocr.py")))
    monkeypatch.setitem(sys.modules, "model_download", SimpleNamespace(
        MODELS={"rec_weights": ("current-recognizer.onnx", "expected-sha256")},
        get_model_path=resolve,
    ))
    args = recognizer_arguments()
    resolve.assert_called_once_with(
        "current-recognizer.onnx", "expected-sha256", package / "model",
    )
    assert args.rec_weights == str(weight)
    assert args.rec_classes == str(package / "config" / "NDLmoji.yaml")
    assert args.device == "cpu"
    assert not args.enable_tcy
