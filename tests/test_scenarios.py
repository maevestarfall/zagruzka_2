"""Автотесты аддона «Сценарии» (ТЗ rev.7.1 §16.4, §13).

Уровни:
1. Юнит: модель/валидатор (§3, §5), extract (§15.2), подстановка {var},
   санитайзер секретов (§11), конвертация в кортежи движка (§0.3).
2. Интеграция с движком: полный маршрут «почта A -> регистрация B» на
   локальных HTML-фикстурах через реальный ``open_browser_session``
   (обе вкладки в одной сессии — §15.1), включая поллинг кода (§15.5),
   ретраи/стоп (§15.4), отмену (§15.6) и общий бюджет времени (§15.6).
3. Обратная совместимость: ``run_browser_macro`` жив и возвращает
   прежние ключи (§13 «регрессия»).
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import os
import socket
import threading

import pytest

# ---------------------------------------------------------------------------
# Browser integration guard
# ---------------------------------------------------------------------------
# The integration tests (test_run_route_*, test_run_browser_macro_*) spin up
# a real browser via open_browser_session. In CI / headless environments where
# none of playwright / nodriver / camoufox is installed, those tests must be
# skipped rather than failing noisily. We probe at collection time so the rest
# of the unit tests in this file still run.
def _browser_engine_available() -> bool:
    for pkg in ("playwright", "nodriver", "camoufox", "cloakbrowser"):
        try:
            __import__(pkg)
            return True
        except ImportError:
            pass
    return False

_BROWSER_AVAILABLE = _browser_engine_available()
_skip_no_browser = pytest.mark.skipif(
    not _BROWSER_AVAILABLE,
    reason="No browser automation engine installed (playwright/nodriver/camoufox)",
)

from bot.addons.scenarios.models import (
    CODE6_REGEX,
    COPY_NOT_FOUND,
    EMAIL_REGEX,
    Route,
    Stage,
    Step,
    resolve_vars,
    validate_route,
)
from bot.addons.scenarios.presets import build_v0_route
from bot.addons.scenarios.runner import (
    RouteStepFailed,
    _extract_value,
    _to_engine_step,
    make_cancel_token,
    run_route,
)
from bot.addons.scenarios.store import sanitize_route_secrets

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "scenarios")


# ────────────────────────── local fixture web server ──────────────────────────

@pytest.fixture(scope="module")
def fixture_server():
    """Serve tests/fixtures/scenarios over http on an ephemeral port."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=FIXTURES
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


# ─────────────────────────────── unit: model ──────────────────────────────────

def test_validate_route_preset_is_clean():
    route = build_v0_route()
    assert validate_route(route) == []


def test_validate_route_catches_problems():
    route = Route(
        id="x", title="bad", site_a="",
        site_b=None,
        stages=[Stage(index=1, title="s", steps=[
            Step(op="wait", value=3),                       # несуществующий op
            Step(op="pause", value="долго"),                # pause не числом
            Step(op="type", target="input", value="{code}"),  # var до copy
            Step(op="copy", target="body"),                 # copy без save_as
            Step(op="click", target="text=Go", page="C"),   # вкладка не A/B
        ])],
    )
    problems = validate_route(route)
    joined = "\n".join(problems)
    assert "site_a" in joined
    assert "недопустимая операция" in joined
    assert "pause.value" in joined
    assert "{code}" in joined
    assert "save_as" in joined
    assert "A или B" in joined


def test_var_declared_by_copy_is_visible_later():
    route = Route(
        id="x", title="ok", site_a="https://a.example",
        site_b="https://b.example",
        stages=[Stage(index=1, title="s", steps=[
            Step(op="copy", target="#mail", save_as="email",
                 extract=EMAIL_REGEX),
            Step(op="type", target="input[type=email]", value="{email}",
                 page="B"),
        ])],
    )
    assert validate_route(route) == []


def test_route_json_roundtrip_and_markdown():
    route = build_v0_route()
    again = Route.from_json(route.to_json())
    assert again.to_dict() == route.to_dict()
    md = route.to_markdown()
    assert "Стадия 1" in md and route.title in md


# ─────────────────────────── unit: extract / vars ─────────────────────────────

def test_extract_value_email_and_code():
    body = "Ваш адрес: user.name+tag@mail.example.org , спасибо"
    assert _extract_value(body, EMAIL_REGEX) == "user.name+tag@mail.example.org"
    assert _extract_value("Ваш код подтверждения: 482913 —", CODE6_REGEX) == "482913"


def test_extract_value_rejects_sentinel_and_miss():
    # §0.1: заглушка движка не должна попасть в переменные
    assert _extract_value(COPY_NOT_FOUND, EMAIL_REGEX) is None
    assert _extract_value("", EMAIL_REGEX) is None
    assert _extract_value("никакого кода тут нет", CODE6_REGEX) is None


def test_resolve_vars():
    assert resolve_vars("hi {email} / {code}", {"email": "a@b.c"}) == "hi a@b.c / {code}"


def test_to_engine_step_shapes():
    # §0.3: type -> ("type", (текст, селектор)); pause в СЕКУНДАХ (§0.2)
    s = Step(op="type", target="input", value="x {email}")
    assert _to_engine_step(s, {"email": "a@b.c"}) == ("type", ("x a@b.c", "input"))
    assert _to_engine_step(Step(op="pause", value=2), {}) == ("pause", 2.0)
    assert _to_engine_step(Step(op="click", target="text=Go"), {}) == ("click", "text=Go")
    assert _to_engine_step(Step(op="type_login", target="#l"), {}) == ("type_login", "#l")
    with pytest.raises(ValueError):
        _to_engine_step(Step(op="goto", target="https://x"), {})  # §0.4


def test_sanitize_route_secrets():
    # §11: пароли/токены не должны храниться в открытом виде в маршруте
    route = Route(
        id="x", title="t", site_a="https://a", site_b=None,
        stages=[Stage(index=1, title="s", steps=[
            Step(op="type", target="input[type=password]", value="Hunter2!",
                 vault_ref="vlt:1"),
        ])],
    )
    sanitize_route_secrets(route)
    assert route.stages[0].steps[0].value is None
    assert route.stages[0].steps[0].vault_ref == "vlt:1"


# ──────────────────── integration: full route over fixtures ───────────────────

def _fixture_route(base: str, *, autocode: bool = True, poll: bool = True) -> Route:
    """Маршрут по фикстурам: почта A -> регистрация B (эталон §16.3)."""
    mail = f"{base}/site_a_mail.html" + ("?autocode=1" if autocode else "")
    signup = f"{base}/site_b_signup.html"
    return Route(
        id="fixture", title="Фикстурный маршрут", site_a=mail, site_b=signup,
        stages=[
            Stage(index=1, title="Скопировать почту", steps=[
                Step(op="copy", target="#email-box", save_as="email",
                     extract=EMAIL_REGEX, page="A"),
            ]),
            Stage(index=2, title="Регистрация", steps=[
                Step(op="click", target="text=Sign Up", page="B"),
                Step(op="type", target='input[aria-label="Email Address"]',
                     value="{email}", page="B"),
                Step(op="click", target="text=Continue with Email", page="B"),
            ]),
            Stage(index=3, title="Дождаться код", steps=[
                Step(op="copy", target="#letter", save_as="code",
                     extract=CODE6_REGEX, poll=poll, page="A"),
            ]),
            Stage(index=4, title="Подтвердить", steps=[
                Step(op="type", target='input[autocomplete="one-time-code"]',
                     value="{code}", page="B"),
                Step(op="click", target="text=Verify", page="B"),
                Step(op="copy", target="#done", save_as="final", page="B"),
            ]),
        ],
    )


@_skip_no_browser
async def test_run_route_end_to_end(fixture_server):
    """Полный маршрут: 2 вкладки, copy+extract, {var}, поллинг кода (§15.1-15.5)."""
    route = _fixture_route(fixture_server)
    assert validate_route(route) == []

    stage_events: list[tuple[str, int, str | None]] = []

    async def on_stage(phase, stage, screenshot=None):
        stage_events.append((phase, stage.index, screenshot))

    res = await run_route(
        route, on_stage=on_stage,
        poll_interval=1.0, poll_timeout=30.0, route_timeout=180.0,
        screenshot_dir="/tmp",
    )
    assert res["ok"], res.get("error")
    assert res["stages_done"] == [1, 2, 3, 4]
    assert res["vars"]["email"] == "route.tester@example.com"
    assert res["vars"]["code"] == "482913"
    assert "Welcome!" in res["vars"]["final"]           # сайт Б дошёл до финала
    assert "code=482913" in res["vars"]["final"]        # именно наш код
    # §9: на каждую стадию есть start и done, у done — скриншот (§8)
    starts = [e for e in stage_events if e[0] == "start"]
    dones = [e for e in stage_events if e[0] == "done"]
    assert len(starts) == 4 and len(dones) == 4
    assert all(shot and os.path.exists(shot) for _, _, shot in dones)
    for _, _, shot in dones:
        os.unlink(shot)


@_skip_no_browser
async def test_run_route_stops_on_missing_element(fixture_server):
    """§15.4: критичный click не нашёл элемент -> ретраи -> явный стоп."""
    route = _fixture_route(fixture_server)
    route.stages[1].steps[0] = Step(
        op="click", target="text=Кнопки Нет Такой", page="B",
    )
    res = await run_route(route, route_timeout=120.0, screenshot_dir="/tmp")
    assert not res["ok"]
    assert res.get("stage") == 2 and res.get("step") == 1
    assert "Стадия 2" in res["error"]


@_skip_no_browser
async def test_run_route_poll_timeout(fixture_server):
    """§15.5: письмо не приходит -> таймаут поллинга, стоп с понятной ошибкой."""
    route = _fixture_route(fixture_server, autocode=False)
    res = await run_route(
        route, poll_interval=0.5, poll_timeout=3.0, route_timeout=60.0,
        screenshot_dir="/tmp",
    )
    assert not res["ok"]
    assert "таймаут поллинга" in res["error"]


@_skip_no_browser
async def test_run_route_cancel(fixture_server):
    """§15.6: отмена во время поллинга останавливает маршрут."""
    route = _fixture_route(fixture_server, autocode=False)
    token = make_cancel_token()

    async def cancel_soon():
        await asyncio.sleep(2.0)
        token.cancel()

    task = asyncio.ensure_future(cancel_soon())
    res = await run_route(
        route, cancel=token,
        poll_interval=0.5, poll_timeout=60.0, route_timeout=120.0,
        screenshot_dir="/tmp",
    )
    await task
    assert not res["ok"]
    assert res.get("cancelled") is True


@_skip_no_browser
async def test_run_route_total_budget(fixture_server):
    """§15.6: общий бюджет времени маршрута соблюдается."""
    route = _fixture_route(fixture_server, autocode=False)
    res = await run_route(
        route, poll_interval=0.5, poll_timeout=300.0, route_timeout=4.0,
        screenshot_dir="/tmp",
    )
    assert not res["ok"]
    assert "лимит времени" in res["error"]


# ───────────────────── regression: legacy macro contract ──────────────────────

@_skip_no_browser
async def test_run_browser_macro_backward_compat(fixture_server):
    """§13: старый контракт run_browser_macro не сломан рефакторингом."""
    from bot.workers.browser_clicker import run_browser_macro

    shot = "/tmp/scn_compat_shot.png"
    res = await run_browser_macro(
        [
            ("goto", f"{fixture_server}/site_a_mail.html"),
            ("copy", "#email-box"),
        ],
        screenshot_path=shot,
    )
    assert res["ok"], res.get("error")
    # прежние ключи на месте (§13)
    for key in ("ok", "error", "screenshot", "browser_type", "copied_texts"):
        assert key in res
    sel, text = res["copied_texts"][0]        # §0.1 — (селектор, текст)
    assert sel == "#email-box"
    assert "route.tester@example.com" in text
    assert os.path.exists(shot)
    os.unlink(shot)
