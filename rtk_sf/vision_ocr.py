"""
vision_ocr.py — Local bilingual OCR interceptor (EN + JA).

Extracts text from image files locally using open-source OCR engines,
bypassing Claude's multimodal vision token pricing entirely.

Primary engine:  PaddleOCR  (pip install paddleocr)
Fallback engine: EasyOCR    (pip install easyocr)

Token impact: a typical screenshot costs 800–8,000 vision tokens when passed
as an image. This module returns the extracted text (~50–300 tokens) instead.

Install:
    pip install paddleocr          # recommended — EN+JA, lightweight CRNN
    pip install easyocr            # fallback — PyTorch sequence pipeline
    pip install Pillow             # optional — grayscale preprocessing
"""

from __future__ import annotations

import os
from pathlib import Path

_SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
_MIN_CONFIDENCE = 0.45


# ---------------------------------------------------------------------------
# Optional preprocessing — grayscale + contrast boost for noisy images
# ---------------------------------------------------------------------------

def _preprocess(image_path: str) -> str:
    """
    Convert to grayscale and sharpen contrast. Returns path to a temp file.
    Falls back to original path if Pillow is not installed.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        import tempfile

        img = Image.open(image_path).convert("L")
        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        suffix = Path(image_path).suffix or ".png"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        img.save(tmp.name)
        return tmp.name
    except ImportError:
        return image_path
    except Exception:
        return image_path


# ---------------------------------------------------------------------------
# OCR engines
# ---------------------------------------------------------------------------

def _paddle_extract(image_path: str) -> list[str]:
    from paddleocr import PaddleOCR
    # 'japan' lang pack covers Kanji/Kana + alphanumeric English
    ocr = PaddleOCR(use_angle_cls=True, lang="japan", show_log=False)
    result = ocr.ocr(image_path, cls=True)
    if not result or not result[0]:
        return []
    lines = []
    for line in result[0]:
        text, confidence = line[1]
        if confidence > _MIN_CONFIDENCE:
            lines.append(text)
    return lines


def _easyocr_extract(image_path: str) -> list[str]:
    import easyocr
    reader = easyocr.Reader(["en", "ja"], gpu=False, verbose=False)
    result = reader.readtext(image_path)
    return [text for (_, text, conf) in result if conf > _MIN_CONFIDENCE]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_image_text(image_path: str, preprocess: bool = True) -> str:
    """
    Extract English and Japanese text from an image file using local OCR.

    Tries PaddleOCR first, falls back to EasyOCR if not installed.
    Optionally applies grayscale + contrast enhancement before OCR.

    Args:
        image_path:  Path to image file (.png .jpg .jpeg .bmp .tiff .webp)
        preprocess:  Apply grayscale/contrast boost (default True)

    Returns:
        Formatted Markdown text block ready to inject into Claude context,
        or an error/install-hint string if OCR cannot run.
    """
    path = Path(image_path)

    if not path.exists():
        return f"[rtk-sf OCR Error: File not found: {image_path}]"

    if path.suffix.lower() not in _SUPPORTED_EXT:
        supported = ", ".join(sorted(_SUPPORTED_EXT))
        return (
            f"[rtk-sf OCR Error: Unsupported format '{path.suffix}'. "
            f"Supported: {supported}]"
        )

    work_path = _preprocess(image_path) if preprocess else image_path
    engine = "unknown"
    lines: list[str] = []

    try:
        lines = _paddle_extract(work_path)
        engine = "PaddleOCR"
    except ImportError:
        try:
            lines = _easyocr_extract(work_path)
            engine = "EasyOCR"
        except ImportError:
            return (
                "[rtk-sf OCR: No OCR engine installed.]\n"
                "Install one of:\n"
                "  pip install paddleocr   ← recommended (EN + JA)\n"
                "  pip install easyocr     ← alternative fallback\n"
            )
    except Exception as exc:
        return f"[rtk-sf OCR Error ({engine}): {exc}]"
    finally:
        if work_path != image_path and Path(work_path).exists():
            try:
                os.unlink(work_path)
            except OSError:
                pass

    if not lines:
        return f"[rtk-sf OCR ({engine}): No text detected in {path.name}]"

    return (
        "```text\n"
        f"[rtk-sf: Local OCR Context — {engine}]\n"
        f"Source: {path.name} | Lines extracted: {len(lines)}\n"
        f"{'─' * 50}\n"
        + "\n".join(lines)
        + "\n```"
    )
