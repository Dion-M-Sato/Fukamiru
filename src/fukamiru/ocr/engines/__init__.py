"""OCR engine adapters."""

from fukamiru.ocr.engines.direct import DirectTextEngine
from fukamiru.ocr.engines.mistral import MistralOCREngine
from fukamiru.ocr.engines.ndlocr_lite import NDLOCRLiteEngine

__all__ = ["DirectTextEngine", "MistralOCREngine", "NDLOCRLiteEngine"]
