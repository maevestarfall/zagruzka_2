# Video dynamics analyzer with fast pixel-only video→prompt (analyze_fast).
# Co-authored with CoCo
"""«🎞 Анализ динамики видео» — frame-by-frame dynamics analysis.

Pipeline (per the user's spec):

1. **Fine pass** (interval 0.5…5 s, configurable): grab many low-res but
   legible screenshots, describe each with Vision and build a *reproduction
   prompt*, then narrate the **dynamics of the camera angle (ракурс)** and
   the **action sequence** across the frames.

2. **Coarse pass** (every ~5 s, configurable, runs in parallel): for each
   sampled frame compute pure-CV metrics — inter-frame motion (mean abs
   diff) and sharpness (variance of Laplacian) — to tell **real dynamics**
   apart from a frame that's just **motion-blurred** (blurry but nothing
   actually moving between samples).

Everything is best-effort: Vision is optional (falls back to CV metrics +
OCR), and a broken frame never aborts the run.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Low-res but legible — keeps Vision cheap and matches «плохое качество но
# различимо чтобы понять что изменилось».
_FRAME_WIDTH = 384
_JPEG_QUALITY = 55


@dataclass
class FrameSample:
    index: int
    t: float  # timestamp in seconds
    jpg_path: str
    sharpness: float = 0.0
    motion_from_prev: float = 0.0  # mean abs diff vs previous sample (fallback)
    flow_from_prev: float = 0.0  # dense optical-flow peak magnitude vs prev (px)
    description: str = ""  # Vision output (empty if no Vision)
    prompt: str = ""  # reproduction prompt


@dataclass
class AnalysisResult:
    duration: float = 0.0
    fps: float = 0.0
    fine_interval: float = 1.0
    coarse_interval: float = 5.0
    fine: list[FrameSample] = field(default_factory=list)
    coarse: list[FrameSample] = field(default_factory=list)
    narrative: str = ""  # LLM synthesis of angle/action dynamics
    coarse_verdict: str = ""  # real-dynamics vs motion-blur verdict
    note: str = ""  # any soft warning shown to the user


def _laplacian_var(gray: Any) -> float:
    """Sharpness proxy: variance of the Laplacian. Low → blurry."""
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _mean_abs_diff(gray_a: Any, gray_b: Any) -> float:
    import numpy as np

    if gray_a is None or gray_b is None:
        return 0.0
    if gray_a.shape != gray_b.shape:
        return 0.0
    return float(np.mean(np.abs(gray_a.astype("int16") - gray_b.astype("int16"))))


def _flow_peak(gray_a: Any, gray_b: Any) -> float:
    """Dense Farneback optical-flow *peak* magnitude (px) between two frames.

    The best lightweight signal for «real dynamics vs motion blur»: it
    measures actual per-pixel displacement, so a frame that's merely smeared
    (no displacement between samples) scores ~0, while a real moving subject
    or camera move scores high — even if the moving object is small (we take
    the 95th percentile, not the mean, to catch localized motion).
    """
    import cv2
    import numpy as np

    if gray_a is None or gray_b is None or gray_a.shape != gray_b.shape:
        return 0.0
    try:
        flow = cv2.calcOpticalFlowFarneback(
            gray_a, gray_b, None, 0.5, 3, 15, 3, 5, 1.2, 0
        )
        mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
        return float(np.percentile(mag, 95))
    except Exception:  # noqa: BLE001
        return 0.0


def _times(duration: float, interval: float, cap: int) -> list[float]:
    """Sample timestamps every ``interval`` s, evenly thinned to ``cap``."""
    if duration <= 0 or interval <= 0:
        return [0.0]
    ts = []
    t = 0.0
    while t < duration:
        ts.append(round(t, 2))
        t += interval
    if not ts:
        ts = [0.0]
    if len(ts) > cap:
        step = len(ts) / cap
        ts = [ts[int(i * step)] for i in range(cap)]
    return ts


def _extract(video_path: str, times: list[float], tag: str) -> list[FrameSample]:
    """Grab + downscale frames at the given timestamps. CV metrics filled in."""
    import cv2

    samples: list[FrameSample] = []
    cap = cv2.VideoCapture(video_path)
    prev_gray = None
    for i, t in enumerate(times):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        h, w = frame.shape[:2]
        if w > _FRAME_WIDTH:
            scale = _FRAME_WIDTH / float(w)
            frame = cv2.resize(frame, (_FRAME_WIDTH, max(1, int(h * scale))))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        import tempfile

        fd, jpg = tempfile.mkstemp(suffix=f"_{tag}{i}.jpg")
        os.close(fd)
        cv2.imwrite(jpg, frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
        s = FrameSample(index=i, t=t, jpg_path=jpg)
        with contextlib.suppress(Exception):
            s.sharpness = _laplacian_var(gray)
        with contextlib.suppress(Exception):
            s.motion_from_prev = _mean_abs_diff(prev_gray, gray)
        if prev_gray is not None:
            s.flow_from_prev = _flow_peak(prev_gray, gray)
        prev_gray = gray
        samples.append(s)
    cap.release()
    return samples


async def _vision_describe(jpg_path: str, idx: int) -> tuple[str, str]:
    """Vision describe + reproduction prompt for one frame. ('', '') on fail."""
    try:
        from ...groq_helpers import describe_image_with_groq, should_use_groq_override

        if not should_use_groq_override():
            return ("", "")
        with open(jpg_path, "rb") as fh:
            data = fh.read()
        caption = (
            "Это кадр из видео. По-русски и коротко: 1) РАКУРС/план камеры и "
            "композиция; 2) что в кадре и какое ДЕЙСТВИЕ; 3) затем строкой "
            "«ПРОМТ:» дай англоязычный image-промт, чтобы воспроизвести именно "
            "этот кадр (стиль, объекты, ракурс, свет)."
        )
        out = (await describe_image_with_groq(data, caption) or "").strip()
        if not out:
            return ("", "")
        prompt = ""
        low = out.lower()
        for marker in ("промт:", "prompt:"):
            if marker in low:
                prompt = out[low.index(marker) + len(marker):].strip()
                break
        return (out, prompt)
    except Exception:  # noqa: BLE001
        logger.debug("viddyn: vision describe failed", exc_info=True)
        return ("", "")


def _ocr(jpg_path: str) -> str:
    try:
        import pytesseract
        from PIL import Image

        return (pytesseract.image_to_string(Image.open(jpg_path)) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _classify_coarse(samples: list[FrameSample]) -> str:
    """Real-dynamics vs motion-blur verdict from CV metrics."""
    if len(samples) < 2:
        return "Слишком короткое видео для оценки динамики между сегментами."
    # Primary signal is optical-flow peak magnitude in PIXELS (actual
    # displacement), which cleanly separates real motion from a merely
    # smeared frame. We fall back to mean-abs-diff if flow is unavailable.
    FLOW_STATIC = 0.6  # px — below → scene essentially unchanged
    FLOW_REAL = 1.5  # px — at/above → clearly something moved/camera panned
    SHARP_BLUR = 40.0  # laplacian var below this → frame genuinely smeared
    use_flow = any(s.flow_from_prev > 0 for s in samples)
    lines: list[str] = []
    for s in samples[1:]:
        if use_flow:
            m = s.flow_from_prev
            lo, hi, unit = FLOW_STATIC, FLOW_REAL, "flow"
        else:
            m = s.motion_from_prev
            lo, hi, unit = 1.5, 3.0, "diff"
        blurry = s.sharpness < SHARP_BLUR
        if m >= hi and not blurry:
            verdict = "✅ реальная динамика (есть смещение + резко)"
        elif m >= hi and blurry:
            verdict = "⚠️ движение есть, но кадр смазан (motion blur)"
        elif m < lo and blurry:
            verdict = "🟡 смаз без реальной динамики (размыто, но сцена не меняется)"
        elif m < lo:
            verdict = "⏸ статика (резко, но сцена почти не меняется)"
        else:
            verdict = "〽️ слабая динамика (смещение небольшое)"
        lines.append(
            f"~{s.t:g}с: {unit}={m:.2f}, sharp={s.sharpness:.0f} → {verdict}"
        )
    return "\n".join(lines)


async def _synthesize(result: AnalysisResult) -> str:
    """LLM narrative of camera-angle dynamics + action sequence."""
    if not any(s.description for s in result.fine):
        return ""
    blocks = []
    for s in result.fine:
        d = s.description or _ocr(s.jpg_path)
        blocks.append(
            f"[t={s.t:g}с | sharp={s.sharpness:.0f} | "
            f"flow={s.flow_from_prev:.2f}px | diff={s.motion_from_prev:.1f}]\n{d}"
        )
    joined = "\n\n".join(blocks)
    prompt = (
        "Ниже — последовательные кадры одного видео с их описаниями и метриками "
        "(sharp = резкость, motion = насколько изменился кадр относительно "
        "предыдущего). Сделай по-русски:\n"
        "1) ДИНАМИКА РАКУРСА — как двигается/меняется камера (план, угол, "
        "приближение, панорама) по ходу видео;\n"
        "2) ПОСЛЕДОВАТЕЛЬНОСТЬ ЭКШНА — что происходит по шагам от кадра к кадру;\n"
        "3) короткий вывод, есть ли настоящая динамика или это статичная сцена.\n\n"
        f"--- кадры ---\n{joined[:24000]}"
    )
    try:
        from ...agent import oneshot_summary

        return (await oneshot_summary(prompt, purpose="viddyn") or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("viddyn: synthesis failed", exc_info=True)
        return ""


async def analyze(
    video_path: str, *, fine_interval: float, coarse_interval: float,
    max_fine: int, max_coarse: int,
) -> AnalysisResult:
    """Run the full two-pass analysis on a local video file."""
    import cv2

    result = AnalysisResult(
        fine_interval=fine_interval, coarse_interval=coarse_interval
    )
    cap = cv2.VideoCapture(video_path)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = (frames / fps) if fps > 0 and frames > 0 else 0.0
    result.fps = fps
    result.duration = duration

    # Fine pass — extract + Vision describe + reproduction prompt.
    fine_times = _times(duration, fine_interval, max_fine)
    result.fine = _extract(video_path, fine_times, "f")
    for s in result.fine:
        desc, prompt = await _vision_describe(s.jpg_path, s.index)
        s.description = desc
        s.prompt = prompt
        if not desc:
            ocr = _ocr(s.jpg_path)
            if ocr:
                s.description = f"(OCR) {ocr}"

    # Coarse pass — independent sampling, CV-metric dynamics verdict.
    coarse_times = _times(duration, coarse_interval, max_coarse)
    result.coarse = _extract(video_path, coarse_times, "c")
    result.coarse_verdict = _classify_coarse(result.coarse)

    # Narrative synthesis of angle/action.
    result.narrative = await _synthesize(result)
    if not any(s.description for s in result.fine):
        result.note = (
            "Vision-мозг (Groq) не настроен — описания кадров недоступны, "
            "показываю только CV-метрики динамики. Подключи в /setup → 🧠 "
            "Авто мозг → Мозг 2 (Groq vision)."
        )
    return result


def cleanup(result: AnalysisResult) -> None:
    for s in (*result.fine, *result.coarse):
        with contextlib.suppress(Exception):
            os.unlink(s.jpg_path)


async def analyze_fast(video_path: str, *, interval: float = 0.5, cap: int = 14) -> str:
    """Fast, pixel-only video→prompt (no Vision).

    Samples frames, measures optical-flow motion + sharpness, classifies the
    dynamics, and composes an English reproduction prompt describing motion,
    camera movement and pacing — so you can recreate a similar video. Quick.
    """
    import cv2

    cap_v = cv2.VideoCapture(video_path)
    fps = float(cap_v.get(cv2.CAP_PROP_FPS) or 0.0)
    frames = int(cap_v.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap_v.release()
    duration = (frames / fps) if fps > 0 and frames > 0 else 0.0
    times = _times(duration, interval, cap)
    samples = _extract(video_path, times, "fast")
    verdict = _classify_coarse(samples)
    flows = [s.flow_from_prev for s in samples if s.flow_from_prev]
    sharps = [s.sharpness for s in samples if getattr(s, "sharpness", 0)]
    avg_flow = (sum(flows) / len(flows)) if flows else 0.0
    peak_flow = max(flows) if flows else 0.0
    # Clean temp frames.
    for s in samples:
        with contextlib.suppress(Exception):
            os.unlink(s.jpg_path)
    if peak_flow < 0.6:
        motion = "static, locked-off camera, minimal movement"
    elif peak_flow < 1.5:
        motion = "slow gentle camera drift / subtle motion"
    elif peak_flow < 4.0:
        motion = "moderate dynamic motion, smooth camera moves"
    else:
        motion = "fast energetic motion, strong camera movement / action"
    sharp_note = ("crisp, sharp focus" if (sum(sharps) / len(sharps) if sharps else 0) >= 60
                  else "soft / slightly motion-blurred")
    prompt = (
        "Recreate a similar video. "
        f"Motion: {motion}. Dynamics: {verdict}. Look: {sharp_note}, "
        f"cinematic, same framing and camera angle as the reference. "
        f"Duration ~{duration:.0f}s, {fps:.0f} fps feel, smooth natural movement."
    )
    metrics = (f"\n\n📊 По пикселям: пик движения {peak_flow:.2f}px, "
               f"среднее {avg_flow:.2f}px, кадров {len(samples)}, {duration:.1f}с")
    return prompt + metrics


def build_report(result: AnalysisResult) -> str:
    lines: list[str] = ["🎞 Анализ динамики видео"]
    dur = f"{result.duration:.1f}с" if result.duration else "?"
    lines.append(
        f"Длительность: {dur} · FPS: {result.fps:.0f} · "
        f"шаг детально: {result.fine_interval:g}с · "
        f"шаг проверки: {result.coarse_interval:g}с"
    )
    if result.note:
        lines.append(result.note)
    if result.narrative:
        lines.append("")
        lines.append(result.narrative)
    if result.coarse_verdict:
        lines.append("")
        lines.append("— Проверка: реальная динамика vs смаз —")
        lines.append(result.coarse_verdict)
    # Reproduction prompts per fine frame.
    prompts = [(s.t, s.prompt) for s in result.fine if s.prompt]
    if prompts:
        lines.append("")
        lines.append("— Промты для воспроизведения по кадрам —")
        for t, p in prompts:
            lines.append(f"• {t:g}с: {p}")
    return "\n".join(lines)
