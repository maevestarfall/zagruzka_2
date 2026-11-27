"""Unit tests for :func:`bot.config.resolve_public_url`.

Verifies the full fallback chain — `PUBLIC_URL` → `RENDER_EXTERNAL_URL`
→ `RAILWAY_PUBLIC_DOMAIN` → `FLY_APP_NAME` → `ORACLE_PUBLIC_HOST` —
and confirms that the optional `KEEP_ALIVE_URL` override only kicks
in when explicitly requested.

These envs are the **single source of truth** for any addon that
mints user-visible links (Fable5, Test, future Master Room) — if
this test passes, the bot is portable between Render, Railway,
Fly, Oracle Free Tier, and bare VPS.
"""

from __future__ import annotations

import pytest

from bot.config import resolve_public_url


_ALL_ENVS = (
    "PUBLIC_URL",
    "RENDER_EXTERNAL_URL",
    "RAILWAY_PUBLIC_DOMAIN",
    "FLY_APP_NAME",
    "ORACLE_PUBLIC_HOST",
    "KEEP_ALIVE_URL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Wipe every host-detection env so each test starts from zero."""
    for name in _ALL_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch


def test_no_envs_returns_empty(clean_env):
    assert resolve_public_url() == ""
    assert resolve_public_url(include_keepalive=True) == ""


def test_public_url_wins(clean_env):
    clean_env.setenv("PUBLIC_URL", "https://my-app.example.com/")
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://render-thing.onrender.com")
    clean_env.setenv("RAILWAY_PUBLIC_DOMAIN", "rw.up.railway.app")
    clean_env.setenv("FLY_APP_NAME", "flyapp")
    clean_env.setenv("ORACLE_PUBLIC_HOST", "oracle.example")
    # PUBLIC_URL is the explicit operator override → first.
    assert resolve_public_url() == "https://my-app.example.com"


def test_render_external_url_fallback(clean_env):
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://render-thing.onrender.com/")
    clean_env.setenv("RAILWAY_PUBLIC_DOMAIN", "rw.up.railway.app")
    assert resolve_public_url() == "https://render-thing.onrender.com"


def test_railway_domain_is_prefixed_with_https(clean_env):
    # Railway exposes a bare host, no scheme.
    clean_env.setenv("RAILWAY_PUBLIC_DOMAIN", "my-bot-prod.up.railway.app")
    assert resolve_public_url() == "https://my-bot-prod.up.railway.app"


def test_railway_domain_with_explicit_scheme_kept(clean_env):
    # Edge case: ops sometimes set this as a full URL.
    clean_env.setenv("RAILWAY_PUBLIC_DOMAIN", "https://my-bot.up.railway.app/")
    assert resolve_public_url() == "https://my-bot.up.railway.app"


def test_fly_app_name_expands_to_fly_dev(clean_env):
    clean_env.setenv("FLY_APP_NAME", "lilush-bot")
    assert resolve_public_url() == "https://lilush-bot.fly.dev"


def test_oracle_public_host_bare_gets_https(clean_env):
    # Oracle Free Tier path — bare hostname (DNS pointing at the VM).
    clean_env.setenv("ORACLE_PUBLIC_HOST", "bot.chloe.example")
    assert resolve_public_url() == "https://bot.chloe.example"


def test_oracle_public_host_with_scheme_kept(clean_env):
    clean_env.setenv("ORACLE_PUBLIC_HOST", "http://10.0.0.5:8080/")
    assert resolve_public_url() == "http://10.0.0.5:8080"


def test_oracle_only_when_higher_priorities_empty(clean_env):
    clean_env.setenv("FLY_APP_NAME", "flyapp")
    clean_env.setenv("ORACLE_PUBLIC_HOST", "oracle.example")
    # FLY_APP_NAME is higher in the chain → it wins.
    assert resolve_public_url() == "https://flyapp.fly.dev"


def test_keepalive_override_only_when_requested(clean_env):
    clean_env.setenv("KEEP_ALIVE_URL", "https://internal-lan-ping.example/")
    clean_env.setenv("PUBLIC_URL", "https://public.example.com")
    # Default: PUBLIC_URL wins — addons must NOT leak internal LAN.
    assert resolve_public_url() == "https://public.example.com"
    # Self-ping loop opts in explicitly.
    assert (
        resolve_public_url(include_keepalive=True)
        == "https://internal-lan-ping.example"
    )


def test_keepalive_alias_matches_include_flag(clean_env):
    """Legacy ``_resolve_keepalive_url`` must still work."""
    from bot.config import _resolve_keepalive_url

    clean_env.setenv("KEEP_ALIVE_URL", "https://ka.example/")
    clean_env.setenv("PUBLIC_URL", "https://pub.example/")
    assert _resolve_keepalive_url() == "https://ka.example"


def test_full_chain_priority_order(clean_env):
    """Walk the chain — peel envs one by one, top must keep losing."""
    clean_env.setenv("PUBLIC_URL", "https://pub.example")
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://render.example")
    clean_env.setenv("RAILWAY_PUBLIC_DOMAIN", "railway.example")
    clean_env.setenv("FLY_APP_NAME", "flyname")
    clean_env.setenv("ORACLE_PUBLIC_HOST", "oracle.example")

    assert resolve_public_url() == "https://pub.example"
    clean_env.delenv("PUBLIC_URL")
    assert resolve_public_url() == "https://render.example"
    clean_env.delenv("RENDER_EXTERNAL_URL")
    assert resolve_public_url() == "https://railway.example"
    clean_env.delenv("RAILWAY_PUBLIC_DOMAIN")
    assert resolve_public_url() == "https://flyname.fly.dev"
    clean_env.delenv("FLY_APP_NAME")
    assert resolve_public_url() == "https://oracle.example"
    clean_env.delenv("ORACLE_PUBLIC_HOST")
    assert resolve_public_url() == ""
