"""Render.com browser-automation deploy (the «🖥 render.com» button).

This drives a deploy on https://dashboard.render.com/ through the existing
stealth browser macro engine (``workers.browser_clicker.run_browser_macro``),
so it reuses the humanized cursor, fingerprint and proxy machinery untouched.

PHASE 0 — Sign in to render.com (optional, before everything else). Driven by
the «🔐 Способ входа» menu. Currently the GitHub-OAuth method is implemented:
click «Continue with GitHub», fill the GitHub login form (if logged out) and
authorize the Render app once. The session is saved to ``render_session.json``
so later runs are already signed in. The steps are best-effort and no-op when
a session already exists.

Then two phases, exactly mirroring the user's manual flow:

PHASE 1 — Suspend the previous service (skipped on the very first deploy):
    1. open dashboard
    2. open the old service (by its name, e.g. ``zagruzka_6``)
    3. ⋮ overflow menu → «Suspend»
    4. type the red confirm phrase ``sudo suspend web service <old>``
    5. «Suspend Web Service»

PHASE 2 — Create the new web service for the freshly-pushed repo:
    1. open dashboard → «Create new project»
    2. type the project name (= repo name, e.g. ``zagruzka_7``)
    3. «Create new service» → «New Web Service»
    4. pick the GitHub repo row (``owner/zagruzka_7``)
    5. choose the Free hobby tier (512 MB / 0.1 CPU)
    6. Environment Variables: ``BOT_TOKEN`` = <stored value>
    7. «Advanced» → Health Check Path ``/healthz``
    8. «Deploy web service»

Render's dashboard is a React SPA, so the macro selects elements by their
visible button/label text wherever possible (the macro engine tries
``text=``, ``button:has-text()``, ``a:has-text()`` and input placeholder /
name / id). Steps that depend on exact wording are best-effort and may need
small text tweaks if Render changes its UI.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://dashboard.render.com/"
HEALTH_CHECK_PATH = "/healthz"
ENV_VAR_NAME = "BOT_TOKEN"


def build_suspend_steps(old_project: str) -> list[tuple[str, Any]]:
    """Macro steps that suspend the previously-deployed service."""
    confirm_phrase = f"sudo suspend web service {old_project}"
    return [
        ("goto", DASHBOARD_URL),
        ("pause", 5),
        # Enter the green «All services are up and running» group, then the
        # old service itself. Clicking the service name is the robust anchor.
        ("click", "All services are up and running"),
        ("pause", 2),
        ("click", old_project),
        ("pause", 3),
        # ⋮ overflow menu (no text label — try the common glyphs/aria labels).
        ("click", "⋮"),
        ("pause", 1),
        ("click", "Suspend"),
        ("pause", 2),
        # Red confirmation: the input's placeholder equals the phrase, so we
        # match on it and type the very same phrase.
        ("type", (confirm_phrase, confirm_phrase)),
        ("pause", 1),
        ("click", "Suspend Web Service"),
        ("pause", 4),
    ]


def build_delete_steps(old_project: str) -> list[tuple[str, Any]]:
    """Macro steps that permanently delete the previously-deployed service.

    Render's delete confirmation asks you to type the service name, so we
    type ``old_project`` into the confirm field.
    """
    return [
        ("goto", DASHBOARD_URL),
        ("pause", 5),
        ("click", "All services are up and running"),
        ("pause", 2),
        ("click", old_project),
        ("pause", 3),
        # Service → Settings → scroll to the danger zone.
        ("click", "Settings"),
        ("pause", 2),
        ("click", "Delete Web Service"),
        ("pause", 2),
        # Confirm input: Render asks to retype the service name.
        ("type", (old_project, old_project)),
        ("pause", 1),
        ("click", "Delete Web Service"),
        ("pause", 4),
    ]


def build_create_steps(repo_name: str, owner: str, bot_token: str) -> list[tuple[str, Any]]:
    """Macro steps that create + deploy a new Free web service for the repo."""
    repo_full = f"{owner}/{repo_name}" if owner else repo_name
    return [
        ("goto", DASHBOARD_URL),
        ("pause", 5),
        ("click", "Create new project"),
        ("pause", 2),
        # Project name field — best-effort placeholder match.
        ("type", (repo_name, "Project name")),
        ("pause", 1),
        ("click", "Create new service"),
        ("pause", 1),
        ("click", "New Web Service"),
        ("pause", 3),
        # Select the just-pushed repo. Try the full «owner/repo» row first,
        # then the bare repo name as a fallback row label.
        ("click", repo_full),
        ("pause", 1),
        ("click", repo_name),
        ("pause", 2),
        # Free hobby tier: 512 MB RAM / 0.1 CPU.
        ("click", "Free"),
        ("pause", 1),
        # Environment Variables: two empty fields → key then value.
        ("type", (ENV_VAR_NAME, "Key")),
        ("type", (bot_token, "Value")),
        ("pause", 1),
        # Expand Advanced to reveal the Health Check Path field.
        ("click", "Advanced"),
        ("pause", 1),
        ("type", (HEALTH_CHECK_PATH, "Health Check Path")),
        ("pause", 1),
        ("click", "Deploy web service"),
        ("pause", 6),
        # Grab the resulting page text so the caller can VERIFY the deploy
        # actually happened (a click that silently missed does not raise).
        ("copy", "body"),
    ]


GITHUB_LOGIN_URL = "https://github.com/login"


def build_github_login_steps(gh_login: str, gh_password: str) -> list[tuple[str, Any]]:
    """Macro steps that sign in to render.com via GitHub OAuth (вариант «GitHub OAuth»).

    Best-effort by design: the macro engine clicks/типизирует by visible text
    and logs (without failing) when a target is missing. So when a render
    session already exists (saved ``storage_state``), we land straight on the
    dashboard and the sign-in clicks below simply no-op. On a clean machine
    render shows its login screen → we click the GitHub provider, then (if
    GitHub itself is logged out) fill its login form and authorize the
    Render OAuth app once. The resulting cookies are persisted by the engine
    into ``render_session.json`` so subsequent deploys are already signed in.
    """
    steps: list[tuple[str, Any]] = [
        ("goto", DASHBOARD_URL),
        ("pause", 6),
        # Render's own login page → choose the GitHub provider. Text varies
        # ("GitHub" / "Continue with GitHub" / "Sign in with GitHub"); the
        # engine's best-match click handles the partial label.
        ("click", "GitHub"),
        ("pause", 5),
    ]
    # GitHub may still have a live session (then it auto-redirects back and we
    # only need the Authorize step). If it is logged out, fill its form.
    if gh_login:
        # GitHub username/email field: name="login" (id="login_field").
        steps += [("type", (gh_login, "login")), ("pause", 1)]
    if gh_password:
        # GitHub password field: name="password".
        steps += [("type", (gh_password, "password")), ("pause", 1)]
    if gh_login or gh_password:
        steps += [("click", "Sign in"), ("pause", 5)]
    # OAuth consent screen — shown only on the first authorization.
    steps += [("click", "Authorize"), ("pause", 6)]
    return steps


def build_email_login_steps(email: str, password: str) -> list[tuple[str, Any]]:
    """Macro steps for the direct render email + password sign-in (вариант «Email + пароль»).

    Render's own login page exposes an email + password form alongside the
    OAuth providers. Best-effort like the GitHub flow: no-op когда сессия уже
    сохранена (мы уже на dashboard), иначе заполняет форму и жмёт «Sign in».
    The resulting cookies are persisted into ``render_session.json``.
    """
    steps: list[tuple[str, Any]] = [
        ("goto", DASHBOARD_URL),
        ("pause", 6),
    ]
    if email:
        # Render's email field: name/id="email" (placeholder «Email»).
        steps += [("type", (email, "email")), ("pause", 1)]
    if password:
        # Render's password field: name/id="password".
        steps += [("type", (password, "password")), ("pause", 1)]
    if email or password:
        steps += [("click", "Sign in"), ("pause", 5)]
    return steps


async def deploy_to_render(
    repo_name: str,
    owner: str,
    *,
    bot_token: str,
    github_token: str = "",
    old_project: str = "",
    delete_prev: bool = False,
    pause_prev: bool = True,
    method: str = "normal",
    use_analyzer: bool = False,
    login_method: str = "github",
    gh_login: str = "",
    gh_password: str = "",
    email: str = "",
    password: str = "",
    screenshot_dir: str | None = None,
    progress=None,
) -> dict[str, Any]:
    """Run the previous-project cleanup (optional) + create phases on render.com.

    ``delete_prev`` wins over ``pause_prev``: if set, the old service is
    permanently deleted; otherwise, if ``pause_prev`` is set, it is suspended.
    ``method`` (``normal``/``browser``) and ``use_analyzer`` are carried into
    the report for now — both publish paths drive the same stealth macro
    engine.

    Returns a report dict:
        {
          "ok": bool,                # create phase succeeded (no engine error)
          "prev_action": "delete"|"suspend"|None,
          "suspend": {...} | None,   # raw macro result of the cleanup phase
          "create": {...},           # raw macro result of the create phase
          "method": str, "use_analyzer": bool,
          "screenshots": [paths],
          "error": str | None,
        }
    """
    from ...workers.browser_clicker import run_browser_macro

    try:
        from ...config import DATA_DIR
        base = str(DATA_DIR / "session")
    except Exception:  # noqa: BLE001 — config import should not be fatal
        base = screenshot_dir or "session"
    if screenshot_dir:
        base = screenshot_dir

    report: dict[str, Any] = {
        "ok": False, "prev_action": None, "suspend": None, "create": None,
        "login": None, "login_method": login_method,
        "method": method, "use_analyzer": use_analyzer,
        "verified": False,
        "screenshots": [], "error": None,
    }

    session_path = f"{base}/render_session.json"

    # --- Phase 0 (optional): sign in to render.com -------------------------
    # The login steps are best-effort and no-op when a saved session already
    # exists, so it is safe to always run them when a method is selected.
    #   github — GitHub OAuth          (build_github_login_steps)
    #   email  — render email+password (build_email_login_steps)
    #   cookie — imported storage_state: no steps, мы просто используем уже
    #            сохранённую сессию render_session.json.
    login_steps = None
    if login_method == "github":
        login_steps = build_github_login_steps(gh_login, gh_password)
    elif login_method == "email":
        login_steps = build_email_login_steps(email, password)

    # Pre-compute the cleanup steps so we know the combined progress total
    # (each build_* starts with a leading "goto" that the engine strips, so we
    # subtract 1 per phase to match the engine's per-step reporting).
    prev_steps = None
    shot_name = ""
    if old_project and old_project != repo_name:
        if delete_prev:
            logger.info("Render deploy: DELETING old service %s", old_project)
            prev_steps = build_delete_steps(old_project)
            report["prev_action"] = "delete"
            shot_name = "render_delete.png"
        elif pause_prev:
            logger.info("Render deploy: suspending old service %s", old_project)
            prev_steps = build_suspend_steps(old_project)
            report["prev_action"] = "suspend"
            shot_name = "render_suspend.png"

    create_steps = build_create_steps(repo_name, owner, bot_token)
    phase0_total = (len(login_steps) - 1) if login_steps else 0
    phase1_total = (len(prev_steps) - 1) if prev_steps else 0
    phase2_total = len(create_steps) - 1
    grand_total = phase0_total + phase1_total + phase2_total

    async def _emit(done: int, op: str, label: str) -> None:
        if progress:
            try:
                await progress(done, grand_total, op, label)
            except Exception:  # noqa: BLE001
                pass

    # --- Phase 0: sign in to render.com (best-effort, GitHub OAuth) ---------
    if login_steps:
        logger.info("Render deploy: signing in via %s (login phase)", login_method)

        async def _p0(i: int, t: int, op: str) -> None:
            await _emit(i, op, "🔐 Вхожу на render")

        login_res = await run_browser_macro(
            login_steps, screenshot_path=f"{base}/render_login.png",
            progress_cb=_p0, session_state_path=session_path)
        report["login"] = login_res
        shot = login_res.get("screenshot")
        if shot:
            report["screenshots"].append(shot)

    # --- Phase 1: clean up the previous service (skip on first ever deploy) --
    if prev_steps:
        cleanup_label = "🗑 Удаляю старый" if report["prev_action"] == "delete" else "💤 Гашу старый"

        async def _p1(i: int, t: int, op: str) -> None:
            await _emit(phase0_total + i, op, cleanup_label)

        prev_res = await run_browser_macro(
            prev_steps, screenshot_path=f"{base}/{shot_name}",
            progress_cb=_p1, session_state_path=session_path)
        report["suspend"] = prev_res
        shot = prev_res.get("screenshot")
        if shot:
            report["screenshots"].append(shot)

    # --- Phase 2: create + deploy the new web service -----------------------
    logger.info("Render deploy: creating service for %s/%s", owner, repo_name)

    async def _p2(i: int, t: int, op: str) -> None:
        await _emit(phase0_total + phase1_total + i, op, "🚀 Создаю новый")

    create_res = await run_browser_macro(
        create_steps, screenshot_path=f"{base}/render_create.png",
        progress_cb=_p2, session_state_path=session_path)
    report["create"] = create_res
    shot = create_res.get("screenshot")
    if shot:
        report["screenshots"].append(shot)

    # --- Honest success check ----------------------------------------------
    # The engine sets ok=False only on an exception. A click that silently
    # missed its target does NOT raise, so the macro can "succeed" having done
    # nothing. We therefore inspect the final page text grabbed by the trailing
    # ("copy", "body") step and confirm we are NOT stuck on a login page and
    # that the create/deploy screen is actually showing.
    engine_ok = not create_res.get("error")
    page_text = " ".join(create_res.get("copied_texts") or []).lower()
    login_markers = ("sign in", "log in", "войти", "continue with github",
                     "sign in to render")
    success_markers = (repo_name.lower(), "deploying", "your service is live",
                       "live", "build", "deploy started", "dashboard",
                       "web service")
    looks_logged_out = (
        any(m in page_text for m in login_markers)
        and not any(m in page_text for m in success_markers)
    )
    confirmed = any(m in page_text for m in success_markers)
    report["verified"] = bool(confirmed)

    if not engine_ok:
        report["ok"] = False
        report["error"] = create_res.get("error")
    elif looks_logged_out:
        report["ok"] = False
        report["error"] = (
            "Похоже, бот не залогинен на render (показана страница входа). "
            "Настрой «🔐 Способ входа»."
        )
    elif page_text and not confirmed:
        report["ok"] = False
        report["error"] = (
            "Не удалось подтвердить создание сервиса — нужные элементы не "
            "найдены (возможно, изменилась вёрстка render). Проверь скриншот."
        )
    else:
        # Either confirmed, or we couldn't read the page text at all (then we
        # fall back to the engine result and flag verified=False).
        report["ok"] = True
        report["error"] = None
    return report
