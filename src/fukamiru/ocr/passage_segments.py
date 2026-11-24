"""Standalone local NDLOCR recognizer helper, run in its configured environment.

No Fukamiru imports: the OCR Python environment owns the ONNX dependencies.
Its output is a review candidate, never a verified replacement of source text.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace


def recognizer_arguments() -> SimpleNamespace:
    """Use the installed release's model manifest, including its verified cache."""
    import ocr
    from model_download import MODELS, get_model_path

    package = Path(ocr.__file__).parent
    return SimpleNamespace(
        rec_weights=str(get_model_path(*MODELS["rec_weights"], package / "model")),
        rec_classes=str(package / "config" / "NDLmoji.yaml"),
        device="cpu", enable_tcy=False,
    )


def main() -> None:
    def deny(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Network is disabled in local passage recognition")

    socket.socket.connect = deny
    socket.create_connection = deny
    import cv2
    import numpy as np
    from ocr import get_recognizer

    recognizer = get_recognizer(recognizer_arguments())
    manifest = Path(sys.argv[1])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for page in payload:
        image = cv2.imdecode(np.fromfile(page["image_path"], dtype=np.uint8), cv2.IMREAD_COLOR)
        for line in page["lines"]:
            points = np.array(line["boundingBox"])
            x0, y0 = points.min(axis=0)
            x1, y1 = points.max(axis=0)
            crop = image[y0:y1, x0:x1]
            h, w = crop.shape[:2]
            if not w or h <= w or h / w < 16:
                continue
            grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            ink = (grey < 160).sum(axis=1)
            cuts = [0]
            maximum = max(120, int(w * 17))
            while h - cuts[-1] > maximum * 1.3:
                target = cuts[-1] + maximum
                low = max(cuts[-1] + 60, target - int(w * 1.2))
                high = min(h - 50, target + int(w * 1.2))
                cuts.append(min(range(low, high), key=lambda i: (ink[i], abs(i - target))))
            cuts.append(h)
            line["original_text"] = line["text"]
            line["segments"] = [{"y0": int(y0 + a), "y1": int(y0 + b),
                                 "text": recognizer.read(crop[a:b])}
                                for a, b in zip(cuts, cuts[1:], strict=False)]
            line["text"] = "".join(p["text"] for p in line["segments"])
    Path(sys.argv[2]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
