"""SwapFace facade (ТЗ SwapFace v2.0 §3, §5.4, §10).

``swap_image()`` hides the mode choice (usertoken vs manual) from the
media pipeline and adds:

* **idempotency** — dedup key ``sha256(frame)+idname+mode``; a repeat
  inside the TTL window returns the cached result without burning
  credits (§10);
* **single-flight per channel** — an ``asyncio.Lock`` per channel_id so
  parallel swaps never confuse correlation in a shared channel (§10);
* result download in full quality (link-buttons first, attachments via
  gateway coordinates as fallback, HTML unwrap per §5.4.3);
* Master Room storage + append-only audit log (§6).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass, field

from . import parser as sw_parser
from . import state as sw_state

logger = logging.getLogger("swapface.service")

_channel_locks: dict[str, asyncio.Lock] = {}


class SwapFaceError(Exception):
    """User-facing (ru) error from the facade."""


@dataclass
class SwapResult:
    variants: list[bytes] = field(default_factory=list)   # full-quality images
    files: list[str] = field(default_factory=list)         # storage-relative paths
    credits_remaining: int | None = None
    from_cache: bool = False
    raw_message: dict | None = None


def _lock_for_channel(channel_id: str) -> asyncio.Lock:
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock


def dedup_key(frame: bytes, idname: str, mode: str) -> str:
    return hashlib.sha256(frame).hexdigest() + f":{idname}:{mode}"


async def _http_get(url: str) -> tuple[bytes, str]:
    """GET url → (bytes, content_type). No auth headers (public CDN/page)."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content, str(resp.headers.get("content-type") or "")


async def download_results(parsed: sw_parser.ParsedReply) -> list[bytes]:
    """§5.4: link-buttons first (full quality), attachments as fallback.

    Link-button URLs may serve HTML — unwrap og:image/<img>. Attachment
    bytes go through gateway coordinates, not the perishable CDN URL.
    """
    images: list[bytes] = []

    for url in parsed.image_urls:
        try:
            data, ctype = await _http_get(url)
            if "text/html" in ctype.lower():
                direct = sw_parser.extract_image_url_from_html(
                    data.decode("utf-8", errors="replace"),
                )
                if not direct:
                    continue
                data, ctype = await _http_get(direct)
            if data:
                images.append(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("link-button download failed: %s", type(exc).__name__)

    if not images:
        try:
            from ...gateway.adapters import discord_gw
        except Exception:  # noqa: BLE001
            discord_gw = None
        for ref in parsed.attachment_refs:
            data = None
            if discord_gw is not None and ref.get("channel_id") and ref.get("message_id"):
                try:
                    data = await discord_gw.fetch_message_bytes(
                        int(ref["channel_id"]), int(ref["message_id"]),
                        int(ref["attachment_id"]), 50 * 1024 * 1024,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("gateway fetch failed: %s", type(exc).__name__)
            if data is None and ref.get("url"):
                try:
                    data, _ = await _http_get(ref["url"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("attachment url fetch failed: %s", type(exc).__name__)
            if data:
                images.append(data)

    return images


def _store_results(images: list[bytes], idname: str) -> list[str]:
    """§6: save both variants into Master Room; return relative paths."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out: list[str] = []
    results = sw_state.results_dir()
    for i, data in enumerate(images, start=1):
        name = f"{stamp}_{idname}_v{i}.png"
        (results / name).write_bytes(data)
        out.append(f"swapface/results/{name}")
    return out


async def _run_swap(frame: bytes, idname: str, mode: str, *,
                    invoker_discord_id: str | None) -> dict:
    """Dispatch to the mode implementation; returns the final message dict."""
    if mode == "usertoken":
        from . import discord_user

        message = await discord_user.swap_image(frame, idname)
        return message if isinstance(message, dict) else {}
    # manual / semi-manual
    from . import watch

    hint = watch.manual_command_hint("swapid", idname)
    await watch.post_frame(frame, "frame.png", f"SwapFace кадр готов. Команда: `{hint}`")
    return await watch.wait_for_reply(invoker_discord_id=invoker_discord_id)


async def swap_image(frame: bytes, *, idname: str | None = None,
                     invoker_discord_id: str | None = None) -> SwapResult:
    """Facade entry: full swap with idempotency + single-flight (§10)."""
    idname = idname or sw_state.get_active_face()
    if not idname:
        raise SwapFaceError("Активное лицо не выбрано — открой «🎭 Лица».")
    err = sw_state.validate_composite(idname)
    if err:
        raise SwapFaceError(err)
    channel_id = sw_state.get_channel_id()
    if not channel_id:
        raise SwapFaceError("Discord не настроен (guild/channel) — открой «🔗 Discord».")

    mode = sw_state.get_mode()
    key = dedup_key(frame, idname, mode)

    cached = sw_state.dedup_get(key)
    if cached and cached.get("files"):
        variants = []
        root = sw_state.storage_root().parent  # /data/storage
        for rel in cached["files"]:
            path = root / rel
            if path.is_file():
                variants.append(path.read_bytes())
        if variants:
            return SwapResult(
                variants=variants, files=list(cached["files"]),
                credits_remaining=sw_state.get_credits().get("remaining"),
                from_cache=True,
            )

    credits_before = sw_state.get_credits().get("remaining")
    started = time.monotonic()

    async with _lock_for_channel(str(channel_id)):
        message = await _run_swap(
            frame, idname, mode, invoker_discord_id=invoker_discord_id,
        )

    parsed = sw_parser.parse_reply(message)
    if parsed.credits_remaining is not None:
        sw_state.set_credits_remaining(parsed.credits_remaining)
    if parsed.status == sw_parser.STATUS_LIMIT:
        raise SwapFaceError(parsed.error_text or "Дневной лимит кредитов исчерпан.")
    if parsed.status == sw_parser.STATUS_ERROR:
        raise SwapFaceError(parsed.error_text or "InsightFaceSwap вернул ошибку.")

    images = await download_results(parsed)
    if not images:
        raise SwapFaceError("Не удалось скачать результаты свапа.")

    files = _store_results(images, idname.replace("+", "-").replace(",", "-"))
    sw_state.dedup_put(key, {"files": files})
    sw_state.audit_log({
        "mode": mode,
        "idname": idname,
        "credits_before": credits_before,
        "credits_after": sw_state.get_credits().get("remaining"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "picked": None,
        "ok": True,
        "error": None,
    })
    return SwapResult(
        variants=images, files=files,
        credits_remaining=sw_state.get_credits().get("remaining"),
        raw_message=message,
    )


async def save_face(image: bytes, idname: str) -> None:
    """Register a face locally and mirror /saveid to Discord (§9.2)."""
    err = sw_state.validate_idname(idname)
    if err:
        raise SwapFaceError(err)
    path = sw_state.faces_dir() / f"{idname}.jpg"
    path.write_bytes(image)
    ok, warn = sw_state.add_face(idname, f"swapface/faces/{idname}.jpg")
    if not ok:
        raise SwapFaceError(warn or "Не удалось добавить лицо.")

    mode = sw_state.get_mode()
    try:
        if mode == "usertoken":
            from . import discord_user

            await discord_user.save_face(image, idname)
            sw_state.update_face(idname, synced=True, last_error=None)
        else:
            from . import watch

            hint = watch.manual_command_hint("saveid", idname)
            await watch.post_frame(
                image, f"{idname}.jpg", f"Фото лица «{idname}». Команда: `{hint}`",
            )
            # semi-manual: sync happens after the owner runs the command
            sw_state.update_face(idname, synced=False, last_error=None)
    except Exception as exc:  # noqa: BLE001
        sw_state.update_face(idname, synced=False, last_error=type(exc).__name__)
        raise


async def delete_face(idname: str, *, local_only: bool = False) -> None:
    """Delete a face from Discord (unless local_only) and locally (§9.2)."""
    if not local_only:
        mode = sw_state.get_mode()
        if mode == "usertoken":
            from . import discord_user

            await discord_user.delete_face(idname)
        else:
            from ...gateway.adapters import discord_gw
            from . import watch

            channel_id = sw_state.get_channel_id()
            if not channel_id:
                raise SwapFaceError("Discord не настроен — удаление выполнено не было.")
            hint = watch.manual_command_hint("delid", idname)
            posted = await discord_gw.send_channel_text(
                int(channel_id),
                f"Удаление лица «{idname}». Выполни команду: `{hint}`",
            )
            if not posted:
                raise SwapFaceError("Не удалось отправить /delid в Discord-канал.")
    path = sw_state.faces_dir() / f"{idname}.jpg"
    with contextlib.suppress(Exception):
        path.unlink(missing_ok=True)
    sw_state.remove_face(idname)


async def set_active(idname: str) -> None:
    """Mark the face active locally and mirror /setid (§9.2)."""
    err = sw_state.validate_composite(idname)
    if err:
        raise SwapFaceError(err)
    mode = sw_state.get_mode()
    if mode == "usertoken":
        from . import discord_user

        await discord_user.set_active(idname)
    sw_state.set_active_face(idname)


# --- media_ui integration helpers (ТЗ §9.3) --------------------------------

def is_enabled(kind: str) -> bool:
    """Тумблер SwapFace для ``kind`` in {"photo","video"} (§9.3)."""
    return sw_state.enabled_video() if kind == "video" else sw_state.enabled_photo()


def readiness(kind: str) -> tuple[bool, str]:
    """(готов_ли, причина_ru) для kind. Не делает сетевых вызовов (§9.3).

    Готов = тумблер включён, задан channel_id и есть активное лицо.
    """
    if not is_enabled(kind):
        return False, "SwapFace выключен для этого режима."
    if not sw_state.get_channel_id():
        return False, "Discord не настроен — открой «🔗 Discord» в меню SwapFace."
    if not sw_state.get_active_face():
        return False, "Не выбрано активное лицо — открой «🎭 Лица»."
    return True, "готово"
