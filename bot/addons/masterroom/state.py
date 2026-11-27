# Master Room state helpers with GitHub account token storage.
# Co-authored with CoCo
"""State helpers for the «🛠 Master Room» addon (v0.3).

State layout — everything lives under ``addons.masterroom.*`` in
:func:`bot.storage.storage._settings`. Reading / writing goes through
:mod:`bot.addons.state` so the regular state.json save cycle picks
up changes:

* ``token``              — URL-safe secret for inbound webhooks (str)
* ``enabled``            — bool, default False; flips on when the user
  generates a link in the addon UI
* ``rotated_at``         — int epoch of the last token rotation
* ``servers``            — list[dict] of *coder-bot* HTTP servers:
  ``{id, name, url, token, added_at}``. Multi-server, mirrors the
  Analyzer PRO server list.
* ``active_server_id``   — currently active coder-bot id (str or None)
* ``github_accounts``    — list[dict] of GitHub accounts:
  ``{id, name, owner, token_ref, default_visibility, added_at}``.
  ``token_ref`` is the env-var name; the secret itself never lands in
  state.json. Lilush only needs to know the name + owner so it can
  show a friendly label and pull the token at call time.
* ``active_github_id``   — currently active github account id (str)
* ``targets``            — Brains-pattern deploy/preview targets:
  ``{render: {name, url_template}, oracle: {name, url_template},
  custom: [{id, name, url_template}]}``. No API keys or SSH keys
  stored — deploy happens through GitHub Actions per :doc:`v0.3`.
* ``active_target_id``   — ``render`` | ``oracle`` | ``custom:<id>``
* ``projects``           — list[dict] of created projects:
  ``{id, task_id, title, stack, target_id, github_account_id,
  github_repo, status, preview_url, deploy_url, created_at}``
* ``defaults``           — FSM defaults: ``{stack, budget, target_id}``

The reserve_defaults() function from the old stub now seeds the full
namespace once on startup so future migrations don't trip on missing
keys.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any, Optional

from .. import state as addon_state

ADDON = "masterroom"

# --- defaults ----------------------------------------------------------

DEFAULT_STACK = "static"
DEFAULT_BUDGET = 200_000
DEFAULT_TARGET_ID = "render"

STACK_CHOICES: tuple[str, ...] = (
    "static", "react", "node_api", "python_api", "tg_bot", "pwa", "other",
)
STACK_LABEL = {
    "static":     "🌐 Static (HTML/CSS/JS)",
    "react":      "⚛️ React (Vite + Tailwind + shadcn)",
    "node_api":   "🟢 Node API (Fastify)",
    "python_api": "🐍 Python API (FastAPI)",
    "tg_bot":     "🤖 Telegram Bot (aiogram 3)",
    "pwa":        "📱 PWA (React + Vite)",
    "other":      "⚙️ Другое (текстом)",
}

BUDGET_CHOICES: tuple[int, ...] = (100_000, 200_000, 500_000, 1_000_000)

PROJECT_STATUSES: tuple[str, ...] = (
    "queued", "coding", "testing", "committed",
    "deploying", "done", "failed", "cancelled",
)

# --- low-level accessors ----------------------------------------------

def _bucket(key: str, default: Any) -> Any:
    v = addon_state.get(ADDON, key, default)
    return v if v is not None else default


# --- token / enabled --------------------------------------------------

def is_enabled() -> bool:
    return bool(addon_state.get(ADDON, "enabled", False))


def get_token() -> str:
    return str(addon_state.get(ADDON, "token", "") or "")


def get_rotated_at() -> int:
    try:
        return int(addon_state.get(ADDON, "rotated_at", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def generate_link(base_url: str | None = None) -> tuple[str, str]:
    """Rotate token, flip ``enabled`` on, return ``(token, public_link)``."""
    token = secrets.token_urlsafe(24)
    addon_state.set_(ADDON, "token", token)
    addon_state.set_(ADDON, "enabled", True)
    addon_state.set_(ADDON, "rotated_at", int(time.time()))
    return token, get_link(base_url)


def disable_link() -> bool:
    was = is_enabled()
    addon_state.set_(ADDON, "enabled", False)
    return was


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    if base_url is None:
        from ...config import resolve_public_url
        base_url = resolve_public_url()
    base = (base_url or "").rstrip("/")
    return f"{base}/masterroom/{token}/" if base else f"/masterroom/{token}/"


# --- coder servers (multi-server) -------------------------------------

def get_servers() -> list[dict[str, Any]]:
    raw = addon_state.get(ADDON, "servers", None)
    return list(raw) if isinstance(raw, list) else []


def _save_servers(servers: list[dict[str, Any]]) -> None:
    addon_state.set_(ADDON, "servers", servers)


def add_server(name: str, url: str, token: str) -> dict[str, Any]:
    server = {
        "id": str(uuid.uuid4()),
        "name": name.strip() or "coder",
        "url": url.strip().rstrip("/"),
        "token": token.strip(),
        "added_at": int(time.time()),
    }
    servers = get_servers()
    servers.append(server)
    _save_servers(servers)
    if not get_active_server_id():
        set_active_server_id(server["id"])
    return server


def remove_server(server_id: str) -> bool:
    servers = get_servers()
    kept = [s for s in servers if s.get("id") != server_id]
    if len(kept) == len(servers):
        return False
    _save_servers(kept)
    if get_active_server_id() == server_id:
        set_active_server_id(kept[0]["id"] if kept else None)
    return True


def get_active_server_id() -> Optional[str]:
    raw = addon_state.get(ADDON, "active_server_id", None)
    return str(raw) if isinstance(raw, str) and raw else None


def set_active_server_id(server_id: Optional[str]) -> None:
    addon_state.set_(ADDON, "active_server_id", server_id or "")


def get_active_server() -> Optional[dict[str, Any]]:
    sid = get_active_server_id()
    if not sid:
        return None
    for s in get_servers():
        if s.get("id") == sid:
            return s
    return None


# --- github accounts (multi-github) -----------------------------------

# Hard cap on GitHub API "slots" (the auto-switch feature walks through
# every filled slot in order). The owner asked for twenty.
MAX_GITHUB_SLOTS = 20


def get_github_accounts() -> list[dict[str, Any]]:
    raw = addon_state.get(ADDON, "github_accounts", None)
    return list(raw) if isinstance(raw, list) else []


def github_slots_full() -> bool:
    return len(get_github_accounts()) >= MAX_GITHUB_SLOTS


def _save_github_accounts(accounts: list[dict[str, Any]]) -> None:
    addon_state.set_(ADDON, "github_accounts", accounts)


def add_github_account(
    name: str,
    owner: str,
    token_ref: str = "GITHUB_TOKEN",
    default_visibility: str = "private",
    token: str = "",
) -> dict[str, Any]:
    if github_slots_full():
        raise ValueError(f"Достигнут лимит {MAX_GITHUB_SLOTS} слотов GitHub")
    acc = {
        "id": str(uuid.uuid4()),
        "name": name.strip() or owner,
        "owner": owner.strip(),
        "token_ref": token_ref.strip() or "GITHUB_TOKEN",
        "token": token.strip(),
        "default_visibility": default_visibility if default_visibility in {"public", "private"} else "private",
        "added_at": int(time.time()),
    }
    accs = get_github_accounts()
    accs.append(acc)
    _save_github_accounts(accs)
    if not get_active_github_id():
        set_active_github_id(acc["id"])
    return acc


def remove_github_account(account_id: str) -> bool:
    accs = get_github_accounts()
    kept = [a for a in accs if a.get("id") != account_id]
    if len(kept) == len(accs):
        return False
    _save_github_accounts(kept)
    if get_active_github_id() == account_id:
        set_active_github_id(kept[0]["id"] if kept else None)
    return True


def get_active_github_id() -> Optional[str]:
    raw = addon_state.get(ADDON, "active_github_id", None)
    return str(raw) if isinstance(raw, str) and raw else None


def set_active_github_id(account_id: Optional[str]) -> None:
    addon_state.set_(ADDON, "active_github_id", account_id or "")


def get_active_github_account() -> Optional[dict[str, Any]]:
    aid = get_active_github_id()
    if not aid:
        return None
    for a in get_github_accounts():
        if a.get("id") == aid:
            return a
    return None


# --- per-slot auto-edit repo (for «Редактировать все репозитории») -----

def _update_github_account(account_id: str, **fields: Any) -> bool:
    accs = get_github_accounts()
    hit = False
    for a in accs:
        if a.get("id") == account_id:
            a.update(fields)
            hit = True
            break
    if hit:
        _save_github_accounts(accs)
    return hit


def set_auto_edit_repo(account_id: str, repo: str) -> bool:
    """Pin the repository this slot auto-edits during the walk."""
    return _update_github_account(account_id, auto_edit_repo=(repo or "").strip())


def get_auto_edit_repo(account_id: str) -> str:
    for a in get_github_accounts():
        if a.get("id") == account_id:
            return str(a.get("auto_edit_repo", "") or "")
    return ""


def set_last_uploaded_repo(account_id: str, repo: str) -> bool:
    """Remember the last repo pushed on this slot (default for auto-edit)."""
    if not account_id:
        return False
    return _update_github_account(account_id, last_uploaded_repo=(repo or "").strip())


def get_last_uploaded_repo(account_id: str) -> str:
    for a in get_github_accounts():
        if a.get("id") == account_id:
            return str(a.get("last_uploaded_repo", "") or "")
    return ""


# ─── Deploy auto-settings (private / README / auto-name counter) ─────────────

def get_gh_auto_private() -> bool:
    """Whether new repos are created private (default True)."""
    return bool(addon_state.get(ADDON, "gh_auto_private", True))


def set_gh_auto_private(value: bool) -> None:
    addon_state.set_(ADDON, "gh_auto_private", bool(value))


def get_gh_auto_readme() -> bool:
    """Whether new repos are auto-initialised with a README (default True)."""
    return bool(addon_state.get(ADDON, "gh_auto_readme", True))


def set_gh_auto_readme(value: bool) -> None:
    addon_state.set_(ADDON, "gh_auto_readme", bool(value))


# ─── ZIP project export toggle (⚙️ Выгрузка ZIP) ──────────────────────
def get_zip_export_enabled() -> bool:
    """Whether the «📥 Скачать ZIP проекта» action is allowed (default False
    for safety — the full source tree is sensitive)."""
    return bool(addon_state.get(ADDON, "zip_export_enabled", False))


def set_zip_export_enabled(value: bool) -> None:
    addon_state.set_(ADDON, "zip_export_enabled", bool(value))


def toggle_zip_export_enabled() -> bool:
    new = not get_zip_export_enabled()
    set_zip_export_enabled(new)
    return new


def next_auto_repo_name() -> str:
    """Return an incrementing auto name: zagruzka_1, zagruzka_2, ..."""
    n = int(addon_state.get(ADDON, "gh_auto_name_counter", 0)) + 1
    addon_state.set_(ADDON, "gh_auto_name_counter", n)
    return f"zagruzka_{n}"


# ─── Render.com browser-deploy settings ───────────────────────────────────
# These drive the «🖥 render.com» screen: the value typed for the BOT_TOKEN
# env-var on Render, the GitHub token used to sign in to render.com via the
# stealth browser, an auto-deploy toggle (deploy to Render right after a
# successful GitHub push), and the name of the last project deployed (so the
# suspend phase knows which old service to put to sleep).

def get_render_bot_token() -> str:
    """Value typed into Render's BOT_TOKEN env-var field (the user's «123»)."""
    return str(addon_state.get(ADDON, "render_bot_token", "") or "")


def set_render_bot_token(value: str) -> None:
    addon_state.set_(ADDON, "render_bot_token", value or "")


def get_render_github_token() -> str:
    """GitHub token/credential used to sign in to render.com via the browser."""
    return str(addon_state.get(ADDON, "render_github_token", "") or "")


def set_render_github_token(value: str) -> None:
    addon_state.set_(ADDON, "render_github_token", value or "")


def get_render_auto() -> bool:
    """Whether a successful GitHub push auto-triggers a Render deploy."""
    return bool(addon_state.get(ADDON, "render_auto", False))


def set_render_auto(value: bool) -> None:
    addon_state.set_(ADDON, "render_auto", bool(value))


def get_render_last_project() -> str:
    """Name of the previously deployed Render service (suspend target)."""
    return str(addon_state.get(ADDON, "render_last_project", "") or "")


def set_render_last_project(value: str) -> None:
    addon_state.set_(ADDON, "render_last_project", value or "")


def get_render_pending() -> dict[str, str]:
    """Repo queued for a manual «🖥 Render на сервер» button press."""
    raw = addon_state.get(ADDON, "render_pending", {})
    return raw if isinstance(raw, dict) else {}


def set_render_pending(repo_name: str, owner: str) -> None:
    addon_state.set_(ADDON, "render_pending", {"repo": repo_name or "", "owner": owner or ""})


# ─── Render.com: способ входа (login method) ─────────────────────────────────
# Три способа авторизации на render.com перед деплоем:
#   "github"  — вход через GitHub OAuth (реализовано)
#   "email"   — render email + пароль (в разработке)
#   "cookie"  — импорт cookie / storage_state один раз (в разработке)

RENDER_LOGIN_METHODS = ("github", "email", "cookie")


def get_render_login_method() -> str:
    """Selected render.com login method (default ``github`` — GitHub OAuth)."""
    val = str(addon_state.get(ADDON, "render_login_method", "github") or "github")
    return val if val in RENDER_LOGIN_METHODS else "github"


def set_render_login_method(value: str) -> None:
    addon_state.set_(
        ADDON, "render_login_method",
        value if value in RENDER_LOGIN_METHODS else "github",
    )


def get_render_gh_login() -> str:
    """GitHub web username/email used for the OAuth sign-in flow on render.com.

    This is the *web* login (заходим на github.com через браузер), а не PAT —
    PAT для веб-сессии не годится.
    """
    return str(addon_state.get(ADDON, "render_gh_login", "") or "")


def set_render_gh_login(value: str) -> None:
    addon_state.set_(ADDON, "render_gh_login", value or "")


def get_render_gh_password() -> str:
    """GitHub web password used for the OAuth sign-in flow on render.com."""
    return str(addon_state.get(ADDON, "render_gh_password", "") or "")


def set_render_gh_password(value: str) -> None:
    addon_state.set_(ADDON, "render_gh_password", value or "")


def get_gh_web_login() -> str:
    """GitHub web username/email entered by the owner for the «вход в GitHub» flow."""
    return str(addon_state.get(ADDON, "gh_web_login", "") or "")


def set_gh_web_login(value: str) -> None:
    addon_state.set_(ADDON, "gh_web_login", value or "")


def get_gh_web_password() -> str:
    """GitHub web password entered by the owner for the «вход в GitHub» flow."""
    return str(addon_state.get(ADDON, "gh_web_password", "") or "")


def set_gh_web_password(value: str) -> None:
    addon_state.set_(ADDON, "gh_web_password", value or "")


def get_render_email() -> str:
    """Render account email (login method «email» — прямой вход render email+пароль)."""
    return str(addon_state.get(ADDON, "render_email", "") or "")


def set_render_email(value: str) -> None:
    addon_state.set_(ADDON, "render_email", value or "")


def get_render_password() -> str:
    """Render account password (login method «email»)."""
    return str(addon_state.get(ADDON, "render_password", "") or "")


def set_render_password(value: str) -> None:
    addon_state.set_(ADDON, "render_password", value or "")


# ─── Render.com: способ публикации + «Прочие» тумблеры ─────────────────────

def get_render_method() -> str:
    """Publish method: ``normal`` (default) or ``browser`` (stealth)."""
    val = str(addon_state.get(ADDON, "render_method", "normal") or "normal")
    return val if val in ("normal", "browser") else "normal"


def set_render_method(value: str) -> None:
    addon_state.set_(ADDON, "render_method", value if value in ("normal", "browser") else "normal")


def get_render_delete_prev() -> bool:
    """«Прочие» → delete the previous project instead of suspending it."""
    return bool(addon_state.get(ADDON, "render_delete_prev", False))


def set_render_delete_prev(value: bool) -> None:
    addon_state.set_(ADDON, "render_delete_prev", bool(value))


def get_render_pause_prev() -> bool:
    """«Прочие» → suspend (pause) the previous project (default on)."""
    return bool(addon_state.get(ADDON, "render_pause_prev", True))


def set_render_pause_prev(value: bool) -> None:
    addon_state.set_(ADDON, "render_pause_prev", bool(value))


def get_render_use_analyzer() -> bool:
    """«Прочие» → use Analyzer PRO to locate functions/elements (default off)."""
    return bool(addon_state.get(ADDON, "render_use_analyzer", False))


def set_render_use_analyzer(value: bool) -> None:
    addon_state.set_(ADDON, "render_use_analyzer", bool(value))


def get_github_account(account_id: str) -> Optional[dict[str, Any]]:
    for a in get_github_accounts():
        if a.get("id") == account_id:
            return a
    return None


# --- deploy/preview targets (Brains pattern) --------------------------

_BUILTIN_TARGETS = {
    "render": {"name": "🚀 Render", "url_template": ""},
    "oracle": {"name": "🐙 Oracle", "url_template": ""},
}


def _targets_root() -> dict[str, Any]:
    raw = addon_state.get(ADDON, "targets", None)
    if not isinstance(raw, dict):
        return {"render": dict(_BUILTIN_TARGETS["render"]),
                "oracle": dict(_BUILTIN_TARGETS["oracle"]),
                "custom": []}
    out = dict(raw)
    out.setdefault("render", dict(_BUILTIN_TARGETS["render"]))
    out.setdefault("oracle", dict(_BUILTIN_TARGETS["oracle"]))
    if not isinstance(out.get("custom"), list):
        out["custom"] = []
    return out


def _save_targets(targets: dict[str, Any]) -> None:
    addon_state.set_(ADDON, "targets", targets)


def get_target(target_id: str) -> Optional[dict[str, Any]]:
    t = _targets_root()
    if target_id in {"render", "oracle"}:
        bucket = dict(t.get(target_id) or {})
        return {"id": target_id, "name": bucket.get("name", _BUILTIN_TARGETS[target_id]["name"]),
                "url_template": str(bucket.get("url_template", "") or "")}
    if target_id.startswith("custom:"):
        cid = target_id.split(":", 1)[1]
        for c in t.get("custom", []) or []:
            if c.get("id") == cid:
                return {"id": target_id, "name": str(c.get("name", "") or ""),
                        "url_template": str(c.get("url_template", "") or "")}
    return None


def set_target_url(target_id: str, url_template: str) -> bool:
    """Update the URL template for a preset slot. Returns True on success."""
    if target_id not in {"render", "oracle"}:
        return False
    t = _targets_root()
    bucket = dict(t.get(target_id) or _BUILTIN_TARGETS[target_id])
    bucket["url_template"] = url_template.strip()
    t[target_id] = bucket
    _save_targets(t)
    return True


def add_custom_target(name: str, url_template: str) -> dict[str, Any]:
    target = {
        "id": str(uuid.uuid4()),
        "name": name.strip() or "custom",
        "url_template": url_template.strip(),
    }
    t = _targets_root()
    custom = list(t.get("custom", []) or [])
    custom.append(target)
    t["custom"] = custom
    _save_targets(t)
    return {"id": f"custom:{target['id']}", "name": target["name"],
            "url_template": target["url_template"]}


def remove_custom_target(custom_id: str) -> bool:
    """``custom_id`` is the raw uuid (NOT the ``custom:<uuid>`` form)."""
    t = _targets_root()
    custom = list(t.get("custom", []) or [])
    kept = [c for c in custom if c.get("id") != custom_id]
    if len(kept) == len(custom):
        return False
    t["custom"] = kept
    _save_targets(t)
    if get_active_target_id() == f"custom:{custom_id}":
        set_active_target_id(DEFAULT_TARGET_ID)
    return True


def list_custom_targets() -> list[dict[str, Any]]:
    """Return custom targets with ``custom:<uuid>`` ids for UI consumption."""
    out = []
    for c in _targets_root().get("custom", []) or []:
        out.append({
            "id": f"custom:{c.get('id', '')}",
            "name": str(c.get("name", "") or ""),
            "url_template": str(c.get("url_template", "") or ""),
        })
    return out


def get_active_target_id() -> str:
    raw = addon_state.get(ADDON, "active_target_id", None)
    if isinstance(raw, str) and raw:
        return raw
    return DEFAULT_TARGET_ID


def set_active_target_id(target_id: str) -> None:
    addon_state.set_(ADDON, "active_target_id", target_id)


def get_active_target() -> Optional[dict[str, Any]]:
    return get_target(get_active_target_id())


# --- projects ---------------------------------------------------------

def get_projects() -> list[dict[str, Any]]:
    raw = addon_state.get(ADDON, "projects", None)
    return list(raw) if isinstance(raw, list) else []


def _save_projects(projects: list[dict[str, Any]]) -> None:
    addon_state.set_(ADDON, "projects", projects)


def get_project(project_id: str) -> Optional[dict[str, Any]]:
    for p in get_projects():
        if p.get("id") == project_id:
            return p
    return None


def add_project(project: dict[str, Any]) -> dict[str, Any]:
    project = dict(project)
    project.setdefault("id", str(uuid.uuid4()))
    project.setdefault("created_at", int(time.time()))
    project.setdefault("status", "queued")
    projects = get_projects()
    projects.insert(0, project)  # newest first
    _save_projects(projects)
    return project


def update_project(project_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    projects = get_projects()
    for i, p in enumerate(projects):
        if p.get("id") == project_id:
            projects[i] = {**p, **fields}
            _save_projects(projects)
            return projects[i]
    return None


def remove_project(project_id: str) -> bool:
    projects = get_projects()
    kept = [p for p in projects if p.get("id") != project_id]
    if len(kept) == len(projects):
        return False
    _save_projects(kept)
    return True


# --- defaults ---------------------------------------------------------

def get_defaults() -> dict[str, Any]:
    raw = addon_state.get(ADDON, "defaults", None) or {}
    return {
        "stack": str(raw.get("stack", DEFAULT_STACK)) or DEFAULT_STACK,
        "budget": int(raw.get("budget", DEFAULT_BUDGET) or DEFAULT_BUDGET),
        "target_id": str(raw.get("target_id", DEFAULT_TARGET_ID)) or DEFAULT_TARGET_ID,
    }


def set_defaults(stack: str | None = None, budget: int | None = None,
                 target_id: str | None = None) -> dict[str, Any]:
    d = get_defaults()
    if stack is not None and stack in STACK_CHOICES:
        d["stack"] = stack
    if budget is not None and budget in BUDGET_CHOICES:
        d["budget"] = budget
    if target_id is not None:
        d["target_id"] = target_id
    addon_state.set_(ADDON, "defaults", d)
    return d


# --- sync («Персональные данные и синхронизация», ЧАСТЬ 18.1) ---------
#
# Всё под ``addons.masterroom.sync`` (dict). Запрещено класть сюда
# секреты: НИ `DATABASE_URL`, ни пароли, ни ключи — только env-ref-имена
# (паттерн ``token_ref`` GitHub-аккаунтов выше). ``db_label`` — челове-
# кочитаемое назначение БЕЗ пароля/токена; ``last_error_public`` —
# безопасный текст ошибки БЕЗ connection string / cookie / перс. данных.

SYNC_DB_STATUSES: tuple[str, ...] = ("not_connected", "checking", "ready", "error")


def _sync_default_retention() -> dict[str, bool]:
    from .sync.categories import ALL_CATEGORIES
    # «Оставлять локально» — default ВСЕ True (ничего не удаляем локально).
    return {c.value: True for c in ALL_CATEGORIES}


def _sync_defaults() -> dict[str, Any]:
    return {
        "db_status": "not_connected",
        "db_label": "",
        "last_sync_at": None,
        "retention": _sync_default_retention(),
        "paused": False,
        "last_error_public": "",
    }


def get_sync_state() -> dict[str, Any]:
    raw = addon_state.get(ADDON, "sync", None)
    base = _sync_defaults()
    if isinstance(raw, dict):
        base.update(raw)
        # retention всегда покрывает все категории (новые — True)
        ret = _sync_default_retention()
        if isinstance(raw.get("retention"), dict):
            ret.update({k: bool(v) for k, v in raw["retention"].items()})
        base["retention"] = ret
    return base


def _save_sync_state(sync: dict[str, Any]) -> None:
    addon_state.set_(ADDON, "sync", sync)


def set_sync_db_status(status: str) -> None:
    if status not in SYNC_DB_STATUSES:
        status = "error"
    s = get_sync_state()
    s["db_status"] = status
    _save_sync_state(s)


def set_sync_db_label(label: str) -> None:
    """Назначение БД без секретов, например «Neon (host …neon.tech, db mydb)»."""
    s = get_sync_state()
    s["db_label"] = (label or "").strip()
    _save_sync_state(s)


def set_sync_last_sync_at(epoch: int | None) -> None:
    s = get_sync_state()
    s["last_sync_at"] = int(epoch) if epoch else None
    _save_sync_state(s)


def get_sync_retention() -> dict[str, bool]:
    return dict(get_sync_state()["retention"])


def set_sync_retention(category: str, keep_local: bool) -> None:
    s = get_sync_state()
    s["retention"][str(category)] = bool(keep_local)
    _save_sync_state(s)


def is_sync_paused() -> bool:
    return bool(get_sync_state()["paused"])


def set_sync_paused(paused: bool) -> None:
    s = get_sync_state()
    s["paused"] = bool(paused)
    _save_sync_state(s)


def get_sync_bootstrap() -> dict[str, Any]:
    """Компактный снапшот прогресса bootstrap (18.5). Пустой dict — не было."""
    raw = get_sync_state().get("bootstrap")
    return dict(raw) if isinstance(raw, dict) else {}


def set_sync_bootstrap(compact: dict[str, Any]) -> None:
    """ТОЛЬКО компактные счётчики/статусы (18.1): никаких payload'ов,
    журналов или секретов — журнал живёт в sync_journal.db, не тут."""
    s = get_sync_state()
    s["bootstrap"] = dict(compact or {})
    _save_sync_state(s)


def set_sync_last_error_public(text: str) -> None:
    """Безопасный текст ошибки. Санитизация НА ЗАПИСИ (18.2 §2):
    connection string / токены / cookie вырезаются до попадания в state,
    даже если вызывающий по ошибке передал сырой текст исключения."""
    from .sync.sanitize import sanitize_error_public
    s = get_sync_state()
    s["last_error_public"] = sanitize_error_public(text)
    _save_sync_state(s)


# --- bootstrap --------------------------------------------------------

def reserve_defaults() -> dict[str, Any]:
    """Seed the full ``addons.masterroom`` namespace once on startup.

    Idempotent — only writes missing keys, never overwrites existing
    user data. Keeps the schema present so future migrations don't
    trip on AttributeError.
    """
    if addon_state.get(ADDON, "enabled", None) is None:
        addon_state.set_(ADDON, "enabled", False)
    if addon_state.get(ADDON, "token", None) is None:
        addon_state.set_(ADDON, "token", "")
    if addon_state.get(ADDON, "rotated_at", None) is None:
        addon_state.set_(ADDON, "rotated_at", 0)
    if addon_state.get(ADDON, "servers", None) is None:
        addon_state.set_(ADDON, "servers", [])
    if addon_state.get(ADDON, "active_server_id", None) is None:
        addon_state.set_(ADDON, "active_server_id", "")
    if addon_state.get(ADDON, "github_accounts", None) is None:
        addon_state.set_(ADDON, "github_accounts", [])
    if addon_state.get(ADDON, "active_github_id", None) is None:
        addon_state.set_(ADDON, "active_github_id", "")
    if addon_state.get(ADDON, "targets", None) is None:
        addon_state.set_(ADDON, "targets", {
            "render": dict(_BUILTIN_TARGETS["render"]),
            "oracle": dict(_BUILTIN_TARGETS["oracle"]),
            "custom": [],
        })
    if addon_state.get(ADDON, "active_target_id", None) is None:
        addon_state.set_(ADDON, "active_target_id", DEFAULT_TARGET_ID)
    if addon_state.get(ADDON, "projects", None) is None:
        addon_state.set_(ADDON, "projects", [])
    if addon_state.get(ADDON, "defaults", None) is None:
        addon_state.set_(ADDON, "defaults", {
            "stack": DEFAULT_STACK,
            "budget": DEFAULT_BUDGET,
            "target_id": DEFAULT_TARGET_ID,
        })
    if addon_state.get(ADDON, "sync", None) is None:
        addon_state.set_(ADDON, "sync", _sync_defaults())
    return {
        "enabled": is_enabled(),
        "token": get_token(),
        "rotated_at": get_rotated_at(),
    }
