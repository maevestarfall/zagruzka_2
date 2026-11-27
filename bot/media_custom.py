# Universal "custom request" media provider.
# Co-authored with CoCo
"""Paste a cURL (or any HTTP request from a provider's docs) and the bot
runs it for img2img / img2video / rmbg.

Why this exists
---------------
The original media engine dispatched by a *fixed* provider family
(``deapi`` / ``openai`` / ``fal`` / ``replicate`` / ``weavy``). Any site
outside that list failed with "нет адаптера". Those were the "рамки"
(frames) the owner wanted gone: you could only use providers the bot was
pre-taught.

This module removes that wall. A media slot becomes a **custom provider**
the moment its raw config carries a ``custom`` dict — a self-describing
request template. The built-in templates (``KNOWN_MEDIA_PROVIDERS``)
stay and keep working; this is purely additive. You can now point a slot
at *any* site by pasting its cURL, marking where the prompt / image /
model go, and saving it.

Template shape
--------------
::

    {
      "method": "POST",
      "url": "https://api.example.com/v1/generate",
      "headers": {"Authorization": "Bearer sk-...",
                  "Content-Type": "application/json"},
      "body_type": "json",          # json | form | raw
      "body": "{\\"prompt\\": \\"{{PROMPT}}\\", \\"image\\": \\"{{IMAGE_DATA_URL}}\\"}",
      "result_path": "",            # JSON path to the result URL; "" = auto-detect
      "poll": {                     # optional async polling
          "url": "",                # explicit poll URL template (placeholders ok)
          "url_path": "urls.get",   # OR a path in the submit response
          "status_path": "status",
          "done_values": ["succeeded", "completed", "done", "success"],
          "fail_values": ["failed", "error", "canceled", "cancelled"],
          "result_path": "output",
          "interval": 3.0,
          "timeout": 600.0
      }
    }

Placeholders (substituted at call time, case-insensitive token names)
---------------------------------------------------------------------
``{{PROMPT}}`` ``{{NEGATIVE_PROMPT}}`` ``{{MODEL}}`` ``{{API_KEY}}``
``{{IMAGE_B64}}`` ``{{IMAGE_DATA_URL}}`` ``{{IMAGE_URL}}``
``{{WIDTH}}`` ``{{HEIGHT}}`` ``{{FRAMES}}`` ``{{FPS}}`` ``{{STEPS}}``
``{{SEED}}`` ``{{DURATION}}`` ``{{QUALITY}}`` ``{{ASPECT}}``

The module is intentionally dependency-light: ``httpx`` is imported lazily
inside the async runner so the parser / substitution / extraction helpers
can be unit-tested without it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("bot.media_custom")

ProgressCb = Callable[[str, float], Awaitable[None]]

# Tokens we know how to fill. Anything else is left untouched (so a user
# can keep a literal ``{{...}}`` in their payload if a provider needs it).
KNOWN_TOKENS = (
    "PROMPT", "NEGATIVE_PROMPT", "MODEL", "API_KEY",
    "IMAGE_B64", "IMAGE_DATA_URL", "IMAGE_URL",
    "WIDTH", "HEIGHT", "FRAMES", "FPS", "STEPS", "SEED",
    "DURATION", "QUALITY", "ASPECT",
)

_URL_EXT = {
    "video": (".mp4", ".mov", ".webm", ".m4v", ".gif"),
    "photo": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "rmbg": (".png", ".webp", ".jpg", ".jpeg"),
}

_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class CustomMediaError(RuntimeError):
    """Raised on any custom-provider failure (parse, request, extract)."""


# ---------------------------------------------------------------------------
# cURL parsing
# ---------------------------------------------------------------------------


def parse_curl(text: str) -> dict:
    """Parse a ``curl ...`` command (as copied from API docs) into a template.

    Handles the common flags: ``-X/--request``, ``-H/--header``,
    ``-d/--data/--data-raw/--data-binary``, ``-F/--form``,
    ``-u/--user``, ``--url``, ``-G``, and the bare URL argument.
    Line continuations (``\\`` + newline) are normalised first.

    Returns a dict with: ``method``, ``url``, ``headers``, ``body``,
    ``body_type`` (``json`` | ``form`` | ``raw``). The original key/secret
    values stay verbatim — the user can swap them for ``{{API_KEY}}`` from
    the UI if they prefer to keep the key in the slot's key field.

    Raises :class:`CustomMediaError` when no URL can be found.
    """
    if not text or "curl" not in text.lower():
        # Be lenient — maybe they pasted just a URL + headers without the
        # leading "curl". Try anyway; we only hard-fail on a missing URL.
        pass

    # Normalise shell line continuations and smart quotes.
    cleaned = text.replace("\r\n", "\n").replace("\\\n", " ")
    cleaned = cleaned.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')

    try:
        tokens = shlex.split(cleaned, comments=False, posix=True)
    except ValueError:
        # Unbalanced quotes — retry without posix so we at least get tokens.
        tokens = shlex.split(cleaned, comments=False, posix=False)

    method = ""
    url = ""
    headers: dict[str, str] = {}
    data_parts: list[str] = []
    form_parts: list[tuple[str, str]] = []
    basic_user = ""
    body_type = "raw"

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        low = tok.lower()
        if low == "curl":
            i += 1
            continue
        if low in ("-x", "--request") and i + 1 < n:
            method = tokens[i + 1].upper()
            i += 2
            continue
        if low in ("-h", "--header") and i + 1 < n:
            raw = tokens[i + 1]
            if ":" in raw:
                k, v = raw.split(":", 1)
                headers[k.strip()] = v.strip()
            i += 2
            continue
        if low in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii") and i + 1 < n:
            data_parts.append(tokens[i + 1])
            i += 2
            continue
        if low in ("-f", "--form") and i + 1 < n:
            raw = tokens[i + 1]
            if "=" in raw:
                k, v = raw.split("=", 1)
                form_parts.append((k.strip(), v))
            i += 2
            continue
        if low in ("-u", "--user") and i + 1 < n:
            basic_user = tokens[i + 1]
            i += 2
            continue
        if low == "--url" and i + 1 < n:
            url = tokens[i + 1]
            i += 2
            continue
        # Flags that take a value we don't model — skip the value.
        if low in ("-a", "--user-agent", "-e", "--referer", "-b", "--cookie",
                   "--connect-timeout", "-m", "--max-time", "-o", "--output",
                   "--retry", "--compressed-ssl"):
            i += 2
            continue
        # Value-less flags we can ignore.
        if low in ("-g", "--get", "-s", "--silent", "-l", "--location", "-k",
                   "--insecure", "--compressed", "-i", "--include", "-v", "--verbose"):
            i += 1
            continue
        # Bare token: if it looks like a URL, take it.
        if tok.startswith(("http://", "https://")) and not url:
            url = tok
            i += 1
            continue
        # Unknown flag with no value match — skip it alone.
        if tok.startswith("-"):
            i += 1
            continue
        # Stray bare token that might be a URL missing scheme.
        if not url and ("." in tok and "/" in tok):
            url = tok if tok.startswith("http") else f"https://{tok}"
            i += 1
            continue
        i += 1

    if not url:
        raise CustomMediaError(
            "Не нашёл URL в cURL. Убедись, что вставил полную команду "
            "(curl 'https://...' -H ... -d ...)."
        )

    # Basic-auth → Authorization header.
    if basic_user and not any(k.lower() == "authorization" for k in headers):
        b = base64.b64encode(basic_user.encode()).decode()
        headers["Authorization"] = f"Basic {b}"

    # Assemble body.
    body = ""
    if form_parts:
        body_type = "form"
        body = "&".join(f"{k}={v}" for k, v in form_parts)
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        # Keep the structured form too for multipart-aware callers.
    elif data_parts:
        body = "&".join(data_parts) if len(data_parts) > 1 else data_parts[0]
        # Detect JSON vs urlencoded form.
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        stripped = body.strip()
        if "json" in ctype.lower() or stripped.startswith(("{", "[")):
            body_type = "json"
        elif "=" in body and "{" not in body:
            body_type = "form"
        else:
            body_type = "raw"

    if not method:
        method = "POST" if (body or form_parts) else "GET"

    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": body,
        "body_type": body_type,
    }


# ---------------------------------------------------------------------------
# placeholder substitution
# ---------------------------------------------------------------------------


def _ctx_value(name: str, ctx: dict[str, Any]) -> Any:
    """Look up a token's replacement value, case-insensitively."""
    if name in ctx:
        return ctx[name]
    upper = name.upper()
    for k, v in ctx.items():
        if k.upper() == upper:
            return v
    return None


def substitute_str(template: str, ctx: dict[str, Any]) -> str:
    """Replace ``{{TOKEN}}`` occurrences in a plain string.

    Unknown tokens are left intact. Values are coerced to ``str``.
    """
    def repl(m: re.Match) -> str:
        val = _ctx_value(m.group(1), ctx)
        return template[m.start():m.end()] if val is None else str(val)
    return _TOKEN_RE.sub(repl, template)


def substitute_obj(obj: Any, ctx: dict[str, Any]) -> Any:
    """Recursively substitute tokens inside a parsed JSON structure.

    String *leaves* that are exactly one token (e.g. ``"{{WIDTH}}"``) adopt
    the value's native type (int stays int) so numeric API fields don't get
    quoted. Embedded tokens (``"data:...;base64,{{IMAGE_B64}}"``) splice as
    strings. Keys are substituted too.
    """
    if isinstance(obj, dict):
        return {substitute_str(str(k), ctx): substitute_obj(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_obj(v, ctx) for v in obj]
    if isinstance(obj, str):
        m = _TOKEN_RE.fullmatch(obj.strip())
        if m:
            val = _ctx_value(m.group(1), ctx)
            return obj if val is None else val
        return substitute_str(obj, ctx)
    return obj


# ---------------------------------------------------------------------------
# JSON path + auto URL extraction
# ---------------------------------------------------------------------------


def get_path(obj: Any, path: str) -> Any:
    """Resolve a dotted / bracketed path: ``data.output[0].url``."""
    if not path:
        return obj
    cur = obj
    parts = re.split(r"\.(?![^\[]*\])", path)
    for part in parts:
        if cur is None:
            return None
        # split off [idx] segments.
        m = re.match(r"^([^\[]*)((?:\[\d+\])*)$", part)
        if not m:
            return None
        key, idxs = m.group(1), m.group(2)
        if key:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        for idx in re.findall(r"\[(\d+)\]", idxs):
            if isinstance(cur, (list, tuple)) and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return None
    return cur


def auto_find_url(obj: Any, exts: tuple[str, ...] = ()) -> str:
    """Best-effort search for a result URL anywhere in a response.

    Two passes: first prefer URLs whose path ends in a media extension in
    ``exts``; then accept any http(s) URL. Also accepts ``data:`` URLs.
    """
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, str):
            s = o.strip()
            if s.startswith(("http://", "https://", "data:")):
                found.append(s)
        elif isinstance(o, dict):
            # Prefer common url-ish keys first.
            for key in ("url", "video_url", "image_url", "result_url", "output", "uri", "href"):
                if key in o:
                    walk(o[key])
            for k, v in o.items():
                if k not in ("url", "video_url", "image_url", "result_url", "output", "uri", "href"):
                    walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    if not found:
        return ""
    if exts:
        for u in found:
            base = u.split("?", 1)[0].lower()
            if base.endswith(exts):
                return u
    return found[0]


# ---------------------------------------------------------------------------
# context builder
# ---------------------------------------------------------------------------


def build_context(
    *,
    image_bytes: bytes | None,
    prompt: str = "",
    negative_prompt: str = "",
    model: str = "",
    api_key: str = "",
    width: int = 0,
    height: int = 0,
    frames: int = 0,
    fps: int = 0,
    steps: int = 0,
    seed: int | None = None,
    duration: int = 0,
    quality: str = "",
    aspect: str = "",
    image_mime: str = "image/jpeg",
) -> dict[str, Any]:
    """Assemble the placeholder context for one media call."""
    ctx: dict[str, Any] = {
        "PROMPT": prompt or "",
        "NEGATIVE_PROMPT": negative_prompt or "",
        "MODEL": model or "",
        "API_KEY": api_key or "",
        "WIDTH": width or 0,
        "HEIGHT": height or 0,
        "FRAMES": frames or 0,
        "FPS": fps or 0,
        "STEPS": steps or 0,
        "SEED": seed if seed is not None else random.randint(1, 2**31 - 1),
        "DURATION": duration or 0,
        "QUALITY": quality or "",
        "ASPECT": aspect or "",
    }
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"
        ctx["IMAGE_B64"] = b64
        ctx["IMAGE_DATA_URL"] = data_url
        ctx["IMAGE_URL"] = data_url
    else:
        ctx["IMAGE_B64"] = ""
        ctx["IMAGE_DATA_URL"] = ""
        ctx["IMAGE_URL"] = ""
    return ctx


# ---------------------------------------------------------------------------
# request building
# ---------------------------------------------------------------------------


def build_request(custom: dict, ctx: dict[str, Any]) -> dict:
    """Produce kwargs for an httpx request from a template + context.

    Returns ``{"method", "url", "headers", "json"|"content"|"data"}``.
    """
    method = (custom.get("method") or "POST").upper()
    url = substitute_str(custom.get("url") or "", ctx)
    headers = {substitute_str(k, ctx): substitute_str(str(v), ctx)
               for k, v in (custom.get("headers") or {}).items()}
    body_type = (custom.get("body_type") or "raw").lower()
    raw_body = custom.get("body") or ""

    out: dict[str, Any] = {"method": method, "url": url, "headers": headers}
    if not raw_body:
        return out

    if body_type == "json":
        try:
            parsed = json.loads(raw_body)
            out["json"] = substitute_obj(parsed, ctx)
            return out
        except (json.JSONDecodeError, TypeError):
            # Template isn't valid JSON on its own (tokens broke it) —
            # substitute with JSON-escaped values into the raw string.
            esc_ctx = {k: (json.dumps(v)[1:-1] if isinstance(v, str) else v)
                       for k, v in ctx.items()}
            out["content"] = substitute_str(raw_body, esc_ctx).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
            return out

    if body_type == "form":
        sub = substitute_str(raw_body, ctx)
        data: dict[str, str] = {}
        for pair in sub.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                data[k] = v
        out["data"] = data
        return out

    # raw
    out["content"] = substitute_str(raw_body, ctx).encode("utf-8")
    return out


# ---------------------------------------------------------------------------
# async runner: submit (+ optional poll) → result URL
# ---------------------------------------------------------------------------


async def run_custom(
    custom: dict,
    ctx: dict[str, Any],
    *,
    task: str = "photo",
    on_progress: ProgressCb | None = None,
) -> str:
    """Execute a custom template and return the final media URL.

    Submit the request, optionally poll until done, then extract the
    result URL (explicit ``result_path`` or auto-detection).
    """
    import httpx  # lazy — keeps parser/helpers importable without it

    exts = _URL_EXT.get(task, ())
    req = build_request(custom, ctx)
    poll = custom.get("poll") or {}
    result_path = custom.get("result_path") or ""

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.request(**req)
        if resp.status_code >= 400:
            raise CustomMediaError(
                f"{custom.get('url')} → HTTP {resp.status_code}: {resp.text[:400]}"
            )
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — some return a bare URL/text
            text = resp.text.strip()
            if text.startswith(("http://", "https://", "data:")):
                return text
            raise CustomMediaError(f"Ответ не JSON и не URL: {text[:300]}")

        # No polling configured → extract straight away.
        if not poll:
            return _extract(body, result_path, exts, custom)

        # Resolve the poll URL.
        poll_url = substitute_str(poll.get("url") or "", ctx)
        if not poll_url:
            up = poll.get("url_path") or ""
            poll_url = str(get_path(body, up) or "") if up else ""
        if not poll_url:
            # Provider returned the result inline despite a poll config.
            inline = _extract(body, result_path, exts, custom, soft=True)
            if inline:
                return inline
            raise CustomMediaError(
                "Поллинг включён, но в ответе нет URL статуса "
                f"(url_path={poll.get('url_path')!r}). Ответ: {json.dumps(body)[:300]}"
            )

        status_path = poll.get("status_path") or "status"
        done_vals = {str(x).lower() for x in (poll.get("done_values")
                     or ["succeeded", "completed", "done", "success", "ok"])}
        fail_vals = {str(x).lower() for x in (poll.get("fail_values")
                     or ["failed", "error", "canceled", "cancelled"])}
        interval = float(poll.get("interval") or 3.0)
        timeout = float(poll.get("timeout") or 600.0)
        poll_result_path = poll.get("result_path") or result_path

        deadline = asyncio.get_event_loop().time() + timeout
        ticks = 0.0
        while asyncio.get_event_loop().time() < deadline:
            sresp = await client.get(poll_url, headers=req.get("headers"))
            if sresp.status_code >= 400:
                raise CustomMediaError(
                    f"poll {poll_url} → HTTP {sresp.status_code}: {sresp.text[:300]}"
                )
            sbody = sresp.json()
            status = str(get_path(sbody, status_path) or "").lower()
            if status in done_vals or (not status and auto_find_url(sbody, exts)):
                return _extract(sbody, poll_result_path, exts, custom)
            if status in fail_vals:
                raise CustomMediaError(f"Задача провалилась: {json.dumps(sbody)[:300]}")
            if on_progress is not None:
                ticks += 1.0
                try:
                    await on_progress(status or "processing", ticks)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(interval)
        raise CustomMediaError(f"Таймаут поллинга {timeout:.0f}s ({poll_url}).")


def _extract(body: Any, result_path: str, exts: tuple[str, ...],
             custom: dict, *, soft: bool = False) -> str:
    """Pull the result URL out of a finished response."""
    if result_path:
        val = get_path(body, result_path)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, (list, tuple)) and val:
            first = val[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                u = auto_find_url(first, exts)
                if u:
                    return u
        if isinstance(val, dict):
            u = auto_find_url(val, exts)
            if u:
                return u
    auto = auto_find_url(body, exts)
    if auto:
        return auto
    if soft:
        return ""
    raise CustomMediaError(
        f"Готово, но не нашёл URL результата. result_path={result_path!r}. "
        f"Ответ: {json.dumps(body)[:300]}"
    )


# ---------------------------------------------------------------------------
# slot helpers
# ---------------------------------------------------------------------------


_PROMPT_KEYS = {"prompt", "text", "input_text", "description", "caption"}
_NEG_KEYS = {"negative_prompt", "negative", "neg_prompt"}
_IMAGE_KEYS = {"image", "image_url", "img", "image_b64", "image_base64",
               "init_image", "first_frame_image", "input_image", "image_data",
               "start_image", "source_image"}
_MODEL_KEYS = {"model", "model_id", "model_name", "version", "model_url"}


def auto_tokenize(custom: dict) -> tuple[dict, list[str]]:
    """Insert ``{{PROMPT}}`` / ``{{IMAGE_*}}`` / ``{{MODEL}}`` into a freshly
    pasted template by recognising common field names.

    Only runs when the body has *no* tokens yet (so we never clobber a
    template the user tokenised by hand). Returns ``(new_custom, inserted)``
    where ``inserted`` lists the tokens that were wired in. Best-effort: if
    the body isn't JSON we leave it untouched.
    """
    body = custom.get("body") or ""
    existing = set(_TOKEN_RE.findall(body) + _TOKEN_RE.findall(custom.get("url") or ""))
    if existing:
        return custom, []
    if (custom.get("body_type") or "") != "json":
        return custom, []
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return custom, []

    inserted: list[str] = []

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                kl = str(k).lower()
                if kl in _PROMPT_KEYS and isinstance(v, str):
                    out[k] = "{{PROMPT}}"
                    inserted.append("{{PROMPT}}")
                elif kl in _NEG_KEYS and isinstance(v, str):
                    out[k] = "{{NEGATIVE_PROMPT}}"
                    inserted.append("{{NEGATIVE_PROMPT}}")
                elif kl in _IMAGE_KEYS and isinstance(v, str):
                    # data-url style by default; bare-b64 if the original
                    # clearly was base64 without a data: prefix.
                    tok = "{{IMAGE_B64}}" if "b64" in kl or "base64" in kl else "{{IMAGE_DATA_URL}}"
                    out[k] = tok
                    inserted.append(tok)
                elif kl in _MODEL_KEYS and isinstance(v, str):
                    out[k] = "{{MODEL}}"
                    inserted.append("{{MODEL}}")
                else:
                    out[k] = walk(v)
            return out
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    new_parsed = walk(parsed)
    if not inserted:
        return custom, []
    new_custom = dict(custom)
    new_custom["body"] = json.dumps(new_parsed, ensure_ascii=False)
    return new_custom, sorted(set(inserted))


def is_custom_raw(raw: dict | None) -> bool:
    """True when a raw slot dict carries a usable custom template."""
    if not raw:
        return False
    c = raw.get("custom")
    return bool(isinstance(c, dict) and c.get("url"))


def template_summary(custom: dict) -> str:
    """Short human summary of a template for the slot UI."""
    if not custom:
        return "—"
    method = custom.get("method", "POST")
    url = custom.get("url", "")
    bt = custom.get("body_type", "raw")
    poll = "poll" if custom.get("poll") else "sync"
    body = custom.get("body") or ""
    toks = sorted(set(_TOKEN_RE.findall(body) + _TOKEN_RE.findall(url)))
    tok_str = (" · " + ",".join(toks)) if toks else ""
    return f"{method} {url[:48]} [{bt}/{poll}]{tok_str}"
