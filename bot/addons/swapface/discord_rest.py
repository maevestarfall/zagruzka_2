"""Thin shared Discord REST v10 client (ТЗ SwapFace v2.0 §5.1).

Used by BOTH modes (user-token slash calls and bot-token polling
fallback). No ``discord.py-self`` — plain ``httpx`` against
``https://discord.com/api/v10`` (same version the gateway adapter uses).

Status handling contract:

* ``401/403``  → hard :class:`DiscordAuthError`, no retry;
* ``429``      → sleep ``retry_after`` (JSON body → ``Retry-After`` header
  → ``X-RateLimit-Reset-After``), then retry;
* ``5xx``/net  → exponential backoff 0.5 → 1 → 2 s, max 3 attempts;
* tokens are never logged; ``Authorization`` is masked in reprs.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from typing import Any

logger = logging.getLogger("swapface.rest")

API_BASE = "https://discord.com/api/v10"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5

_DISCORD_EPOCH_MS = 1420070400000


class DiscordRestError(Exception):
    """Base REST error. ``status`` may be None for network failures."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DiscordAuthError(DiscordRestError):
    """401/403 — hard failure, never retried."""


def mask_headers(headers: dict) -> dict:
    """Mask Authorization for any logging/tracebacks (§5.1 security)."""
    out = dict(headers or {})
    if "Authorization" in out:
        out["Authorization"] = "***"
    return out


def generate_nonce() -> str:
    """Snowflake-style nonce: current time shifted into snowflake layout.

    Doubles as the idempotency key on interaction retries (§5.5) —
    Discord will not create a second invocation for the same nonce.
    """
    return str((int(time.time() * 1000) - _DISCORD_EPOCH_MS) << 22)


def generate_session_id() -> str:
    return secrets.token_hex(16)


class DiscordRest:
    """Async REST client bound to one token (bot or user)."""

    def __init__(self, token: str, *, is_bot: bool, client: Any | None = None,
                 sleep=asyncio.sleep) -> None:
        self._token = token
        self._is_bot = is_bot
        self._client = client  # injected in tests (httpx.AsyncClient-compatible)
        self._sleep = sleep

    # -- plumbing ----------------------------------------------------------

    @property
    def auth_header(self) -> str:
        return f"Bot {self._token}" if self._is_bot else self._token

    async def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    @staticmethod
    def _retry_after(resp: Any) -> float:
        """429 → seconds to wait (JSON body / Retry-After / Reset-After)."""
        try:
            body = resp.json()
            if isinstance(body, dict) and "retry_after" in body:
                return max(0.0, float(body["retry_after"]))
        except Exception:  # noqa: BLE001
            pass
        headers = getattr(resp, "headers", {}) or {}
        for name in ("Retry-After", "X-RateLimit-Reset-After"):
            raw = headers.get(name)
            if raw:
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    continue
        return 1.0

    async def request(self, method: str, path: str, *, json: Any = None,
                      params: dict | None = None, content: bytes | None = None,
                      headers: dict | None = None,
                      absolute_url: str | None = None) -> Any:
        """One logical request with the §5.1 retry policy. Returns response."""
        client = await self._ensure_client()
        url = absolute_url or (API_BASE + path)
        req_headers = {"Authorization": self.auth_header}
        if headers:
            req_headers.update(headers)
        if absolute_url:
            # e.g. google-cloud upload_url — no Discord auth there
            req_headers.pop("Authorization", None)
            if headers:
                req_headers.update(headers)

        attempt = 0
        backoff = BACKOFF_BASE
        while True:
            attempt += 1
            try:
                resp = await client.request(
                    method, url, json=json, params=params,
                    content=content, headers=req_headers,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — network / DNS / timeout
                logger.warning("discord rest network error: %s", type(exc).__name__)
                if attempt >= MAX_ATTEMPTS:
                    raise DiscordRestError(
                        f"Сетевая ошибка Discord API ({type(exc).__name__}).",
                    ) from None
                await self._sleep(backoff + random.uniform(0, 0.1))
                backoff *= 2
                continue

            status = resp.status_code
            if 200 <= status < 300:
                return resp
            if status in (401, 403):
                raise DiscordAuthError(
                    "Discord отклонил токен (401/403). Проверь env-токен и права.",
                    status=status,
                )
            if status == 429:
                wait = self._retry_after(resp)
                logger.warning("discord rest 429, retry_after=%.2fs", wait)
                await self._sleep(wait)
                continue
            if status >= 500:
                logger.warning("discord rest %s (attempt %d)", status, attempt)
                if attempt >= MAX_ATTEMPTS:
                    raise DiscordRestError(
                        f"Discord API вернул {status} после {attempt} попыток.",
                        status=status,
                    )
                await self._sleep(backoff + random.uniform(0, 0.1))
                backoff *= 2
                continue
            # other 4xx — no retry
            raise DiscordRestError(f"Discord API вернул {status}.", status=status)

    # -- high-level helpers --------------------------------------------------

    async def me(self) -> dict:
        """§5.6 token check: GET /users/@me."""
        resp = await self.request("GET", "/users/@me")
        return resp.json()

    async def command_index(self, guild_id: str) -> dict:
        """§5.2: application-command-index for the guild."""
        resp = await self.request(
            "GET", f"/guilds/{guild_id}/application-command-index",
        )
        return resp.json()

    async def create_attachment_slot(self, channel_id: str, filename: str,
                                     file_size: int) -> dict:
        """§5.3 step 1: reserve an upload slot; returns upload_url etc."""
        resp = await self.request(
            "POST", f"/channels/{channel_id}/attachments",
            json={"files": [{"filename": filename, "file_size": file_size, "id": "0"}]},
        )
        data = resp.json()
        atts = data.get("attachments") or []
        if not atts:
            raise DiscordRestError("Discord не выдал upload-слот для вложения.")
        return atts[0]

    async def upload_to_slot(self, upload_url: str, data: bytes,
                             content_type: str = "application/octet-stream") -> None:
        """§5.3 step 2: PUT bytes to the returned upload_url (no auth)."""
        await self.request(
            "PUT", "", absolute_url=upload_url, content=data,
            headers={"Content-Type": content_type},
        )

    async def post_interaction(self, payload: dict) -> tuple[str | None, str]:
        """§5.3 step 3–4: POST /interactions.

        Returns ``(interaction_id | None, nonce)`` — a ``204`` with empty
        body means we must correlate by nonce.
        """
        nonce = str(payload.get("nonce") or generate_nonce())
        payload["nonce"] = nonce
        resp = await self.request("POST", "/interactions", json=payload)
        interaction_id = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                interaction_id = body.get("id")
        except Exception:  # noqa: BLE001 — 204 empty body
            pass
        return (str(interaction_id) if interaction_id else None), nonce

    async def get_messages_after(self, channel_id: str, after: str | None,
                                 limit: int = 50) -> list[dict]:
        """Polling fallback: GET /channels/{id}/messages?after=<cursor>."""
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        resp = await self.request(
            "GET", f"/channels/{channel_id}/messages", params=params,
        )
        data = resp.json()
        return list(data) if isinstance(data, list) else []


def build_interaction_payload(*, app_id: str, guild_id: str, channel_id: str,
                              command: dict, command_name: str,
                              options: list[dict],
                              attachments: list[dict] | None = None,
                              session_id: str | None = None,
                              nonce: str | None = None) -> dict:
    """Assemble the §5.3 type=2 interactions payload (pure function).

    ``command`` is the ``{id, version, ...}`` dict from the scanned index;
    if it carries full metadata we mirror it into ``application_command``
    (midjourney-api practice — improves compatibility).
    """
    data: dict[str, Any] = {
        "id": str(command["id"]),
        "version": str(command["version"]),
        "name": command_name,
        "type": 1,
        "options": options,
    }
    if attachments:
        data["attachments"] = attachments
    meta = command.get("application_command")
    if isinstance(meta, dict):
        data["application_command"] = meta
    return {
        "type": 2,
        "application_id": str(app_id),
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "session_id": session_id or generate_session_id(),
        "nonce": nonce or generate_nonce(),
        "data": data,
    }


def find_insight_commands(index: dict) -> tuple[str | None, dict]:
    """§5.2: locate the application exposing saveid/swapid/… commands.

    Returns ``(application_id, {name: {id, version, application_command}})``.
    ``(None, {})`` when not found.
    """
    wanted = {"saveid", "delid", "swapid", "setid", "listid"}
    apps = index.get("applications") or []
    commands = index.get("application_commands") or []

    by_app: dict[str, dict[str, dict]] = {}
    for cmd in commands:
        app_id = str(cmd.get("application_id") or "")
        name = str(cmd.get("name") or "").lower()
        if not app_id or name not in wanted:
            continue
        by_app.setdefault(app_id, {})[name] = {
            "id": str(cmd.get("id")),
            "version": str(cmd.get("version")),
            "application_command": cmd,
        }

    # приоритет: приложение, у которого есть swapid (ядро фичи)
    best_id, best = None, {}
    for app_id, cmds in by_app.items():
        if "swapid" in cmds and len(cmds) > len(best):
            best_id, best = app_id, cmds
    if best_id is None:
        return None, {}
    # sanity: known application names, if provided
    _ = apps  # names are informative only; command match is authoritative
    return best_id, best
