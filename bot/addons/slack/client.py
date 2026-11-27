"""Thin wrapper over the official ``slack_sdk`` WebClient.

Built on the maintained, first-party Slack SDK (``slackapi/python-slack-sdk``)
rather than hand-rolled HTTP: it already ships cursor pagination helpers, a
built-in rate-limit retry handler (HTTP 429 → wait & retry) and typed errors.

Everything here is read-only: we only call ``auth.test``, ``conversations.*``
(list/history/replies/info), ``users.info`` and download files via their
``url_private`` link with a Bearer header. No message is ever posted or
modified.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    from slack_sdk.http_retry.builtin_handlers import (
        ConnectionErrorRetryHandler,
        RateLimitErrorRetryHandler,
    )
except ImportError as _slack_import_err:  # noqa: BLE001
    # slack_sdk is an optional dependency — it's only needed when the Slack
    # addon is actually configured.  Defer the hard error to the first runtime
    # call (SlackClient.__init__) instead of crashing the entire bot import
    # graph on startup / during tests.
    WebClient = None  # type: ignore[assignment,misc]
    SlackApiError = Exception  # type: ignore[assignment,misc]
    ConnectionErrorRetryHandler = None  # type: ignore[assignment]
    RateLimitErrorRetryHandler = None  # type: ignore[assignment]
    _SLACK_SDK_MISSING: str | None = str(_slack_import_err)
else:
    _SLACK_SDK_MISSING: str | None = None  # type: ignore[no-redef]

from .config import REQUIRED_SCOPES, get_settings
from .credentials import load_token
from .models import SlackConversation, SlackFile, SlackMessage

logger = logging.getLogger(__name__)


class SlackNotConfiguredError(RuntimeError):
    """Raised when a Slack call is attempted before a token is stored."""


class SlackAuthError(RuntimeError):
    """Raised when auth.test fails (bad token) or a scope is missing."""


# Map Slack error codes to a friendly, actionable message (Russian UI).
_FRIENDLY = {
    "invalid_auth": "Неверный токен Slack. Проверь, что скопирован полностью (xoxb-… / xoxp-…).",
    "account_inactive": "Токен отключён/деактивирован в Slack.",
    "token_revoked": "Токен отозван. Создай новый в настройках Slack-приложения.",
    "not_authed": "Токен не передан или пустой.",
    "missing_scope": "Не хватает прав (scope). Добавь нужные права приложению и переустанови его.",
    "not_allowed_token_type": "Тип токена не подходит. Нужен Bot (xoxb-) или User (xoxp-) token.",
}


def _friendly(err_code: str, needed: str | None = None) -> str:
    base = _FRIENDLY.get(err_code, f"Slack API error: {err_code}")
    if err_code == "missing_scope" and needed:
        base += f"\nНужно право: {needed}"
    return base


class SlackClient:
    """Read-only Slack Web API wrapper with name + rate-limit handling."""

    def __init__(self, token: str | None = None) -> None:
        if _SLACK_SDK_MISSING is not None:
            raise SlackNotConfiguredError(
                f"slack_sdk не установлен: {_SLACK_SDK_MISSING}. "
                "Добавь 'slack-sdk' в зависимости проекта."
            )
        tok = token or load_token()
        if not tok:
            raise SlackNotConfiguredError(
                "Slack-токен не настроен. Открой «💬 Слак → 🔌 Подключить Slack»."
            )
        settings = get_settings()
        retry_handlers = []
        if RateLimitErrorRetryHandler is not None:
            retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
        if ConnectionErrorRetryHandler is not None:
            retry_handlers.append(ConnectionErrorRetryHandler(max_retry_count=3))
        self._client = WebClient(
            token=tok,
            timeout=int(settings.request_timeout),
            retry_handlers=retry_handlers,
        )
        self._token = tok
        self._settings = settings
        self._user_cache: dict[str, str] = {}
        self._own_user_id: str | None = None

    # ---- auth -----------------------------------------------------------

    def auth_test(self) -> dict:
        """Verify the token. Returns the auth.test payload or raises SlackAuthError."""
        try:
            resp = self._client.auth_test()
            data = dict(resp.data)
            self._own_user_id = data.get("user_id") or self._own_user_id
            return data
        except SlackApiError as exc:
            code = exc.response.get("error", "unknown") if exc.response else "unknown"
            raise SlackAuthError(_friendly(code)) from exc

    @property
    def own_user_id(self) -> str:
        """The token owner's Slack user id (cached; resolved via auth.test)."""
        if self._own_user_id is None:
            try:
                self.auth_test()
            except SlackAuthError:
                self._own_user_id = ""
        return self._own_user_id or ""

    # ---- users ----------------------------------------------------------

    def resolve_user_name(self, user_id: str) -> str:
        """Map a Slack user id to a display name (cached). Falls back to the id."""
        if not user_id:
            return ""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        name = user_id
        try:
            resp = self._client.users_info(user=user_id)
            prof = (resp.data.get("user") or {}).get("profile", {}) if resp.data else {}
            user = resp.data.get("user") or {} if resp.data else {}
            name = (
                prof.get("display_name")
                or prof.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
        except SlackApiError as exc:
            code = exc.response.get("error", "") if exc.response else ""
            logger.debug("users.info failed for %s: %s", user_id, code)
        self._user_cache[user_id] = name
        return name

    # ---- conversations --------------------------------------------------

    def list_conversations(self, include_dms: bool = True) -> list[SlackConversation]:
        """List channels (public+private) and, optionally, DMs / group DMs."""
        types = ["public_channel", "private_channel"]
        if include_dms:
            types += ["im", "mpim"]
        out: list[SlackConversation] = []
        cursor: str | None = None
        try:
            while True:
                resp = self._client.conversations_list(
                    types=",".join(types),
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor or None,
                )
                for ch in resp.data.get("channels", []) if resp.data else []:
                    out.append(self._conversation_from_raw(ch))
                cursor = (resp.data.get("response_metadata", {}) or {}).get("next_cursor") if resp.data else None
                if not cursor:
                    break
        except SlackApiError as exc:
            code = exc.response.get("error", "unknown") if exc.response else "unknown"
            raise SlackAuthError(_friendly(code, "channels:read / groups:read")) from exc
        return out

    def _conversation_from_raw(self, ch: dict) -> SlackConversation:
        cid = ch.get("id", "")
        if ch.get("is_im"):
            uname = self.resolve_user_name(ch.get("user", ""))
            return SlackConversation(id=cid, name=f"DM: {uname}", kind="im")
        if ch.get("is_mpim"):
            return SlackConversation(id=cid, name=f"Группа: {ch.get('name', cid)}", kind="mpim")
        if ch.get("is_private"):
            return SlackConversation(id=cid, name=f"🔒 {ch.get('name', cid)}", kind="private",
                                     is_member=bool(ch.get("is_member", True)))
        return SlackConversation(id=cid, name=f"#{ch.get('name', cid)}", kind="public",
                                 is_member=bool(ch.get("is_member", True)))

    # ---- history & threads ---------------------------------------------

    def fetch_history(self, channel_id: str) -> list[SlackMessage]:
        """Read the ENTIRE channel history (cursor pagination to the end)."""
        messages: list[SlackMessage] = []
        cursor: str | None = None
        pages = 0
        try:
            while True:
                resp = self._client.conversations_history(
                    channel=channel_id,
                    limit=self._settings.history_page_size,
                    cursor=cursor or None,
                )
                for raw in resp.data.get("messages", []) if resp.data else []:
                    messages.append(self._message_from_raw(raw, channel_id))
                pages += 1
                meta = (resp.data.get("response_metadata", {}) or {}) if resp.data else {}
                cursor = meta.get("next_cursor")
                has_more = bool(resp.data.get("has_more")) if resp.data else False
                if not cursor or not has_more or pages >= self._settings.max_history_pages:
                    break
        except SlackApiError as exc:
            code = exc.response.get("error", "unknown") if exc.response else "unknown"
            if code in {"missing_scope", "not_in_channel", "channel_not_found"}:
                raise SlackAuthError(
                    f"Нет доступа к каналу: {code}. "
                    "Возможно, не хватает прав (channels:history / groups:history) "
                    "или бот не добавлен в этот канал."
                ) from exc
            raise SlackAuthError(_friendly(code)) from exc
        # Slack returns newest-first; flip to chronological.
        messages.reverse()
        return messages

    def fetch_replies(self, channel_id: str, thread_ts: str) -> list[SlackMessage]:
        """Read ALL replies of a thread (paginated), excluding the parent."""
        out: list[SlackMessage] = []
        cursor: str | None = None
        try:
            while True:
                resp = self._client.conversations_replies(
                    channel=channel_id,
                    ts=thread_ts,
                    limit=self._settings.history_page_size,
                    cursor=cursor or None,
                )
                for raw in resp.data.get("messages", []) if resp.data else []:
                    if raw.get("ts") == thread_ts:
                        continue  # skip the parent itself
                    out.append(self._message_from_raw(raw, channel_id))
                meta = (resp.data.get("response_metadata", {}) or {}) if resp.data else {}
                cursor = meta.get("next_cursor")
                if not cursor or not (resp.data.get("has_more") if resp.data else False):
                    break
        except SlackApiError as exc:
            code = exc.response.get("error", "unknown") if exc.response else "unknown"
            logger.warning("conversations.replies failed for %s/%s: %s", channel_id, thread_ts, code)
        return out

    def fetch_all_with_threads(self, channel_id: str) -> list[SlackMessage]:
        """Full conversation: every top-level message with all its thread replies."""
        top = self.fetch_history(channel_id)
        for msg in top:
            # A message owns a thread when reply_count>0 and it is the parent.
            if msg.reply_count > 0 and (not msg.thread_ts or msg.thread_ts == msg.ts):
                msg.is_thread_parent = True
                msg.replies = self.fetch_replies(channel_id, msg.ts)
                for r in msg.replies:
                    if not r.author_name:
                        r.author_name = self.resolve_user_name(r.author_id)
        # Resolve author names for the top-level messages too.
        for msg in top:
            if not msg.author_name:
                msg.author_name = self.resolve_user_name(msg.author_id)
        return top

    def _message_from_raw(self, raw: dict, channel_id: str) -> SlackMessage:
        ts = raw.get("ts", "")
        dt = None
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, TypeError):
            pass
        files = []
        for f in raw.get("files", []) or []:
            files.append(
                SlackFile(
                    id=f.get("id", ""),
                    name=f.get("name", "") or f.get("title", "") or f.get("id", ""),
                    title=f.get("title", ""),
                    mimetype=f.get("mimetype", ""),
                    size=int(f.get("size", 0) or 0),
                    url_private=f.get("url_private_download") or f.get("url_private", ""),
                    permalink=f.get("permalink", ""),
                )
            )
        author_id = raw.get("user", "") or raw.get("bot_id", "")
        return SlackMessage(
            ts=ts,
            channel_id=channel_id,
            author_id=author_id,
            author_name=raw.get("username", ""),  # bot messages sometimes carry this
            is_me=bool(author_id) and author_id == self.own_user_id,
            text=raw.get("text", "") or "",
            datetime=dt,
            files=files,
            thread_ts=raw.get("thread_ts", "") or "",
            reply_count=int(raw.get("reply_count", 0) or 0),
        )

    # ---- file download --------------------------------------------------

    def download_file(self, file: SlackFile, dest_path: Path) -> Path:
        """Download a Slack file (private URL) using the Bearer token, with 429 retry."""
        if not file.url_private:
            raise ValueError(f"У файла {file.name!r} нет приватной ссылки для скачивания.")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {self._token}"}
        attempts = 0
        with httpx.Client(timeout=self._settings.request_timeout, follow_redirects=True) as http:
            while True:
                attempts += 1
                with http.stream("GET", file.url_private, headers=headers) as resp:
                    if resp.status_code == 429 and attempts <= 5:
                        wait = int(resp.headers.get("Retry-After", "2") or "2")
                        time.sleep(max(1, wait))
                        continue
                    resp.raise_for_status()
                    # Slack returns an HTML login page (not the file) when the
                    # token lacks files:read or can't see the file — guard
                    # against silently saving that page as the "download".
                    ctype = resp.headers.get("Content-Type", "")
                    if "text/html" in ctype and file.mimetype and "html" not in file.mimetype:
                        raise SlackAuthError(
                            f"Не удалось скачать {file.name!r}: Slack вернул страницу входа. "
                            "Проверь право files:read и доступ к каналу."
                        )
                    with open(dest_path, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            if chunk:
                                fh.write(chunk)
                    return dest_path


def scopes_hint() -> str:
    return ", ".join(REQUIRED_SCOPES)
