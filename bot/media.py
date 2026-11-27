# Media generation (img2img/img2video/rmbg) with optional per-call model/size/aspect overrides.
# Co-authored with CoCo
"""Image-to-video / image-to-image / background-removal via pluggable providers.

Supported providers
-------------------
* **deAPI.ai** — img2video / img2img / img-rmbg via the deAPI client API.
* **OpenAI-compatible** — any host that speaks ``/v1/images/edits`` (image
  edits) and/or ``/v1/images/generations`` (txt2img). Works with OpenAI,
  Together, Groq, Fireworks, custom OpenAI-proxy gateways, etc.
* **fal.ai** — submit/poll JSON queue API (``queue/submit`` →
  ``queue/status``). Used for img2video / img2img.
* **Replicate** — synchronous predictions API. Used for img2img / img2video.

Storage model
-------------
The bot keeps up to 3 *media slots* in ``state.json``. Each slot is a
self-contained provider config::

    {
        "url": "https://api.deapi.ai",
        "api_key": "...",
        "name": "deapi",                   # auto-derived from URL
        "video_model": "Ltx2_19B_Dist_FP8",
        "photo_model": "Flux_2_Klein_4B_BF16",
        "rmbg_model":  "Ben2",
    }

A slot is *active for a task* when ``storage.set_active_media_slot(task,
slot_id)`` points at it. Tasks: ``"video"`` (img2video), ``"photo"``
(img2img), ``"rmbg"`` (background removal).

Provider auto-detection
-----------------------
``detect_provider_name(url)`` matches the URL against ``KNOWN_MEDIA_PROVIDERS``.
If the URL doesn't match any known host we fall back to the
``openai_compatible`` adapter — this is what lets the user point a slot at
*any* image-generation product without the bot insisting on deAPI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# ---- provider catalogue --------------------------------------------------

# Known providers — used to auto-name a slot once its URL is filled and
# to surface model-slug hints in the wizard. The actual request shape is
# dispatched by ``slot.name`` further down (see ``image_to_*`` helpers).

KNOWN_MEDIA_PROVIDERS: dict[str, dict] = {
    "deapi": {
        "url": "https://api.deapi.ai",
        "label": "deAPI.ai",
        "match_url": "deapi.ai",
        "hint": (
            "Decentralized AI inference. $5 free signup. Endpoints:\n"
            "• img2video — LTX-Video\n"
            "• img2img — Qwen-Image-Edit / Flux-2-Klein\n"
            "• img-rmbg — Ben2"
        ),
        "video_models": [
            ("Ltxv_13B_0_9_8_Distilled_FP8", "LTX-Video 0.9.8 13B — fast/cheap, 512×512, 30 fps"),
            ("Ltx2_19B_Dist_FP8",            "LTX-2 19B Distilled — up to 1024×1024, 24 fps"),
            ("Ltx2_3_22B_Dist_INT8",         "LTX-2.3 22B Distilled — best quality, also audio2video"),
        ],
        "photo_models": [
            ("QwenImageEdit_Plus_NF4", "Qwen-Image-Edit Plus — best for prompted edits"),
            ("Flux_2_Klein_4B_BF16",  "Flux 2 Klein 4B — faster, smaller (txt2img+img2img)"),
        ],
        "rmbg_models": [
            ("Ben2", "Ben2 — boundary-aware background removal"),
        ],
    },
    "openai": {
        "url": "https://api.openai.com",
        "label": "OpenAI (images)",
        "match_url": "api.openai.com",
        "hint": (
            "OpenAI Images API.\n"
            "• img2img — POST /v1/images/edits  (multipart: image, prompt, model, size)\n"
            "• txt2img — POST /v1/images/generations\n"
            "Background removal not supported — оставь rmbg-слот на deAPI или другой провайдер."
        ),
        "video_models": [],
        "photo_models": [
            ("gpt-image-1", "gpt-image-1 — фотореализм, поддерживает image edits"),
            ("dall-e-3",    "DALL·E 3 — только txt2img"),
            ("dall-e-2",    "DALL·E 2 — img2img / edits / variations"),
        ],
        "rmbg_models": [],
    },
    "openai_compatible": {
        "url": "",
        "label": "OpenAI-compatible (custom)",
        "match_url": "",
        "hint": (
            "Любой OpenAI-совместимый хост: Together, Groq, Fireworks, локальный "
            "прокси и т.п.\n"
            "Использует те же эндпоинты что и OpenAI: /v1/images/edits и "
            "/v1/images/generations.\n"
            "Если у тебя в URL уже есть /v1 — слот сам это поймёт и не задублирует."
        ),
        "video_models": [],
        "photo_models": [],
        "rmbg_models": [],
    },
    "fal": {
        "url": "https://fal.run",
        "label": "fal.ai",
        "match_url": "fal.run",
        "match_urls": ["fal.run", "fal.ai"],
        "hint": (
            "fal.ai queue API. Слот шлёт POST /<model> и поллит /queue/status. "
            "Auth: Key <api_key>."
        ),
        "video_models": [
            ("fal-ai/kling-video/v3/standard/image-to-video", "Kling v3 Std — premium quality"),
            ("fal-ai/wan/v2.2-a14b/image-to-video",           "Wan 2.2 — cheaper alternative"),
        ],
        "photo_models": [
            ("fal-ai/flux/dev/image-to-image", "Flux Dev img2img"),
            ("fal-ai/flux-pro/v1.1",           "Flux Pro v1.1 — txt2img"),
        ],
        "rmbg_models": [
            ("fal-ai/birefnet", "BiRefNet — background removal"),
        ],
    },
    "replicate": {
        "url": "https://api.replicate.com",
        "label": "Replicate",
        "match_url": "replicate.com",
        "hint": (
            "Replicate Predictions API. Слот шлёт POST /v1/predictions с "
            "{model, input}, поллит до status=succeeded."
        ),
        "video_models": [
            ("kwaivgi/kling-v1.6-standard", "Kling v1.6 standard — image-to-video"),
        ],
        "photo_models": [
            ("black-forest-labs/flux-dev",  "Flux Dev — txt2img / img2img"),
            ("black-forest-labs/flux-1.1-pro", "Flux 1.1 Pro — txt2img"),
        ],
        "rmbg_models": [
            ("cjwbw/rembg", "rembg — background removal"),
        ],
    },
    "weavy": {
        "url": "https://api.weavy.ai",
        "label": "Weavy.ai",
        "match_url": "weavy.ai",
        "hint": (
            "Weavy.ai — node-based AI media платформа c REST API.\n"
            "• img2video — выбираешь промпт, картинку, модель, "
            "длину и качество — на выходе mp4.\n"
            "Auth: Bearer <api_key>. Эндпоинты можно подправить "
            "под свой аккаунт в _weavy_submit_and_poll()."
        ),
        "video_models": [
            ("seedance",  "Seedance — плавное image-to-video, быстро/дёшево"),
            ("kling-2.1", "Kling 2.1 — премиальное качество движения"),
            ("veo-3",     "Veo 3 — кинематографичное, c аудио"),
            ("sora",      "Sora — длинные сцены, высокая согласованность"),
        ],
        "photo_models": [
            ("flux-2-pro",          "Flux 2 Pro — фотореализм (txt2img / img2img)"),
            ("stable-diffusion-xl", "SDXL — универсальный img2img"),
        ],
        "rmbg_models": [],
    },
}


def detect_provider_name(url: str) -> str:
    """Auto-derive a slot name from its URL.

    Returns the matching provider key from :data:`KNOWN_MEDIA_PROVIDERS`,
    or ``"openai_compatible"`` for any unknown host (so the bot stops
    insisting on deAPI when the user wires in a different product).
    Empty string only when URL itself is empty.
    """
    if not url:
        return ""
    url_l = url.lower().strip()
    for key, meta in KNOWN_MEDIA_PROVIDERS.items():
        # A provider may declare several host aliases (e.g. fal.ai is the
        # marketing site, fal.run the API host) — match any of them.
        aliases = meta.get("match_urls") or ([meta.get("match_url")] if meta.get("match_url") else [])
        for match in aliases:
            if match and match in url_l:
                return key
    # Fallback — assume an OpenAI-compatible image API.
    return "openai_compatible"


def _provider_family(name: str) -> str:
    """Group provider names into adapter families.

    Returns one of: ``"deapi"``, ``"openai"``, ``"fal"``, ``"replicate"``.
    Unknown / custom names default to ``"openai"`` (most universal shape).
    """
    n = (name or "").lower()
    if n == "deapi":
        return "deapi"
    if n in ("openai", "openai_compatible"):
        return "openai"
    if n == "fal":
        return "fal"
    if n == "replicate":
        return "replicate"
    if n == "weavy":
        return "weavy"
    return "openai"


# ---- defaults / quality mapping -----------------------------------------

# LTXv-0.9.8 is single-step (must use steps=1, fps=30). LTX-2 (19B/22B)
# wants multi-step and 24 fps. Anything outside the model's range will
# be 422'd by deAPI, so callers should pick a quality-friendly model
# (i.e. send 480p to LTXv-0.9.8, 1080p to LTX-2).
VIDEO_QUALITY_MAP: dict[str, tuple[int, int, int, int, int]] = {
    # quality -> (width, height, frames, fps, steps)
    "480":  (512, 512, 60, 30, 1),    # LTXv-0.9.8 friendly
    "720":  (768, 768, 60, 30, 1),    # LTXv-0.9.8 friendly
    "1080": (1024, 1024, 120, 24, 8), # LTX-2 friendly (max 1024)
    "2k":   (1024, 1024, 120, 24, 8), # cap at model max — 2K is aspirational
    "auto": (512, 512, 60, 30, 1),    # safe default
}


# ---- Weavy.ai presets ---------------------------------------------------

# Weavy expects a human resolution string + clip duration in seconds,
# not a frames/fps tuple. These maps drive the guided Weavy video flow.
WEAVY_RESOLUTION_MAP: dict[str, str] = {
    "480":  "480p",
    "720":  "720p",
    "1080": "1080p",
    "2k":   "1440p",
    "auto": "720p",
}

# Selectable clip lengths (seconds) shown in the Weavy length picker.
WEAVY_DURATIONS: tuple[int, ...] = (3, 5, 8, 10)
WEAVY_DEFAULT_DURATION = 5
WEAVY_DEFAULT_QUALITY = "720"

# Built-in default custom model — seeded into the user-editable Weavy
# model list on first use. The user can delete it or paste their own
# models from the "🤖 Модель" picker ("➕ Добавить").
WEAVY_GROK_MODEL = "https://fal.ai/models/xai/grok-imagine-video/v1.5/image-to-video"

# The model selected by default in the Weavy panel. Per the owner's
# request this is the Grok image-to-video model URL above.
WEAVY_DEFAULT_MODEL = WEAVY_GROK_MODEL


# ---- data types ----------------------------------------------------------


@dataclass(frozen=True)
class MediaSlot:
    slot_id: str  # "1" | "2" | "3"
    url: str
    api_key: str
    name: str
    video_model: str
    photo_model: str
    rmbg_model: str
    # Universal "custom request" template (parsed from a pasted cURL / docs
    # request). When present with a URL, the slot bypasses the built-in
    # provider families and runs this template verbatim — this is what lets
    # the bot talk to *any* site, not just the pre-taught providers.
    custom: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        # A custom-template slot is "configured" as soon as it has a request
        # URL — the API key may live inside the template's headers, so we
        # don't force a separate api_key for custom slots.
        if self.custom and self.custom.get("url"):
            return True
        return bool(self.url and self.api_key)

    @property
    def is_custom(self) -> bool:
        return bool(self.custom and self.custom.get("url"))


class MediaError(RuntimeError):
    pass


ProgressCb = Callable[[str, float], Awaitable[None]]


# ---- slot loader ---------------------------------------------------------


def _load_slot(task: str) -> MediaSlot:
    """Pull the active slot for ``task`` (``"video"``/``"photo"``/``"rmbg"``).

    Falls back to env vars when no slot is configured (back-compat with
    the simpler initial integration).
    """
    from .storage import storage  # local import — avoids circular load

    slot = storage.get_active_media_slot(task)
    if slot is None:
        # Back-compat: synthesise a slot from env so the existing
        # DEAPI_API_KEY-only deploy keeps working.
        env_key = os.environ.get("DEAPI_API_KEY") or os.environ.get("DEAPI_KEY")
        if not env_key:
            raise MediaError(
                "Media-провайдер не настроен. /setup → 🎬 Мозг для видео и фото"
            )
        defaults = KNOWN_MEDIA_PROVIDERS["deapi"]
        return MediaSlot(
            slot_id="env",
            url=defaults["url"],
            api_key=env_key,
            name="deapi",
            video_model="Ltxv_13B_0_9_8_Distilled_FP8",
            photo_model="QwenImageEdit_Plus_NF4",
            rmbg_model="Ben2",
        )
    if not slot.configured:
        raise MediaError(
            f"Media-слот {slot.slot_id} не настроен (нет URL или API key). "
            "Открой /setup → 🎬 Мозг для видео и фото и дозаполни."
        )
    return slot


# ============================================================================
# deAPI adapter (original)
# ============================================================================


async def _deapi_submit_and_poll(
    slot: MediaSlot,
    op: str,
    data: dict[str, str],
    files: dict,
    *,
    on_progress: ProgressCb | None = None,
    poll_timeout: float = 600.0,
    poll_interval: float = 4.0,
) -> dict:
    """Submit a deAPI request and poll until ``status`` is ``done``."""
    headers = {
        "Authorization": f"Bearer {slot.api_key}",
        "Accept": "application/json",
    }
    submit_url = f"{slot.url.rstrip('/')}/api/v1/client/{op}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(submit_url, headers=headers, data=data, files=files)
        if resp.status_code >= 400:
            raise MediaError(f"{op} submit HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            request_id = resp.json()["data"]["request_id"]
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"{op}: unexpected submit response {resp.text[:500]}") from exc
        logger.info("media[deapi]: %s submitted id=%s slot=%s", op, request_id, slot.slot_id)
        return await _deapi_poll(client, slot, request_id, headers, op,
                                 on_progress=on_progress,
                                 poll_timeout=poll_timeout,
                                 poll_interval=poll_interval)


async def _deapi_poll(
    client: httpx.AsyncClient,
    slot: MediaSlot,
    request_id: str,
    headers: dict[str, str],
    op: str,
    *,
    on_progress: ProgressCb | None,
    poll_timeout: float,
    poll_interval: float,
) -> dict:
    deadline = asyncio.get_event_loop().time() + poll_timeout
    last_reported = -10.0
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(
            f"{slot.url.rstrip('/')}/api/v1/client/request-status/{request_id}",
            headers=headers,
        )
        if resp.status_code >= 400:
            raise MediaError(f"{op} status HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json().get("data") or {}
        status = payload.get("status") or "processing"
        progress = float(payload.get("progress") or 0.0)
        if status == "done":
            return payload
        if status == "error":
            raise MediaError(f"{op} failed: {payload!r}")
        if on_progress is not None and (progress - last_reported >= 5.0 or status != "processing"):
            last_reported = progress
            try:
                await on_progress(status, progress)
            except Exception:  # noqa: BLE001
                logger.debug("on_progress failed", exc_info=True)
        await asyncio.sleep(poll_interval)
    raise MediaError(f"{op} timeout after {poll_timeout}s (request_id={request_id})")


# ============================================================================
# OpenAI-compatible adapter (img2img via /v1/images/edits, txt2img via
# /v1/images/generations). Tested against OpenAI, also works for any
# proxy / gateway that mimics the same surface.
# ============================================================================


def _openai_base(url: str) -> str:
    """Return the host root without /v1 suffix (we add the path explicitly)."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")]
    return u


async def _openai_image_edit(
    slot: MediaSlot,
    image_bytes: bytes,
    prompt: str,
    *,
    model: str | None = None,
    size: str | None = None,
    on_progress: ProgressCb | None = None,
) -> tuple[str, str]:
    """img2img via POST /v1/images/edits.

    Returns ``(png_data_url_or_url, jpg_data_url_or_url)``. We always
    request the response in ``b64_json`` so the caller can write the
    bytes to a temp file, but we wrap the bytes in a ``data:`` URL so
    the rest of the bot can keep its "url-in-string" contract.
    """
    base = _openai_base(slot.url)
    submit_url = f"{base}/v1/images/edits"
    headers = {"Authorization": f"Bearer {slot.api_key}"}
    data = {
        "prompt": prompt,
        "model": model or slot.photo_model or "gpt-image-1",
        "n": "1",
        "size": size or "1024x1024",
        "response_format": "b64_json",
    }
    files = {"image": ("input.png", image_bytes, "image/png")}
    if on_progress is not None:
        try:
            await on_progress("processing", 5.0)
        except Exception:  # noqa: BLE001
            pass
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(submit_url, headers=headers, data=data, files=files)
        if resp.status_code >= 400:
            raise MediaError(
                f"images/edits HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
    items = payload.get("data") or []
    if not items:
        raise MediaError(f"images/edits returned no data: {payload!r}")
    item = items[0]
    if "b64_json" in item and item["b64_json"]:
        png_data_url = f"data:image/png;base64,{item['b64_json']}"
        if on_progress is not None:
            try:
                await on_progress("done", 100.0)
            except Exception:  # noqa: BLE001
                pass
        return png_data_url, ""
    if "url" in item and item["url"]:
        if on_progress is not None:
            try:
                await on_progress("done", 100.0)
            except Exception:  # noqa: BLE001
                pass
        return item["url"], ""
    raise MediaError(f"images/edits item has no url/b64_json: {item!r}")


# ============================================================================
# fal.ai adapter — queue submit/poll
# ============================================================================


async def _fal_submit_and_poll(
    slot: MediaSlot,
    model_slug: str,
    payload_json: dict,
    *,
    on_progress: ProgressCb | None = None,
    poll_timeout: float = 600.0,
    poll_interval: float = 3.0,
) -> dict:
    base = slot.url.rstrip("/")
    submit_url = f"{base}/{model_slug.lstrip('/')}"
    headers = {
        "Authorization": f"Key {slot.api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(submit_url, headers=headers, json=payload_json)
        if resp.status_code >= 400:
            raise MediaError(f"fal submit HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        # fal's sync endpoint returns final result directly; queue endpoints
        # return {request_id, status_url, response_url}.
        if "request_id" not in body:
            return body
        status_url = body.get("status_url") or ""
        response_url = body.get("response_url") or ""
        deadline = asyncio.get_event_loop().time() + poll_timeout
        last_reported = -10.0
        while asyncio.get_event_loop().time() < deadline:
            sresp = await client.get(status_url, headers=headers)
            if sresp.status_code >= 400:
                raise MediaError(f"fal status HTTP {sresp.status_code}: {sresp.text[:300]}")
            sbody = sresp.json()
            status = (sbody.get("status") or "").upper()
            progress = float(sbody.get("queue_position") or 0.0)
            if status == "COMPLETED":
                rresp = await client.get(response_url, headers=headers)
                if rresp.status_code >= 400:
                    raise MediaError(f"fal result HTTP {rresp.status_code}: {rresp.text[:300]}")
                return rresp.json()
            if status in ("FAILED", "ERROR"):
                raise MediaError(f"fal failed: {sbody!r}")
            if on_progress is not None and progress != last_reported:
                last_reported = progress
                try:
                    await on_progress(status.lower() or "processing", min(progress, 100.0))
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(poll_interval)
        raise MediaError(f"fal timeout after {poll_timeout}s")


# ============================================================================
# Replicate adapter — predictions API
# ============================================================================


async def _replicate_predict(
    slot: MediaSlot,
    model_ref: str,
    input_payload: dict,
    *,
    on_progress: ProgressCb | None = None,
    poll_timeout: float = 600.0,
    poll_interval: float = 3.0,
) -> dict:
    base = slot.url.rstrip("/")
    headers = {
        "Authorization": f"Token {slot.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Replicate accepts "owner/model" via /v1/models/.../predictions
    if "/" in model_ref and ":" not in model_ref:
        submit_url = f"{base}/v1/models/{model_ref}/predictions"
        body = {"input": input_payload}
    else:
        submit_url = f"{base}/v1/predictions"
        body = {"version": model_ref, "input": input_payload}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(submit_url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise MediaError(f"replicate submit HTTP {resp.status_code}: {resp.text[:500]}")
        pred = resp.json()
        get_url = pred.get("urls", {}).get("get") or f"{base}/v1/predictions/{pred.get('id')}"
        deadline = asyncio.get_event_loop().time() + poll_timeout
        last_reported = -10.0
        while asyncio.get_event_loop().time() < deadline:
            sresp = await client.get(get_url, headers=headers)
            if sresp.status_code >= 400:
                raise MediaError(f"replicate status HTTP {sresp.status_code}: {sresp.text[:300]}")
            sbody = sresp.json()
            status = (sbody.get("status") or "").lower()
            if status == "succeeded":
                return sbody
            if status in ("failed", "canceled"):
                raise MediaError(f"replicate failed: {sbody.get('error') or sbody!r}")
            if on_progress is not None:
                # Replicate doesn't report numeric progress; report status only.
                try:
                    await on_progress(status or "processing", last_reported + 1.0)
                except Exception:  # noqa: BLE001
                    pass
                last_reported += 1.0
            await asyncio.sleep(poll_interval)
        raise MediaError(f"replicate timeout after {poll_timeout}s")


# ============================================================================
# Weavy.ai adapter (guided img2video: prompt + image + model + length + quality)
# ============================================================================


def _load_weavy_slot() -> "MediaSlot":
    """Resolve the Weavy.ai slot for the guided video flow.

    Lookup order:
      1. Any configured media slot whose provider name is ``weavy``
         (added via /setup → 🎬 Мозг для видео и фото, URL содержит
         ``weavy.ai``).
      2. Env fallback ``WEAVY_API_KEY`` (+ optional ``WEAVY_API_URL``).

    Raises :class:`MediaError` with a setup hint when nothing is found.
    """
    from .storage import storage  # local import — avoids circular load

    for sid in storage.MEDIA_SLOT_IDS:
        slot = storage.get_media_slot(sid)
        if slot is not None and slot.name == "weavy" and slot.configured:
            return slot

    env_key = os.environ.get("WEAVY_API_KEY") or os.environ.get("WEAVY_KEY")
    if env_key:
        return MediaSlot(
            slot_id="weavy-env",
            url=os.environ.get("WEAVY_API_URL") or KNOWN_MEDIA_PROVIDERS["weavy"]["url"],
            api_key=env_key,
            name="weavy",
            video_model=os.environ.get("WEAVY_VIDEO_MODEL") or WEAVY_DEFAULT_MODEL,
            photo_model="",
            rmbg_model="",
        )

    raise MediaError(
        "Weavy.ai не настроен. Открой /setup → 🎬 Мозг для видео и фото, "
        "добавь слот c URL https://api.weavy.ai и своим API-ключом "
        "(или задай переменную окружения WEAVY_API_KEY)."
    )


async def _weavy_submit_and_poll(
    slot: "MediaSlot",
    payload: dict,
    *,
    on_progress: ProgressCb | None = None,
    poll_timeout: float = 600.0,
    poll_interval: float = 4.0,
) -> dict:
    """Submit a Weavy.ai job and poll until it finishes.

    Weavy exposes an async REST job API: ``POST {url}/v1/jobs`` returns a
    job id, and ``GET {url}/v1/jobs/{id}`` reports status until the result
    is ready. Both the submit path and the result-extraction below cover
    the common response shapes; tweak them here if your Weavy account uses
    different endpoints.
    """
    base = slot.url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {slot.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    submit_url = f"{base}/v1/jobs"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(submit_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise MediaError(f"weavy submit HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        job_id = (
            body.get("id")
            or body.get("job_id")
            or (body.get("data") or {}).get("id")
        )
        if not job_id:
            raise MediaError(f"weavy: unexpected submit response {resp.text[:500]}")
        logger.info("media[weavy]: job submitted id=%s slot=%s", job_id, slot.slot_id)

        status_url = f"{base}/v1/jobs/{job_id}"
        deadline = asyncio.get_event_loop().time() + poll_timeout
        last_reported = -10.0
        while asyncio.get_event_loop().time() < deadline:
            sresp = await client.get(status_url, headers=headers)
            if sresp.status_code >= 400:
                raise MediaError(f"weavy status HTTP {sresp.status_code}: {sresp.text[:300]}")
            sbody = sresp.json()
            data = sbody.get("data") or sbody
            status = (data.get("status") or "processing").lower()
            progress = float(data.get("progress") or 0.0)
            if status in ("done", "succeeded", "completed", "success"):
                return data
            if status in ("error", "failed", "canceled", "cancelled"):
                raise MediaError(f"weavy job failed: {data.get('error') or data!r}")
            if on_progress is not None and (progress - last_reported >= 5.0):
                last_reported = progress
                try:
                    await on_progress(status, progress)
                except Exception:  # noqa: BLE001
                    logger.debug("on_progress failed", exc_info=True)
            await asyncio.sleep(poll_interval)
        raise MediaError(f"weavy timeout after {poll_timeout}s (job_id={job_id})")


def _weavy_extract_url(result: dict) -> str:
    """Pull a video URL out of a finished Weavy job payload."""
    candidates = [
        result.get("video_url"),
        result.get("url"),
        result.get("result_url"),
        (result.get("video") or {}).get("url") if isinstance(result.get("video"), dict) else None,
        (result.get("output") or {}).get("url") if isinstance(result.get("output"), dict) else None,
    ]
    out = result.get("output")
    if isinstance(out, list) and out:
        first = out[0]
        candidates.append(first.get("url") if isinstance(first, dict) else first)
    for c in candidates:
        if c:
            return str(c)
    raise MediaError(f"weavy done but no video url in payload: {result!r}")


# «Живая карта проекта»: @traced — no-op при LIVEMAP_ENABLED=0 (ТЗ 5.3).
from .addons.projectmap.tracer import traced as _lm_traced


@_lm_traced("video_gen.weavy")
async def weavy_image_to_video(
    image_bytes: bytes,
    prompt: str,
    *,
    model: str,
    quality: str = WEAVY_DEFAULT_QUALITY,
    duration: int = WEAVY_DEFAULT_DURATION,
    on_progress: ProgressCb | None = None,
) -> str:
    """Animate ``image_bytes`` via Weavy.ai. Returns the resulting MP4 URL.

    Driven by the guided Telegram flow: caller picks ``prompt`` (текст),
    the attached image, ``model`` (модель), ``duration`` (длина, сек) and
    ``quality`` (качество → resolution). Auth/URL come from the Weavy slot
    (or ``WEAVY_API_KEY`` env).
    """
    slot = _load_weavy_slot()
    resolution = WEAVY_RESOLUTION_MAP.get(quality, WEAVY_RESOLUTION_MAP["auto"])
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "type": "image-to-video",
        "model": model or slot.video_model or WEAVY_DEFAULT_MODEL,
        "prompt": prompt or "Cinematic gentle motion, smooth camera move, soft natural light",
        "image": f"data:image/jpeg;base64,{b64}",
        "duration": int(duration),
        "resolution": resolution,
    }
    result = await _weavy_submit_and_poll(slot, payload, on_progress=on_progress)
    return _weavy_extract_url(result)


# ============================================================================
# Public API — dispatches by provider family
# ============================================================================


@_lm_traced("video_gen")
async def image_to_video(
    image_bytes: bytes,
    prompt: str,
    *,
    quality: str = "auto",
    negative_prompt: str | None = None,
    model: str | None = None,
    aspect: str | None = None,
    on_progress: ProgressCb | None = None,
) -> str:
    """Animate ``image_bytes`` with ``prompt`` at ``quality``. Returns MP4 URL.

    Optional ``model`` / ``aspect`` ("9:16"|"16:9") override the active
    video slot's defaults (used by the algo-slots presets).
    """
    slot = _load_slot("video")
    w, h, frames, fps, steps = VIDEO_QUALITY_MAP.get(quality, VIDEO_QUALITY_MAP["auto"])
    # Apply aspect override by orienting w/h.
    if aspect == "9:16" and w > h:
        w, h = h, w
    elif aspect == "16:9" and h > w:
        w, h = h, w

    # Universal custom-template slot (pasted cURL / docs request) — runs for
    # *any* site, no built-in adapter required.
    if slot.is_custom:
        from . import media_custom

        ctx = media_custom.build_context(
            image_bytes=image_bytes,
            prompt=prompt or "Cinematic gentle motion, soft natural light",
            negative_prompt=negative_prompt or "",
            model=model or slot.video_model,
            api_key=slot.api_key,
            width=w, height=h, frames=frames, fps=fps, steps=steps,
            duration=WEAVY_DEFAULT_DURATION, quality=quality, aspect=aspect or "",
        )
        return await media_custom.run_custom(
            slot.custom, ctx, task="video", on_progress=on_progress,
        )

    family = _provider_family(slot.name)
    _vmodel = model or slot.video_model

    if family == "deapi":
        seed = random.randint(1, 2**31 - 1)
        data = {
            "prompt": prompt or "Cinematic gentle motion, soft natural light",
            "model": _vmodel,
            "width": str(w),
            "height": str(h),
            "frames": str(frames),
            "fps": str(fps),
            "steps": str(steps),
            "guidance": "7.5",
            "seed": str(seed),
        }
        if negative_prompt:
            data["negative_prompt"] = negative_prompt
        files = {"first_frame_image": ("input.jpg", image_bytes, "image/jpeg")}
        payload = await _deapi_submit_and_poll(
            slot, "img2video", data, files, on_progress=on_progress,
        )
        url = payload.get("result_url")
        if not url:
            raise MediaError(f"img2video done but no result_url: {payload!r}")
        return url

    if family == "fal":
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload_json = {
            "prompt": prompt or "Cinematic gentle motion",
            "image_url": f"data:image/jpeg;base64,{b64}",
        }
        if negative_prompt:
            payload_json["negative_prompt"] = negative_prompt
        result = await _fal_submit_and_poll(
            slot, _vmodel, payload_json, on_progress=on_progress,
        )
        # Common shapes: {"video": {"url": ...}} or {"video_url": ...}
        url = (
            (result.get("video") or {}).get("url")
            or result.get("video_url")
            or result.get("url")
        )
        if not url:
            raise MediaError(f"fal img2video done but no url in response: {result!r}")
        return url

    if family == "replicate":
        b64 = base64.b64encode(image_bytes).decode("ascii")
        result = await _replicate_predict(
            slot,
            _vmodel,
            {"prompt": prompt, "image": f"data:image/jpeg;base64,{b64}"},
            on_progress=on_progress,
        )
        out = result.get("output")
        if isinstance(out, list) and out:
            out = out[0]
        if not out:
            raise MediaError(f"replicate img2video done but no output: {result!r}")
        return str(out)

    if family == "weavy":
        return await weavy_image_to_video(
            image_bytes,
            prompt,
            model=_vmodel,
            quality=quality,
            duration=WEAVY_DEFAULT_DURATION,
            on_progress=on_progress,
        )

    raise MediaError(
        f"Провайдер {slot.name!r} ({slot.url}) — у меня нет адаптера для img2video. "
        "Для видео сейчас поддерживаются: deAPI, fal.ai, Replicate, Weavy.ai. "
        "Для фото — ещё OpenAI-совместимые. Переключи слот или открой issue."
    )


@_lm_traced("image_edit")
async def image_to_image(
    image_bytes: bytes,
    prompt: str,
    *,
    fmt: str = "png",
    model: str | None = None,
    size: str | None = None,
    on_progress: ProgressCb | None = None,
) -> tuple[str, str]:
    """Transform ``image_bytes`` per ``prompt``.

    Optional ``model`` / ``size`` override the active photo slot's defaults
    (used by the algo-slots presets). ``size`` is ``"WIDTHxHEIGHT"``.
    Returns ``(png_url_or_data_url, jpg_url_or_empty)`` — both URLs from
    one submission. For OpenAI-compatible providers we return a single
    PNG data URL (the JPG slot will be empty); the caller already
    handles that case for sending as document + photo preview.
    """
    slot = _load_slot("photo")

    if slot.is_custom:
        from . import media_custom

        w = h = 0
        if size and "x" in size.lower():
            try:
                w, h = (int(x) for x in size.lower().split("x", 1))
            except ValueError:
                w = h = 0
        ctx = media_custom.build_context(
            image_bytes=image_bytes,
            prompt=prompt,
            model=model or slot.photo_model,
            api_key=slot.api_key,
            width=w, height=h,
        )
        url = await media_custom.run_custom(
            slot.custom, ctx, task="photo", on_progress=on_progress,
        )
        return url, ""

    family = _provider_family(slot.name)
    _model = model or slot.photo_model

    if family == "deapi":
        seed = random.randint(1, 2**31 - 1)
        data = {
            "prompt": prompt,
            "model": _model,
            "steps": "20",
            "seed": str(seed),
            "guidance": "7.5",
        }
        if size:
            data["size"] = size
        files = {"image": ("input.jpg", image_bytes, "image/jpeg")}
        payload = await _deapi_submit_and_poll(
            slot, "img2img", data, files, on_progress=on_progress,
        )
        png_url = payload.get("result_url") or ""
        jpg_url = (payload.get("results_alt_formats") or {}).get("jpg") or ""
        if not png_url and not jpg_url:
            raise MediaError(f"img2img done but no result_url: {payload!r}")
        fmt_l = (fmt or "png").lower()
        if fmt_l in ("jpeg", "jpg") and jpg_url:
            return jpg_url, png_url
        return png_url, jpg_url

    if family == "openai":
        return await _openai_image_edit(slot, image_bytes, prompt,
                                        model=_model, size=size, on_progress=on_progress)

    if family == "fal":
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload_json = {
            "prompt": prompt,
            "image_url": f"data:image/jpeg;base64,{b64}",
        }
        result = await _fal_submit_and_poll(
            slot, _model, payload_json, on_progress=on_progress,
        )
        # Common shapes: {"images": [{"url": ...}]} or {"image": {"url": ...}}
        url = ""
        if isinstance(result.get("images"), list) and result["images"]:
            url = result["images"][0].get("url") or ""
        if not url:
            url = (result.get("image") or {}).get("url") or result.get("url") or ""
        if not url:
            raise MediaError(f"fal img2img done but no url: {result!r}")
        return url, ""

    if family == "replicate":
        b64 = base64.b64encode(image_bytes).decode("ascii")
        result = await _replicate_predict(
            slot,
            _model,
            {"prompt": prompt, "image": f"data:image/jpeg;base64,{b64}"},
            on_progress=on_progress,
        )
        out = result.get("output")
        if isinstance(out, list) and out:
            out = out[0]
        if not out:
            raise MediaError(f"replicate img2img done but no output: {result!r}")
        return str(out), ""

    raise MediaError(
        f"Провайдер {slot.name!r} ({slot.url}) — у меня нет адаптера для img2img. "
        "Поддерживаются: deAPI, OpenAI-совместимые, fal.ai, Replicate."
    )


@_lm_traced("image_edit.remove_background")
async def remove_background(
    image_bytes: bytes,
    *,
    on_progress: ProgressCb | None = None,
) -> tuple[str, str]:
    """Remove background from ``image_bytes``.

    Returns ``(png_url, jpg_url_or_empty)`` — the PNG keeps transparency
    (Photoshop-ready), the JPG is a flat preview for Telegram inline.
    """
    slot = _load_slot("rmbg")

    if slot.is_custom:
        from . import media_custom

        ctx = media_custom.build_context(
            image_bytes=image_bytes,
            prompt="",
            model=slot.rmbg_model,
            api_key=slot.api_key,
        )
        url = await media_custom.run_custom(
            slot.custom, ctx, task="rmbg", on_progress=on_progress,
        )
        return url, ""

    family = _provider_family(slot.name)

    if family == "deapi":
        data = {"model": slot.rmbg_model}
        files = {"image": ("input.jpg", image_bytes, "image/jpeg")}
        payload = await _deapi_submit_and_poll(
            slot, "img-rmbg", data, files, on_progress=on_progress,
        )
        png_url = payload.get("result_url")
        if not png_url:
            raise MediaError(f"img-rmbg done but no result_url: {payload!r}")
        jpg_url = (payload.get("results_alt_formats") or {}).get("jpg", "")
        return png_url, jpg_url

    if family == "fal":
        b64 = base64.b64encode(image_bytes).decode("ascii")
        result = await _fal_submit_and_poll(
            slot,
            slot.rmbg_model,
            {"image_url": f"data:image/jpeg;base64,{b64}"},
            on_progress=on_progress,
        )
        url = ""
        if isinstance(result.get("images"), list) and result["images"]:
            url = result["images"][0].get("url") or ""
        if not url:
            url = (result.get("image") or {}).get("url") or result.get("url") or ""
        if not url:
            raise MediaError(f"fal rmbg done but no url: {result!r}")
        return url, ""

    if family == "replicate":
        b64 = base64.b64encode(image_bytes).decode("ascii")
        result = await _replicate_predict(
            slot,
            slot.rmbg_model,
            {"image": f"data:image/jpeg;base64,{b64}"},
            on_progress=on_progress,
        )
        out = result.get("output")
        if isinstance(out, list) and out:
            out = out[0]
        if not out:
            raise MediaError(f"replicate rmbg done but no output: {result!r}")
        return str(out), ""

    raise MediaError(
        f"Провайдер {slot.name!r} ({slot.url}) не умеет background removal. "
        "Для rmbg используются: deAPI, fal.ai, Replicate. Переключи rmbg-слот."
    )


# ---- diagnostics ---------------------------------------------------------


async def get_balance(slot: MediaSlot | None = None) -> float:
    """Return current credit balance for the slot (defaults to video slot).

    Only deAPI exposes a public balance endpoint. For everything else we
    return ``0.0`` instead of raising — the wizard's balance line gets
    hidden in that case.
    """
    if slot is None:
        slot = _load_slot("video")
    if _provider_family(slot.name) != "deapi":
        return 0.0
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{slot.url.rstrip('/')}/api/v1/client/balance",
            headers={
                "Authorization": f"Bearer {slot.api_key}",
                "Accept": "application/json",
            },
        )
        if resp.status_code >= 400:
            raise MediaError(f"balance HTTP {resp.status_code}: {resp.text[:200]}")
        return float(resp.json().get("data", {}).get("balance", 0.0))


async def list_models(slot: MediaSlot | None = None) -> list[dict]:
    if slot is None:
        slot = _load_slot("video")
    family = _provider_family(slot.name)
    if family == "deapi":
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{slot.url.rstrip('/')}/api/v1/client/models",
                headers={
                    "Authorization": f"Bearer {slot.api_key}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code >= 400:
                raise MediaError(f"models HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json().get("data") or []
    if family == "openai":
        base = _openai_base(slot.url)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base}/v1/models",
                headers={
                    "Authorization": f"Bearer {slot.api_key}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code >= 400:
                raise MediaError(f"models HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json().get("data") or []
    # fal / replicate / unknown — no catalog endpoint, return what the
    # static catalogue knows.
    meta = KNOWN_MEDIA_PROVIDERS.get(slot.name) or {}
    out: list[dict] = []
    for key in ("video_models", "photo_models", "rmbg_models"):
        for slug, hint in meta.get(key, []) or []:
            out.append({"id": slug, "hint": hint, "task": key.replace("_models", "")})
    return out
