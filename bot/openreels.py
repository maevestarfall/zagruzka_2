# OpenReels integration — topic -> publish-ready vertical MP4.
# Co-authored with CoCo
"""Thin async client for a self-hosted **OpenReels** instance.

OpenReels (github.com/tsensei/OpenReels) is a Fastify + BullMQ pipeline
that turns a topic into a vertical MP4 (research -> script -> voiceover ->
visuals -> music -> captions -> assembly). It runs via ``docker compose
up`` and exposes a versioned REST API (default ``http://localhost:3000``):

* ``POST /api/v1/jobs``        -> create a job ``{topic, platform, provider, ...}``
* ``GET  /api/v1/jobs/{id}``   -> job meta incl. ``status`` + output info
* ``GET  /api/v1/events?jobId``-> SSE progress stream (we poll instead)

The bot doesn't host OpenReels; the owner runs it and points the bot at
its base URL (``📢 Публикация → 🎬 OpenReels → 🔧 URL сервера``). We keep
the client defensive: endpoints/field names vary across OpenReels
versions, so submit/poll/download each try a few known shapes and report
clear diagnostics instead of failing silently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("bot.openreels")

ProgressCb = Callable[[str, float], Awaitable[None]]

DEFAULT_BASE_URL = "http://localhost:3000"
API_PREFIX = "/api/v1"

# Job statuses OpenReels reports in meta.json / job API.
_DONE = {"completed", "complete", "done", "success", "succeeded", "finished", "ready"}
_FAIL = {"failed", "error", "errored", "cancelled", "canceled", "rejected"}

_MP4_RE = re.compile(r"\.(mp4|mov|webm|m4v)(\?|$)", re.IGNORECASE)


class OpenReelsError(RuntimeError):
    pass


def base_url() -> str:
    """Resolve the configured OpenReels base URL (no trailing slash)."""
    try:
        from .addons import state as addon_state

        url = addon_state.get("publication", "openreels_url", "") or ""
    except Exception:  # noqa: BLE001
        url = ""
    url = url or os.environ.get("OPENREELS_URL") or DEFAULT_BASE_URL
    return url.rstrip("/")


def api_key() -> str:
    try:
        from .addons import state as addon_state

        k = addon_state.get("publication", "openreels_key", "") or ""
    except Exception:  # noqa: BLE001
        k = ""
    return k or os.environ.get("OPENREELS_API_KEY") or ""


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    key = api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _find_mp4(obj: Any) -> str:
    """Recursively find the first mp4-ish path/url in a job meta payload."""
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, str):
            if _MP4_RE.search(o):
                out.append(o)
        elif isinstance(o, dict):
            for k in ("videoUrl", "video_url", "outputUrl", "output", "outputPath",
                      "video", "result", "url", "path", "file"):
                if k in o:
                    walk(o[k])
            for k, v in o.items():
                if k not in ("videoUrl", "video_url", "outputUrl", "output",
                             "outputPath", "video", "result", "url", "path", "file"):
                    walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return out[0] if out else ""


async def health() -> tuple[bool, str]:
    """Best-effort connectivity check. Returns ``(ok, human_message)``."""
    import httpx

    base = base_url()
    candidates = [f"{base}{API_PREFIX}/health", f"{base}/api/health", f"{base}/health", base]
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in candidates:
            try:
                r = await client.get(url, headers=_headers())
                if r.status_code < 500:
                    return True, f"OK ({url} → HTTP {r.status_code})"
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                continue
    return False, f"Не достучался до {base}. Последняя ошибка: {last if 'last' in dir() else 'n/a'}"


async def generate(
    topic: str,
    *,
    platform: str = "youtube",
    provider: str = "",
    archetype: str = "",
    direction: str = "",
    on_progress: ProgressCb | None = None,
    poll_interval: float = 5.0,
    poll_timeout: float = 1800.0,
) -> dict:
    """Create an OpenReels job for ``topic`` and poll until the MP4 is ready.

    Returns ``{"job_id", "status", "video_url", "download_urls", "meta"}``.
    ``video_url`` is the absolute URL to fetch the finished MP4 (empty if
    OpenReels only reports a server-local path — then ``download_urls`` lists
    the candidate URLs to try).
    """
    import httpx

    base = base_url()
    body: dict[str, Any] = {"topic": topic}
    if platform:
        body["platform"] = platform
    if provider:
        body["provider"] = provider
    if archetype:
        body["archetype"] = archetype
    if direction:
        body["direction"] = direction

    submit_urls = [
        f"{base}{API_PREFIX}/jobs",
        f"{base}/api/jobs",
        f"{base}{API_PREFIX}/generate",
    ]

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        sub_resp = None
        sub_err = ""
        for url in submit_urls:
            try:
                r = await client.post(url, headers=_headers(), json=body)
            except Exception as exc:  # noqa: BLE001
                sub_err = f"{url}: {exc}"
                continue
            if r.status_code < 400:
                sub_resp = r
                submit_url = url
                break
            sub_err = f"{url} → HTTP {r.status_code}: {r.text[:200]}"
        if sub_resp is None:
            raise OpenReelsError(
                f"Не удалось создать задачу в OpenReels ({base}). {sub_err}\n"
                "Проверь URL сервера в 📢 Публикация → 🎬 OpenReels → 🔧 URL."
            )

        try:
            created = sub_resp.json()
        except Exception:  # noqa: BLE001
            created = {}
        job_id = (
            created.get("id")
            or created.get("jobId")
            or created.get("job_id")
            or (created.get("data") or {}).get("id")
            or (created.get("job") or {}).get("id")
        )
        if not job_id:
            raise OpenReelsError(f"OpenReels принял запрос, но не вернул job id: {created!r}")
        logger.info("openreels: job %s created via %s", job_id, submit_url)

        status_urls = [
            f"{base}{API_PREFIX}/jobs/{job_id}",
            f"{base}/api/jobs/{job_id}",
        ]
        deadline = asyncio.get_event_loop().time() + poll_timeout
        ticks = 0.0
        last_status = ""
        meta: dict[str, Any] = created
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            meta = {}
            for surl in status_urls:
                try:
                    sr = await client.get(surl, headers=_headers())
                except Exception:  # noqa: BLE001
                    continue
                if sr.status_code < 400:
                    try:
                        meta = sr.json()
                    except Exception:  # noqa: BLE001
                        meta = {}
                    break
            data = meta.get("data") if isinstance(meta.get("data"), dict) else meta
            status = str(
                (data.get("status") or data.get("state") or "")
            ).lower()
            progress = data.get("progress") or data.get("percent") or 0
            stage = data.get("stage") or data.get("step") or status
            if status and status != last_status and on_progress is not None:
                last_status = status
                ticks += 1.0
                try:
                    await on_progress(str(stage), float(progress) if progress else ticks)
                except Exception:  # noqa: BLE001
                    pass
            if status in _DONE or _find_mp4(meta):
                mp4 = _find_mp4(meta)
                video_url = ""
                download_urls = [
                    f"{base}{API_PREFIX}/jobs/{job_id}/video",
                    f"{base}{API_PREFIX}/jobs/{job_id}/download",
                    f"{base}/outputs/{job_id}.mp4",
                    f"{base}/output/{job_id}.mp4",
                ]
                if mp4:
                    if mp4.startswith(("http://", "https://")):
                        video_url = mp4
                    else:
                        rel = mp4.lstrip("/")
                        download_urls.insert(0, f"{base}/{rel}")
                return {
                    "job_id": job_id,
                    "status": status or "completed",
                    "video_url": video_url,
                    "download_urls": download_urls,
                    "meta": meta,
                }
            if status in _FAIL:
                err = data.get("error") or data.get("message") or meta
                raise OpenReelsError(f"OpenReels job {job_id} провалился: {str(err)[:300]}")
        raise OpenReelsError(f"OpenReels: таймаут ожидания job {job_id} ({poll_timeout:.0f}s).")


async def download_to(dest_path: str, video_url: str, download_urls: list[str] | None = None) -> str:
    """Download the finished MP4 to ``dest_path``. Returns the path used.

    Tries ``video_url`` first, then each candidate in ``download_urls``.
    """
    import httpx

    urls = [u for u in ([video_url] + list(download_urls or [])) if u]
    if not urls:
        raise OpenReelsError("Нет URL для скачивания результата OpenReels.")
    last = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0), follow_redirects=True) as client:
        for url in urls:
            try:
                r = await client.get(url, headers=_headers())
            except Exception as exc:  # noqa: BLE001
                last = f"{url}: {exc}"
                continue
            if r.status_code < 400 and r.content:
                ctype = r.headers.get("content-type", "")
                if "json" in ctype and not _MP4_RE.search(url):
                    last = f"{url}: вернул JSON, не видео"
                    continue
                with open(dest_path, "wb") as f:
                    f.write(r.content)
                logger.info("openreels: downloaded %s -> %s (%d bytes)", url, dest_path, len(r.content))
                return dest_path
            last = f"{url} → HTTP {r.status_code}"
    raise OpenReelsError(f"Не удалось скачать MP4 OpenReels. Последняя ошибка: {last}")
