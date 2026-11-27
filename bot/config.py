# Bot configuration: env-driven settings with persistent /data default.
# Co-authored with CoCo
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _split_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


# Which farm role this process represents — see bot/persona.py. Defaults
# to "boss" so a single-bot deploy (the original @openaiopus_bot setup)
# keeps working unchanged.
BOT_PERSONA = os.environ.get("BOT_PERSONA", "boss").strip().lower()

# BOT_TOKEN is OPTIONAL in farm mode — a service can be deployed BEFORE
# the user has created its BotFather bot. Empty token → dormant mode in
# bot.main (only /healthz, no polling). Set the var in Render dashboard
# later to activate.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ALLOWED_USER_IDS = _split_ids(os.environ.get("ALLOWED_USER_IDS", ""))

# Default to polling so a freshly-deployed container works without any
# webhook URL setup. Switch to ``webhook`` + set ``PUBLIC_URL`` for
# production scale.
MODE = os.environ.get("BOT_MODE", "polling")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# WEBHOOK_PATH uses bot id prefix; if BOT_TOKEN is empty we still expose
# a deterministic /tg path so the dormant health server has a stable URL.
WEBHOOK_PATH = f"/tg/{BOT_TOKEN.split(':', 1)[0] if BOT_TOKEN else 'dormant'}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL and BOT_TOKEN else ""


def resolve_public_url(*, include_keepalive: bool = False) -> str:
    """Single source of truth for the bot's own public HTTPS base URL.

    Fallback chain (first non-empty wins):

    1. ``PUBLIC_URL``           — explicit operator override (recommended).
    2. ``RENDER_EXTERNAL_URL``  — auto-injected on Render web services.
    3. ``RAILWAY_PUBLIC_DOMAIN`` (bare host → prefixed with ``https://``).
    4. ``FLY_APP_NAME``         (bare name → ``https://<name>.fly.dev``).
    5. ``ORACLE_PUBLIC_HOST``   — bare host or full URL; bare is
       prefixed with ``https://``. Used on Oracle Cloud Free Tier VMs
       behind nginx + Let's Encrypt (see ``deploy/oracle/``).

    When ``include_keepalive`` is ``True``, ``KEEP_ALIVE_URL`` (set
    explicitly by ops) wins over everything else. That's only useful
    for the self-ping loop — addons that mint user-visible links
    (Fable5, Test, future Master Room) MUST use the default
    (``include_keepalive=False``) so a per-environment override of
    ``KEEP_ALIVE_URL`` (e.g. an internal LAN address) doesn't leak
    into Telegram replies.

    Returns ``""`` when none of the above is set — local dev. Callers
    typically fall back to a relative ``/path/...`` in that case so
    the user can manually stitch the host.
    """
    if include_keepalive:
        explicit = os.environ.get("KEEP_ALIVE_URL", "").strip().rstrip("/")
        if explicit:
            return explicit

    direct = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if direct:
        return direct

    render = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render:
        return render

    rw_dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if rw_dom:
        # RAILWAY_PUBLIC_DOMAIN is a bare host like ``foo-prod.up.railway.app``.
        return rw_dom if rw_dom.startswith(("http://", "https://")) else f"https://{rw_dom}"

    fly_app = os.environ.get("FLY_APP_NAME", "").strip()
    if fly_app:
        return f"https://{fly_app}.fly.dev"

    oracle = os.environ.get("ORACLE_PUBLIC_HOST", "").strip().rstrip("/")
    if oracle:
        return oracle if oracle.startswith(("http://", "https://")) else f"https://{oracle}"

    return ""


def _resolve_keepalive_url() -> str:
    """Legacy alias — keep-alive loop uses the keep-alive override."""
    return resolve_public_url(include_keepalive=True)


# Self-ping keeps Render Free (15-minute idle timer) awake. By default we
# pick a random delay between MIN and MAX seconds, biased toward MAX so
# the pattern doesn't look like a fixed cron interval. Set
# ``KEEP_ALIVE_INTERVAL=N`` to use a fixed N-second interval instead, or
# ``KEEP_ALIVE_INTERVAL=0`` to disable self-pinging entirely (e.g. on Fly
# always-on, Oracle Free Tier always-on, or your own VPS).
KEEP_ALIVE_URL = _resolve_keepalive_url()

_raw_interval = os.environ.get("KEEP_ALIVE_INTERVAL", "").strip()
if _raw_interval == "":
    KEEP_ALIVE_INTERVAL: int | None = None  # random mode
else:
    KEEP_ALIVE_INTERVAL = int(_raw_interval)  # 0 disables, >0 fixed

KEEP_ALIVE_MIN_SECONDS = int(os.environ.get("KEEP_ALIVE_MIN_SECONDS", "240"))  # 4 min
KEEP_ALIVE_MAX_SECONDS = int(os.environ.get("KEEP_ALIVE_MAX_SECONDS", "420"))  # 7 min
KEEP_ALIVE_BIAS = float(os.environ.get("KEEP_ALIVE_BIAS", "0.8"))

DEFAULT_MODEL = os.environ.get("MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
# Backward-compat alias for older imports.
MODEL = DEFAULT_MODEL
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "FALLBACK_MODELS",
        "openai/gpt-oss-120b:free,qwen/qwen3-coder:free,minimax/minimax-m2.5:free",
    ).split(",")
    if m.strip()
]

def _default_data_dir() -> str:
    """Pick a sensible DATA_DIR default.

    Prefer a persistent ``/data`` disk (the Render Blueprint mounts one
    there) so the Vault, state.json and uploaded files survive restarts
    even when the ``DATA_DIR`` env var was not set (e.g. a service that
    was created manually instead of via the Blueprint). Fall back to an
    ephemeral ``/tmp`` path only when ``/data`` is unusable (local dev).
    """
    candidate = Path("/data")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return "/data"
    except Exception:
        return "/tmp/bot-data"


DATA_DIR = Path(os.environ.get("DATA_DIR", _default_data_dir()))
PROJECTS_DIR = DATA_DIR / "projects"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# --- «Персональные данные и синхронизация» (ЧАСТЬ 18.1) ---------------
#
# Все переменные ОПЦИОНАЛЬНЫ: их отсутствие — штатный режим (локальный
# persistent disk, никакой внешней синхронизации). На этом этапе значения
# только читаются и валидируются по формату — НИКАКИХ подключений.
# Секреты (полный DATABASE_URL, S3 secret key, master key) никогда не
# попадают в state.json / UI / логи / журнал — только env.


def _validate_database_url(raw: str) -> str:
    """Вернуть DATABASE_URL, если он похож на валидный PostgreSQL DSN.

    Поддерживается любой совместимый PostgreSQL (Neon, Yandex Managed
    PostgreSQL, self-hosted) через один стандартный URL. НЕ подключаемся:
    только формат. Невалидное значение → "" (штатный режим) + warning
    без утечки самого значения (в нём пароль).
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(raw)
        if parsed.scheme in ("postgres", "postgresql") and parsed.hostname:
            return raw
    except ValueError:
        pass
    import logging

    logging.getLogger(__name__).warning(
        "DATABASE_URL is set but is not a valid postgres:// URL — "
        "ignoring it (value not logged)."
    )
    return ""


# PostgreSQL для синхронизации (18.3+). Пустая строка == не настроено.
DATABASE_URL = _validate_database_url(os.environ.get("DATABASE_URL", ""))

# S3-совместимое хранилище крупных файлов (Yandex Object Storage или иной
# provider). Используется начиная с 18.5; здесь только объявление.
SYNC_S3_ENDPOINT = os.environ.get("SYNC_S3_ENDPOINT", "").strip()
SYNC_S3_BUCKET = os.environ.get("SYNC_S3_BUCKET", "").strip()
SYNC_S3_ACCESS_KEY_ID = os.environ.get("SYNC_S3_ACCESS_KEY_ID", "").strip()
SYNC_S3_SECRET_ACCESS_KEY = os.environ.get("SYNC_S3_SECRET_ACCESS_KEY", "").strip()
SYNC_S3_REGION = os.environ.get("SYNC_S3_REGION", "").strip()

# Master key для envelope encryption (18.4). В env лежит САМ ключ
# (base64) в переменной SYNC_MASTER_KEY; в state.json допустима только
# СТРОКА-ИМЯ этой env-переменной (паттерн token_ref). НЕ создавать
# переменную с суффиксом _REF, содержащую сам ключ, и НЕ писать значение
# ключа в state/логи/журнал.
SYNC_MASTER_KEY = os.environ.get("SYNC_MASTER_KEY", "").strip()

# Lilush pipeline: SQLite-backed job queue + worker poll cadence.
JOBS_DB_PATH = DATA_DIR / "jobs.db"
WORKER_POLL_INTERVAL_S = float(os.environ.get("WORKER_POLL_INTERVAL_S", "1.0"))

# Downloader stage (yt-dlp).
DOWNLOADS_DIR = DATA_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_MAX_FILESIZE_MB = int(os.environ.get("DOWNLOAD_MAX_FILESIZE_MB", "5000"))
DOWNLOAD_MAX_HEIGHT = int(os.environ.get("DOWNLOAD_MAX_HEIGHT", "1080"))
# Optional: Netscape-format cookies file passed to yt-dlp for restricted
# content (age-gate / region-locked). Empty == no cookies.
#
# Resolution order:
#  1. Env var ``YT_COOKIES_FILE`` if set and non-empty (operator override).
#  2. ``data/yt_cookies.txt`` if it exists on disk — that's the path the
#     wizard «🍪 Загрузить cookies» button writes to.
#  3. No cookies at all (yt-dlp goes anonymous).
_YT_COOKIES_DEFAULT_PATH = DATA_DIR / "yt_cookies.txt"
_yt_cookies_env = os.environ.get("YT_COOKIES_FILE", "").strip()
if _yt_cookies_env:
    YT_COOKIES_FILE: str = _yt_cookies_env
elif _YT_COOKIES_DEFAULT_PATH.is_file():
    YT_COOKIES_FILE = str(_YT_COOKIES_DEFAULT_PATH)
else:
    YT_COOKIES_FILE = ""

# Analyzer stage (faster-whisper + pyscenedetect + LLM ranker).
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
ANALYZER_TARGET_CLIPS = int(os.environ.get("ANALYZER_TARGET_CLIPS", "3"))
ANALYZER_CLIP_MIN_S = float(os.environ.get("ANALYZER_CLIP_MIN_S", "15"))
ANALYZER_CLIP_MAX_S = float(os.environ.get("ANALYZER_CLIP_MAX_S", "60"))
# Threshold (0-100) for pyscenedetect's ContentDetector.
SCENEDETECT_THRESHOLD = float(os.environ.get("SCENEDETECT_THRESHOLD", "27"))

# Editor stage (ffmpeg crop / scale / overlay).
EDITOR_OUTPUT_WIDTH = int(os.environ.get("EDITOR_OUTPUT_WIDTH", "1080"))
EDITOR_OUTPUT_HEIGHT = int(os.environ.get("EDITOR_OUTPUT_HEIGHT", "1920"))
EDITOR_VIDEO_CRF = int(os.environ.get("EDITOR_VIDEO_CRF", "23"))
EDITOR_AUDIO_BITRATE = os.environ.get("EDITOR_AUDIO_BITRATE", "128k")
EDITOR_VIDEO_PRESET = os.environ.get("EDITOR_VIDEO_PRESET", "medium")
# Optional PNG (with alpha) burned into the bottom-right of every clip.
# Empty == no overlay.
OVERLAY_LOGO_PATH = os.environ.get("OVERLAY_LOGO_PATH", "").strip()
OVERLAY_MARGIN_PX = int(os.environ.get("OVERLAY_MARGIN_PX", "40"))

# SEO stage (pytrends + LLM-generated metadata).
SEO_LLM_MODEL = os.environ.get("SEO_LLM_MODEL", "openai/gpt-4o-mini")
SEO_MAX_TAGS = int(os.environ.get("SEO_MAX_TAGS", "15"))
SEO_TITLE_MAX_LEN = int(os.environ.get("SEO_TITLE_MAX_LEN", "90"))
SEO_DESCRIPTION_MAX_LEN = int(os.environ.get("SEO_DESCRIPTION_MAX_LEN", "500"))

# Publisher stage. DRY-RUN packages metadata + clip into a release dir;
# real uploaders are separate tentacles that consume that release.
RELEASES_DIR = DATA_DIR / "releases"
RELEASES_DIR.mkdir(parents=True, exist_ok=True)
# When True (default) we never call upload APIs — just stage the files.
PUBLISHER_DRY_RUN = os.environ.get("PUBLISHER_DRY_RUN", "true").lower() != "false"
# Default privacy status on YouTube; user flips to public manually.
PUBLISHER_YT_PRIVACY = os.environ.get("PUBLISHER_YT_PRIVACY", "private")
PUBLISHER_YT_CATEGORY_ID = os.environ.get("PUBLISHER_YT_CATEGORY_ID", "22")

# Default per-command shell timeout for /exec, /git and the LLM agent's
# exec_bash tool. Bumped from 30s to 600s so real-world installs like
# `pip install -e .` or `playwright install chromium` actually finish on
# slow free-tier hosts before being killed. Override via env if needed.
EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "600"))
# Per-command timeout used by the /work batch flow. Treated as "terminal
# mode" — no practical limit, so we cap at 1 hour to still rescue truly
# stuck processes (network hangs, etc.).
WORK_EXEC_TIMEOUT = int(os.environ.get("WORK_EXEC_TIMEOUT", "3600"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", "200000"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "20"))
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
# Cap on completion tokens per LLM call. OpenRouter free-tier accounts
# can usually afford only a few thousand tokens per request, and the
# default (model max, e.g. 65 536 for Opus) triggers a 402 "needs more
# credits" error. 4 096 is plenty for chat replies; raise via env if you
# pay for a bigger budget.
AGENT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "4096"))

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
HTTP_REFERER = os.environ.get("HTTP_REFERER", "https://github.com/")
APP_TITLE = os.environ.get("APP_TITLE", "Lilush")
# --- Editor Agent v2 (final_spec_FULL.md §13.1) --------------------
# These knobs are only consumed when ``EDITOR_VERSION`` is set to "v2",
# "v2.1", or "v6". Default ``v1`` keeps the legacy
# ``bot/workers/editor.py`` pipeline untouched. The full set is parsed
# into a frozen dataclass by ``bot.uniqueization.config.load_config``.

# Pipeline selector: "v1" (default, legacy worker) | "v2" | "v2.1" | "v6".
EDITOR_VERSION = os.environ.get("EDITOR_VERSION", "v1").strip().lower()
# Quality / cost profile for the v2+ pipeline: "light" | "medium" | "heavy".
EDITOR_PROFILE = os.environ.get("EDITOR_PROFILE", "light").strip().lower()
# Hard timeout (seconds) for each single fused ffmpeg invocation. The
# FfmpegRunner kills the subprocess and removes the partial temp file on
# timeout (§9.11).
EDITOR_FFMPEG_TIMEOUT_S = float(os.environ.get("EDITOR_FFMPEG_TIMEOUT_S", "360"))
# Optional override for ffmpeg ``-threads N``. Empty == let ffmpeg pick.
EDITOR_FFMPEG_THREADS = os.environ.get("EDITOR_FFMPEG_THREADS", "").strip()

# v2.0 stage toggles + tunables (§13.1).
EDITOR_ZOOM = os.environ.get("EDITOR_ZOOM", "").strip()
EDITOR_FACE_REFRAME_ENABLED = (
    os.environ.get("EDITOR_FACE_REFRAME_ENABLED", "true").strip().lower() == "true"
)
EDITOR_YUNET_MODEL_PATH = os.environ.get("EDITOR_YUNET_MODEL_PATH", "").strip()
EDITOR_MIRROR_ENABLED = (
    os.environ.get("EDITOR_MIRROR_ENABLED", "true").strip().lower() == "true"
)
EDITOR_MIRROR_DURATION_S = float(os.environ.get("EDITOR_MIRROR_DURATION_S", "1.5"))
EDITOR_REDRAW_ENABLED = (
    os.environ.get("EDITOR_REDRAW_ENABLED", "false").strip().lower() == "true"
)
EDITOR_COLORGRADE_ENABLED = (
    os.environ.get("EDITOR_COLORGRADE_ENABLED", "true").strip().lower() == "true"
)
EDITOR_LUT_PATH = os.environ.get("EDITOR_LUT_PATH", "").strip()
EDITOR_EFFECTS_DIR = os.environ.get("EDITOR_EFFECTS_DIR", "").strip()
EDITOR_EFFECTS_OPACITY = float(os.environ.get("EDITOR_EFFECTS_OPACITY", "0.15"))
EDITOR_MUSIC_REMOVER_ENABLED = (
    os.environ.get("EDITOR_MUSIC_REMOVER_ENABLED", "false").strip().lower() == "true"
)
# "mute" | "lowpass" | "model" — see §9.9.
EDITOR_MUSIC_REMOVER_MODE = os.environ.get(
    "EDITOR_MUSIC_REMOVER_MODE", "mute"
).strip().lower()
EDITOR_AUDIO_FX_ENABLED = (
    os.environ.get("EDITOR_AUDIO_FX_ENABLED", "true").strip().lower() == "true"
)
EDITOR_AUDIO_FX_WET = float(os.environ.get("EDITOR_AUDIO_FX_WET", "0.0") or "0.0")
EDITOR_LOUDNORM_ENABLED = (
    os.environ.get("EDITOR_LOUDNORM_ENABLED", "true").strip().lower() == "true"
)
EDITOR_PHASH_ENABLED = (
    os.environ.get("EDITOR_PHASH_ENABLED", "true").strip().lower() == "true"
)
EDITOR_THUMBNAIL_ENABLED = (
    os.environ.get("EDITOR_THUMBNAIL_ENABLED", "true").strip().lower() == "true"
)
EDITOR_KEEP_UNIQ_ARTIFACTS = (
    os.environ.get("EDITOR_KEEP_UNIQ_ARTIFACTS", "false").strip().lower() == "true"
)

# v2.1 layer toggles (§13.1, Part 10).
EDITOR_SUBTITLE_ENABLED = (
    os.environ.get("EDITOR_SUBTITLE_ENABLED", "true").strip().lower() == "true"
)
EDITOR_SUBTITLE_KARAOKE_ENABLED = (
    os.environ.get("EDITOR_SUBTITLE_KARAOKE_ENABLED", "true").strip().lower() == "true"
)
EDITOR_SUBTITLE_MAX_WORDS_PER_CUE = int(
    os.environ.get("EDITOR_SUBTITLE_MAX_WORDS_PER_CUE", "3")
)
EDITOR_BLUR_FILL_ENABLED = (
    os.environ.get("EDITOR_BLUR_FILL_ENABLED", "true").strip().lower() == "true"
)
EDITOR_HOOK_EMPHASIS_ENABLED = (
    os.environ.get("EDITOR_HOOK_EMPHASIS_ENABLED", "true").strip().lower() == "true"
)
EDITOR_UNIQUE_DISTANCE_ENABLED = (
    os.environ.get("EDITOR_UNIQUE_DISTANCE_ENABLED", "true").strip().lower() == "true"
)

# v6 creative-planner toggles (§13.1, Parts 1-8 of spec).
EDITOR_V6_ENABLED = (
    os.environ.get("EDITOR_V6_ENABLED", "false").strip().lower() == "true"
)
EDITOR_V6_FORCE_SKIP = (
    os.environ.get("EDITOR_V6_FORCE_SKIP", "false").strip().lower() == "true"
)
EDITOR_HOOK_DIALOGUE_DENSITY_THRESHOLD = float(
    os.environ.get("EDITOR_HOOK_DIALOGUE_DENSITY_THRESHOLD", "12.0")
)
EDITOR_CREATIVE_INTENSITY_CEILING_LIGHT = float(
    os.environ.get("EDITOR_CREATIVE_INTENSITY_CEILING_LIGHT", "0.4")
)
EDITOR_CREATIVE_INTENSITY_CEILING_MEDIUM = float(
    os.environ.get("EDITOR_CREATIVE_INTENSITY_CEILING_MEDIUM", "0.6")
)
EDITOR_CREATIVE_INTENSITY_CEILING_HEAVY = float(
    os.environ.get("EDITOR_CREATIVE_INTENSITY_CEILING_HEAVY", "0.8")
)
# Per-job uniqueness slider (UI "Уникальность"), 0..400.
#   0   = baseline byte-equivalence (Part 1-11 pipeline output).
#   100 = exactly one debate-vetted profile envelope
#         (light → heavy bookends from ``profiles.py``).
#   400 = 4× the debate envelope (red zone, user-approved hard cap).
# See ``bot.uniqueization.randomization`` for the parameter list
# (zoom / mirror_duration_s / effects_opacity / audio_fx_wet) and
# the per-job ``v = uniform(0.6 × slider, slider)`` formula.
EDITOR_UNIQUENESS_PCT = int(os.environ.get("EDITOR_UNIQUENESS_PCT", "0"))

