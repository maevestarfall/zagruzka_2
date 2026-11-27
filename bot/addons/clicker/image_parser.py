"""OpenCV + OCR visual macro compiler from marked screenshots.

Ported from the older build's ``bot/addons/issuance/image_parser.py`` into the
clicker addon (kept away from cards/issuance). The owner marks a screenshot on
the phone:

  * RED rectangle  → a button to CLICK   → step ``Клик: <OCR text>``
  * GREEN rectangle → an area to COPY/OCR → step ``Скопировать: <OCR text>``

The bot finds the coloured contours with OpenCV (HSV masks), crops each region,
runs Tesseract OCR (rus+eng) and returns ordered ``(type, text)`` steps in
natural reading order (top-to-bottom, left-to-right).
"""

from __future__ import annotations

import logging
import re
from typing import Any

try:
    import cv2
    import numpy as np
    _CV_OK = True
except ImportError:  # pragma: no cover - optional heavy dep
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _CV_OK = False

try:
    import pytesseract
except ImportError:
    pytesseract = None

logger = logging.getLogger(__name__)


def detect_colored_boxes(img_path: str, smart: bool = True) -> list[tuple[str, str]]:
    """Detect red/green rectangles in the image and OCR them.

    Returns an ordered list of ``(type, text)`` where ``type`` is ``"click"``
    (red boxes) or ``"copy"`` (green boxes). Empty list if OpenCV is missing or
    the image can't be read.

    ``smart`` (Умное зрение): when True, use Ramer–Douglas–Peucker polygon
    approximation (``cv2.approxPolyDP``) to keep only rectangle-ish closed
    contours (4..8 vertices), ignoring text, arrows, circles and other noise —
    robust even to hand-drawn frames. When False, fall back to plain contour
    bounding boxes.
    """
    if not _CV_OK:
        logger.warning("OpenCV not available — visual macro disabled")
        return []

    img = cv2.imread(img_path)
    if img is None:
        logger.error("Failed to load image at path: %s", img_path)
        return []

    h, w, _ = img.shape
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Red wraps around the HSV hue circle → two ranges.
    lower_red1 = np.array([0, 70, 50])
    upper_red1 = np.array([10, 255, 255])
    lower_red2 = np.array([170, 70, 50])
    upper_red2 = np.array([180, 255, 255])
    mask_red = cv2.inRange(hsv, lower_red1, upper_red1) + cv2.inRange(hsv, lower_red2, upper_red2)

    # Green.
    lower_green = np.array([35, 50, 50])
    upper_green = np.array([85, 255, 255])
    mask_green = cv2.inRange(hsv, lower_green, upper_green)

    contours_red, _ = cv2.findContours(mask_red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours_green, _ = cv2.findContours(mask_green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes: list[dict[str, Any]] = []
    for kind, contours in (("click", contours_red), ("copy", contours_green)):
        for c in contours:
            area = cv2.contourArea(c)
            if area < 150:  # noise
                continue
            if smart:
                # «Умное зрение»: оставляем только замкнутые контуры,
                # аппроксимирующиеся в 4..8 вершин (прямоугольник, в т.ч.
                # нарисованный от руки). Текст/стрелки/круги отсекаются.
                peri = cv2.arcLength(c, True)
                approx = cv2.approxPolyDP(c, 0.02 * peri, True)
                if not (4 <= len(approx) <= 8):
                    continue
            x, y, bw, bh = cv2.boundingRect(c)
            # Skip thin lines and full-image borders.
            if bw < 15 or bh < 15 or (bw > w * 0.95 and bh > h * 0.95):
                continue
            boxes.append({"type": kind, "box": (x, y, bw, bh)})

    # Natural reading order: top-to-bottom (row-banded), then left-to-right.
    boxes.sort(key=lambda b: (b["box"][1] // 25, b["box"][0]))

    steps: list[tuple[str, str]] = []
    for item in boxes:
        x, y, bw, bh = item["box"]
        pad = 3
        cx = max(0, x + pad)
        cy = max(0, y + pad)
        cw = max(5, bw - pad * 2)
        ch = max(5, bh - pad * 2)
        cropped = img[cy:cy + ch, cx:cx + cw]

        text = ""
        if pytesseract is not None and cropped.size > 0:
            cropped_rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
            # Try rus+eng, fall back to eng-only if the ru pack is missing,
            # and never crash if Tesseract itself is unavailable.
            for lang in ("rus+eng", "eng"):
                try:
                    text = pytesseract.image_to_string(cropped_rgb, lang=lang).strip()
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("OCR (%s) failed: %s", lang, e)
                    text = ""
            if text:
                text = re.sub(r"\s+", " ", text)
                text = re.sub(r"[^\w\sа-яА-ЯёЁ.,?!-]", "", text).strip()

        if not text:
            text = f"Кнопка_{x}_{y}" if item["type"] == "click" else f"Область_{x}_{y}"

        steps.append((item["type"], text))

    return steps


def steps_to_macro_text(steps: list[tuple[str, str]]) -> str:
    """Render ``(type, text)`` steps as the clicker macro text (Клик:/Скопировать:)."""
    lines: list[str] = []
    for kind, text in steps:
        if kind == "copy":
            lines.append(f"Скопировать: {text}")
        else:
            lines.append(f"Клик: {text}")
    return "\n".join(lines)


__all__ = ["detect_colored_boxes", "steps_to_macro_text"]
