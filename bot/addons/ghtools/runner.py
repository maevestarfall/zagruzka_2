"""Runners for the GitHub-tools catalog.

* Lightweight tools (optical flow, PySceneDetect, blur) run *on the host*
  against a video the user sends — CPU only, pip-installable.
* Heavy tools (GPU / big models) are NOT run here; instead ``clone_probe``
  does a shallow ``git clone`` and reports what was pulled + the run recipe,
  so the user still gets «скачать гитхаб и посмотреть алгоритм».
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

_FRAME_WIDTH = 384
_MAX_FRAMES = 24


def _sample_grays(video_path: str, every: float):
    """Yield (t, gray_frame) sampled every ``every`` seconds, capped."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = (frames / fps) if fps > 0 and frames > 0 else 0.0
    out = []
    if duration <= 0:
        cap.release()
        return out, duration
    t = 0.0
    while t < duration and len(out) < _MAX_FRAMES:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            if w > _FRAME_WIDTH:
                sc = _FRAME_WIDTH / float(w)
                frame = cv2.resize(frame, (_FRAME_WIDTH, max(1, int(h * sc))))
            out.append((round(t, 2), cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
        t += every
    cap.release()
    return out, duration


def _run_opticalflow(video_path: str) -> str:
    import cv2
    import numpy as np

    grays, duration = _sample_grays(video_path, 1.0)
    if len(grays) < 2:
        return "Слишком короткое видео для optical flow."
    lines = [f"🌀 Optical Flow (Farneback), длительность {duration:.1f}с"]
    prev = None
    for t, g in grays:
        if prev is not None:
            flow = cv2.calcOpticalFlowFarneback(prev, g, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            peak = float(np.percentile(mag, 95))
            mean = float(mag.mean())
            tag = "✅ движение" if peak >= 1.5 else ("〽️ слабое" if peak >= 0.6 else "⏸ статика")
            lines.append(f"~{t:g}с: peak={peak:.2f}px, avg={mean:.2f}px → {tag}")
        prev = g
    return "\n".join(lines)


def _run_blur(video_path: str) -> str:
    import cv2

    grays, duration = _sample_grays(video_path, 1.0)
    if not grays:
        return "Не удалось прочитать кадры."
    lines = [f"🌫 Blur (variance-of-Laplacian), длительность {duration:.1f}с"]
    for t, g in grays:
        var = float(cv2.Laplacian(g, cv2.CV_64F).var())
        tag = "🌫 смазан" if var < 40 else "🔷 резкий"
        lines.append(f"~{t:g}с: sharp={var:.0f} → {tag}")
    return "\n".join(lines)


def _run_pyscenedetect(video_path: str) -> str:
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except Exception:  # noqa: BLE001
        try:
            # Older API fallback.
            from scenedetect import SceneManager, VideoManager  # type: ignore
            from scenedetect.detectors import ContentDetector  # type: ignore

            vm = VideoManager([video_path])
            sm = SceneManager()
            sm.add_detector(ContentDetector(threshold=27.0))
            vm.start()
            sm.detect_scenes(frame_source=vm)
            scenes = sm.get_scene_list()
            vm.release()
            return _fmt_scenes(scenes)
        except Exception as exc:  # noqa: BLE001
            return f"PySceneDetect недоступен: {exc}"
    video = open_video(video_path)
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=27.0))
    sm.detect_scenes(video)
    return _fmt_scenes(sm.get_scene_list())


def _fmt_scenes(scenes) -> str:
    if not scenes:
        return "✂️ PySceneDetect\nСмен сцен не найдено — видео без резких склеек (один план)."
    lines = [f"✂️ PySceneDetect — найдено сцен: {len(scenes)}"]
    for i, (start, end) in enumerate(scenes[:20], 1):
        lines.append(f"{i}. {start.get_timecode()} → {end.get_timecode()}")
    return "\n".join(lines)


_HOST_RUNNERS = {
    "opticalflow": _run_opticalflow,
    "blur": _run_blur,
    "pyscenedetect": _run_pyscenedetect,
}


async def run_on_video(runner_key: str, video_path: str) -> str:
    """Run a lightweight host runner (blocking CV work off the event loop)."""
    fn = _HOST_RUNNERS.get(runner_key)
    if fn is None:
        return "Этот инструмент не запускается на хосте — см. сценарий и клон репо."
    try:
        return await asyncio.to_thread(fn, video_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ghtools: runner failed")
        return f"Ошибка запуска: {exc}"


async def clone_probe(repo_url: str) -> str:
    """Shallow git-clone a heavy repo and report what was pulled + recipe head."""
    if shutil.which("git") is None:
        return "git не установлен на хосте — открой репозиторий по ссылке вручную."
    tmp = tempfile.mkdtemp(prefix="ghclone_")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo_url, tmp + "/repo",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return "git clone превысил таймаут (90с). Репозиторий большой — клонируй локально."
        if proc.returncode != 0:
            return f"git clone не удался:\n{(out or b'').decode(errors='ignore')[:400]}"
        repo_dir = tmp + "/repo"
        files = sorted(os.listdir(repo_dir))[:30]
        req = ""
        for name in ("requirements.txt", "pyproject.toml", "environment.yml"):
            p = os.path.join(repo_dir, name)
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    req = f"\n\n{name} (фрагмент):\n{fh.read()[:500]}"
                break
        listing = ", ".join(files)
        return (
            f"📥 Клонировано: {repo_url}\n"
            f"Файлы в корне: {listing}{req}"
        )
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp, ignore_errors=True)
