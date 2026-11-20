"""OCR providers for scanned PDFs.

Order of preference: PaddleOCR (if installed), then Tesseract via PyMuPDF. Each provider
returns words/lines as (page, x0, y0, x1, y1, text, confidence) in PDF points, so the
layout extractor treats OCR output exactly like native PDF text.
"""

import os
import threading
from dataclasses import dataclass

import pymupdf

from app.settings import settings

OCR_DPI = 200
Word = tuple[int, float, float, float, float, str, float]

_paddle_lock = threading.Lock()
_paddle_engine = None


@dataclass
class OcrResult:
    provider: str
    text: str
    words: list[Word]


def paddle_available() -> bool:
    if settings.ocr_provider not in ("auto", "paddle"):
        return False
    try:
        import paddleocr  # noqa: F401
    except Exception:
        return False
    return True


def tesseract_available() -> bool:
    if settings.ocr_provider not in ("auto", "tesseract"):
        return False
    try:
        return bool(pymupdf.get_tessdata(settings.tessdata_dir))
    except Exception:
        return False


def available_provider() -> str | None:
    if paddle_available():
        return "paddleocr"
    if tesseract_available():
        return "tesseract"
    return None


def run_ocr(document: pymupdf.Document) -> OcrResult | None:
    provider = available_provider()
    if provider == "paddleocr":
        return _paddle(document)
    if provider == "tesseract":
        return _tesseract(document)
    return None


def _paddle(document: pymupdf.Document) -> OcrResult:
    global _paddle_engine
    import numpy as np

    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    with _paddle_lock:
        if _paddle_engine is None:
            from paddleocr import PaddleOCR

            _paddle_engine = PaddleOCR(
                lang=settings.paddle_ocr_language,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        words: list[Word] = []
        texts: list[str] = []
        scale = 72 / OCR_DPI
        for index, page in enumerate(document):
            pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csRGB, alpha=False)
            image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)[:, :, ::-1].copy()
            result = _paddle_engine.predict(image)[0]
            page_lines = []
            for text, score, box in zip(result["rec_texts"], result["rec_scores"], result["rec_boxes"]):
                x0, y0, x1, y1 = (float(value) * scale for value in box)
                if text.strip():
                    words.append((index, x0, y0, x1, y1, text.strip(), float(score)))
                    page_lines.append((y0, x0, text.strip()))
            texts.append(_reading_order(page_lines))
    return OcrResult("paddleocr", "\n".join(texts), words)


def _tesseract(document: pymupdf.Document) -> OcrResult:
    words: list[Word] = []
    texts: list[str] = []
    for index, page in enumerate(document):
        textpage = page.get_textpage_ocr(language=settings.ocr_languages, dpi=300, full=True, tessdata=settings.tessdata_dir)
        texts.append(page.get_text(textpage=textpage))
        words.extend((index, *word[:5], 0.85) for word in page.get_text("words", textpage=textpage))
    return OcrResult("tesseract", "\n".join(texts), words)


def _reading_order(lines: list[tuple[float, float, str]], tolerance: float = 4.0) -> str:
    """Group OCR lines into visual rows (labels and values side by side), then left to right."""
    rows: list[list[tuple[float, float, str]]] = []
    for line in sorted(lines):
        if rows and abs(rows[-1][0][0] - line[0]) <= tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    return "\n".join(text for row in rows for _, _, text in sorted(row, key=lambda item: item[1]))
