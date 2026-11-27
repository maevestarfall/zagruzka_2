# Bot entrypoint: dispatcher wiring, routers, and the freeze kill-switch guard.
# Co-authored with CoCo
import asyncio
import contextlib
import logging
import random

# Tag stdout the moment Python starts so a deploy log can never look
# blank. Render's free tier sometimes kills the container mid-import
# (OOM), and previously the user just saw "Cause of failure could not
# be determined" — no Python output at all. With this print we at
# least know we *got* to Python before something blew up.
print("[boot] bot.main entering", flush=True)

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .addons import build_addon_routers
from .capabilities import ensure_recorded as ensure_capabilities_recorded
from .config import (
    ALLOWED_USER_IDS,
    BOT_PERSONA,
    BOT_TOKEN,
    KEEP_ALIVE_BIAS,
    KEEP_ALIVE_INTERVAL,
    KEEP_ALIVE_MAX_SECONDS,
    KEEP_ALIVE_MIN_SECONDS,
    KEEP_ALIVE_URL,
    MODE,
    PORT,
    WEBHOOK_PATH,
    WEBHOOK_URL,
    WORKER_POLL_INTERVAL_S,
)
from .groq_handlers import groq_router
from .handlers import router
from .heartbeat import heartbeat_loop
from .jobs import get_default_queue
from .media_ui import media_router
from .persona import get_persona, known_keys
from .storage import storage
from .wizard import wizard_router
from .workers.base import Worker

print("[boot] all imports OK", flush=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _build_bot() -> Bot:
    return Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# Commands shown by Telegram when the user taps the menu button (≡)
# next to the input field. ``setMyCommands`` is set on startup so the
# popup matches whatever the user actually sees in the bot today.
# ``⚙️ Настройки`` lives at the bottom per user request.
_BOT_COMMANDS = [
    ("start", "🏠 Главное меню"),
    ("chat", "💬 Чат с LLM"),
    ("helpzavr", "🤖 Helpzavr"),
    ("pretty", "✨ Красивый текст"),
    ("mailbox", "📬 Почта"),
    ("media", "🎬 Генерация фото/видео"),
    ("github", "🐙 GitHub проекты"),
    ("work", "🖥 Терминал (пакет команд)"),
    ("settings", "⚙️ Настройки"),
]


async def _register_bot_commands(bot: Bot) -> None:
    """Publish the menu-button command list to Telegram.

    Best-effort: failures are logged but don't block startup — the bot
    still works without the menu (slash commands keep functioning;
    only the popup is missing). Defaults to clearing any previous
    list so renames / removals stick.
    """
    from aiogram.types import BotCommand, MenuButtonCommands

    try:
        commands = [BotCommand(command=name, description=desc) for name, desc in _BOT_COMMANDS]
        await bot.set_my_commands(commands)
        # Force the round (≡) menu button instead of the default web-app
        # button or no-button-at-all state. Without this the user has
        # to hold the "/" key to see commands — the round menu icon
        # next to the paperclip is what they actually want.
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("registered %d bot commands with Telegram", len(commands))
    except Exception:  # noqa: BLE001
        logger.exception("failed to register bot commands — menu button will be empty")


def _build_dispatcher() -> Dispatcher:
    # MemoryStorage is fine — wizard FSM only holds short-lived "awaiting input"
    # states; if the bot restarts mid-onboarding the user just taps the button
    # again. No need for a Redis/SQLite FSM backend.
    dp = Dispatcher(storage=MemoryStorage())

    # ── Freeze kill-switch: when the persistent freeze marker is set, the bot
    # is permanently inert — every update is dropped before any handler runs,
    # for everyone (no in-bot way to lift it, by design).
    from .selfdestruct import is_frozen as _is_frozen

    @dp.update.outer_middleware()
    async def _freeze_guard(handler, event, data):  # noqa: ANN001
        if _is_frozen():
            return None  # drop silently
        return await handler(event, data)

    # ── Private-mode payment gate: when «приват режим» is ON and paid access
    # is configured, guests without a positive balance are shown the welcome /
    # limit-reached offer with pay buttons and their update is dropped. Owners,
    # co-owners and payment updates always pass. Registered after the freeze
    # guard so a frozen bot still wins.
    try:
        from .addons.private_mode import build_payment_gate_middleware
        dp.update.outer_middleware(build_payment_gate_middleware())
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "private-mode payment gate disabled: %s", _e
        )

    # ── Private-mode per-feature USE gate: enforces the «Функции» toggles.
    # After the payment gate has decided a guest may interact, this drops any
    # callback/command that invokes a feature the owner has NOT enabled for
    # guests. No-op when «приват режим» is off; owners/co-owners always pass.
    try:
        from .feature_gate import build_feature_use_gate_middleware
        dp.update.outer_middleware(build_feature_use_gate_middleware())
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "private-mode feature gate disabled: %s", _e
        )

    if _is_frozen():
        logging.critical("lilush is FROZEN (kill-switch active) — all updates dropped")
    # Wizard router goes first so its /start, /setup and callback queries win
    # over the generic handlers in `router`. The media router sits
    # between them so its FSM-stated handlers (photo prompt capture,
    # slot-edit text input) match before the LLM chat catch-all.
    dp.include_router(wizard_router)
    # ТЗ-1 (Этап 1): роутер экранов «🌐 Платформы» (gateway:*). Идёт сразу
    # после wizard — обрабатывает только свой префикс, остальное пропускает.
    try:
        from .gateway.wizard_ui import gateway_router
        dp.include_router(gateway_router)
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning("gateway router disabled: %s", _e)
    # «🔌 Каналы-источники» capture router goes right after the wizard,
    # BEFORE the Groq catch-all, so posts in registered source chats are
    # harvested into the buffer instead of being answered as normal chat.
    try:
        from .addons.source_channels import build_source_capture_router
        dp.include_router(build_source_capture_router())
    except Exception as _e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "source_channels capture router disabled: %s", _e
        )
    # Groq overrides go right after the wizard so voice (no other
    # competitor) and photo-with-caption (would otherwise hit
    # photo_chooser first) get a chance to be transcribed / analysed
    # by Groq before the photo chooser asks the user what to do with
    # the image. Both handlers are state-less (`StateFilter(None)`) so
    # the wizard and addon FSMs are untouched.
    dp.include_router(groq_router)
    dp.include_router(media_router)
    # Addon routers (Helpzavr / Красивый текст / Проверка почты) sit
    # between the media and generic catch-all routers so their FSM-aware
    # message handlers run before the LLM chat fallback.
    for addon_router in build_addon_routers():
        dp.include_router(addon_router)
    dp.include_router(router)

    # Global error handler: when something the user asked for crashes, show a
    # friendly message with the two «🔧 Регенерация» / «🛠 перенастроить через
    # админку» buttons (Mason's request).
    @dp.errors()
    async def _on_error(event) -> bool:  # noqa: ANN001
        exc = getattr(event, "exception", None)
        logging.exception("unhandled update error: %s", exc)
        with contextlib.suppress(Exception):
            update = getattr(event, "update", None)
            target = None
            if update is not None:
                msg = getattr(update, "message", None)
                cq = getattr(update, "callback_query", None)
                if msg is not None:
                    target = msg
                elif cq is not None and getattr(cq, "message", None) is not None:
                    target = cq.message
            if target is not None:
                from .wizard import regen_error_keyboard, regen_record_error

                text = f"{type(exc).__name__}: {exc}" if exc else "Неизвестная ошибка"
                regen_record_error(text, where="unhandled")
                with contextlib.suppress(Exception):
                    await target.answer(
                        "⚠️ Что-то сломалось при выполнении.\n"
                        f"<code>{text[:500]}</code>",
                        reply_markup=regen_error_keyboard(),
                        parse_mode=ParseMode.HTML,
                    )
        return True

    return dp


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def _next_keepalive_delay() -> float:
    """Compute the next ping delay in seconds.

    Three modes:
      * ``KEEP_ALIVE_INTERVAL`` is a positive int → fixed interval.
      * ``KEEP_ALIVE_INTERVAL`` is ``None`` (default, env var unset) →
        random delay in ``[MIN, MAX]`` with ``BIAS`` probability of
        landing in the upper half (closer to ``MAX``). This makes the
        ping pattern look human-ish rather than a fixed cron tick.
      * ``KEEP_ALIVE_INTERVAL == 0`` → disabled (caller short-circuits).
    """
    if KEEP_ALIVE_INTERVAL is not None and KEEP_ALIVE_INTERVAL > 0:
        return float(KEEP_ALIVE_INTERVAL)
    midpoint = (KEEP_ALIVE_MIN_SECONDS + KEEP_ALIVE_MAX_SECONDS) / 2
    if random.random() < KEEP_ALIVE_BIAS:
        return random.uniform(midpoint, KEEP_ALIVE_MAX_SECONDS)
    return random.uniform(KEEP_ALIVE_MIN_SECONDS, midpoint)


async def _keep_alive_loop() -> None:
    """Periodically GET our own ``/healthz`` to keep Render Free awake.

    Render counts only *incoming* HTTP traffic toward the 15-minute idle
    timer — the bot's outgoing Telegram polling doesn't qualify. Hitting
    our own public URL through the platform's load balancer DOES count.

    Disabled when no URL was resolved (local dev, VPS) or when
    ``KEEP_ALIVE_INTERVAL`` is explicitly set to ``0``.
    """
    if not KEEP_ALIVE_URL or KEEP_ALIVE_INTERVAL == 0:
        return
    target = f"{KEEP_ALIVE_URL}/healthz"
    if KEEP_ALIVE_INTERVAL is not None and KEEP_ALIVE_INTERVAL > 0:
        logger.info(
            "keep-alive: pinging %s every %ss (fixed)", target, KEEP_ALIVE_INTERVAL
        )
    else:
        logger.info(
            "keep-alive: pinging %s every %d-%ds (%.0f%% bias toward upper end)",
            target,
            KEEP_ALIVE_MIN_SECONDS,
            KEEP_ALIVE_MAX_SECONDS,
            KEEP_ALIVE_BIAS * 100,
        )
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # First ping after a short grace period so the health server has
        # time to bind and the platform DNS to resolve our URL.
        await asyncio.sleep(min(_next_keepalive_delay(), 30))
        while True:
            try:
                async with session.get(target) as resp:
                    if resp.status >= 400:
                        logger.warning(
                            "keep-alive ping returned HTTP %s", resp.status
                        )
            except Exception as exc:  # noqa: BLE001 — log everything, retry forever
                logger.warning("keep-alive ping failed: %s", exc)
            await asyncio.sleep(_next_keepalive_delay())


def _build_workers() -> list[Worker]:
    """Instantiate one worker per pipeline stage, all bound to the default queue.

    Concrete worker types are kept as a flat list so each gets exactly one
    polling task. If we later want N concurrent editors (the CPU-heavy
    stage), we just add ``EditorWorker(queue)`` multiple times.
    """
    from .workers import (
        AnalyzerWorker,
        DownloaderWorker,
        PublisherWorker,
        SeoWorker,
        build_editor_worker,
    )
    queue = get_default_queue()
    poll = WORKER_POLL_INTERVAL_S
    return [
        DownloaderWorker(queue, poll_interval_s=poll),
        AnalyzerWorker(queue, poll_interval_s=poll),
        # ``build_editor_worker`` picks v1 (default) or v2/v2.1/v6 based on
        # the ``EDITOR_VERSION`` env var (or the Telegram «🎞 Монтажёр»
        # screen at runtime). Default keeps v1 behaviour exactly as before.
        build_editor_worker(queue, poll_interval_s=poll),
        SeoWorker(queue, poll_interval_s=poll),
        PublisherWorker(queue, poll_interval_s=poll),
    ]


async def _run_polling() -> None:
    """Long-polling mode + a tiny aiohttp server for cloud health checks.

    Render free, Railway, Fly etc. usually require a process to bind a port
    so they know the deploy is healthy. A 200-OK on ``/`` and ``/healthz`` is
    enough; we keep the same paths the webhook mode exposes.

    The health server is bound FIRST (before any Telegram I/O or worker
    boot) so the platform marks the deploy as live as soon as possible.
    Earlier we set up the bot before binding the port — if any of the
    network calls (``delete_webhook``, ``set_my_commands``) hung or
    OOM-killed the process, Render saw no logs and aborted the deploy
    with the unhelpful "Cause of failure could not be determined". Now
    even a later crash leaves application logs behind.
    """
    health_app = web.Application()
    health_app.router.add_get("/", _health)
    health_app.router.add_get("/healthz", _health)
    # Fable5 live-access endpoints (token-gated remote control surface
    # used by Viktor to verify builds on the running bot). Routes have
    # to be registered before AppRunner.setup() — the dispatcher
    # freezes on start. The bot reference is set later via
    # ``health_app['fable5_bot'] = bot`` once it's built.
    try:
        from .addons.fable5.server import register_http_endpoints as _fable5_register
        _fable5_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Fable5 endpoints (polling mode)")
    # «🧪 Тест» web preview surface — same mount pattern as Fable5.
    try:
        from .addons.test.server import register_http_endpoints as _test_register
        _test_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Test endpoints (polling mode)")
    # Master Room namespace placeholder — no routes yet, but reserve
    # state.json slot + call the (no-op) registrar so the import path
    # is exercised in production. Once the real surface lands the
    # registrar will mount /masterroom/<token>/... without any wiring
    # changes here.
    try:
        from .addons.masterroom.server import (
            register_http_endpoints as _mr_register,
            reserve_defaults as _mr_reserve,
        )
        _mr_register(health_app)
        _mr_reserve()
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Master Room endpoints (polling mode)")
    # «👁 VISION» web dashboard — token-gated bot management panel.
    try:
        from .addons.vision.server import register_http_endpoints as _vision_register
        _vision_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Vision endpoints (polling mode)")
    # «🗺 Карта проекта» (graphify) — token-gated interactive graph.
    try:
        from .addons.projectmap.server import register_http_endpoints as _pmap_register
        _pmap_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Project-map endpoints (polling mode)")
    # «🗺 План карта» — token-gated flowchart editor (Fable5 link principle).
    try:
        from .addons.plan_map.server import register_http_endpoints as _planmap_register
        _planmap_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Plan-map endpoints (polling mode)")
    # «🧭 Гид» — token-gated step-by-step web wizard.
    try:
        from .addons.guide.server import register_http_endpoints as _guide_register
        _guide_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Guide endpoints (polling mode)")
    # «🎓 Обучалка» — token-gated knowledge-vessel for teachers/revisor.
    try:
        from .addons.obuchalka.server import register_http_endpoints as _obuch_register
        _obuch_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Obuchalka endpoints (polling mode)")
    # «🎯 Дебаты v3» embedded debate engine HTTP surface.
    try:
        from .addons.debates.debate_server import register_http_endpoints as _debate_register
        _debate_register(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Debate-v3 endpoints (polling mode)")
    # 🪙 Private-mode CryptoBot pay webhook (auto-credit). No-ops unless the
    # owner turned it ON + set a token. Bot ref is read from app at request time.
    try:
        from .addons.private_mode.crypto_webhook import register_crypto_webhook
        register_crypto_webhook(health_app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register crypto webhook (polling mode)")
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("health server listening on 0.0.0.0:%s", PORT)

    bot = _build_bot()
    health_app["fable5_bot"] = bot
    dp = _build_dispatcher()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:  # noqa: BLE001 — log and keep going
        logger.exception("delete_webhook failed; continuing")
    try:
        await _register_bot_commands(bot)
    except Exception:  # noqa: BLE001
        logger.exception("set_my_commands failed; continuing")
    try:
        ensure_capabilities_recorded()
    except Exception:  # noqa: BLE001
        logger.exception("ensure_capabilities_recorded failed; continuing")

    queue = get_default_queue()
    await queue.init()
    workers = _build_workers()
    worker_tasks = [asyncio.create_task(w.run(), name=f"worker:{w.kind}") for w in workers]

    keepalive_task = asyncio.create_task(_keep_alive_loop())
    heartbeat_task = asyncio.create_task(heartbeat_loop(), name="heartbeat")

    # ЧАСТЬ 18.6: инкрементальная синхронизация Master Room. Без
    # DATABASE_URL — полный no-op (hook не ставится, задача не создаётся);
    # ошибка старта sync никогда не блокирует запуск бота.
    try:
        from .addons.masterroom.sync import incremental as _pdsync_inc

        _pdsync_inc.start_incremental_sync()
    except Exception:  # noqa: BLE001
        logger.exception("failed to start pdsync incremental sync")

    # Context paging — fire due «продолжить кусок» continuation timers.
    async def _ctx_timer_loop() -> None:
        from .addons.context.handlers import process_due_timers
        while True:
            with contextlib.suppress(Exception):
                await process_due_timers(bot)
            await asyncio.sleep(30)
    ctx_timer_task = asyncio.create_task(_ctx_timer_loop(), name="ctx-timers")

    # «➕ Улучшить проект из Важного» — плановый запуск по таймингу
    # (каждый день/неделю), настроенному в управлении Гермесом.
    async def _improveproj_sched_loop() -> None:
        from .addons.improveproj import store as _impj_store, run_scheduled
        while True:
            await asyncio.sleep(120)
            with contextlib.suppress(Exception):
                if _impj_store.due():
                    owner = storage.get_owner_id()
                    if owner:
                        await run_scheduled(bot, int(owner))
    improveproj_task = asyncio.create_task(_improveproj_sched_loop(), name="improveproj-sched")

    # «🔌 Каналы-источники» scheduler — runs buffer checks at the daily
    # times the owner configured, when auto-mode is ON.
    try:
        from .addons.source_channels.scheduler import (
            scheduler_loop as _srcch_scheduler_loop,
        )

        srcch_task = asyncio.create_task(
            _srcch_scheduler_loop(bot), name="source-channels-scheduler"
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to start source_channels scheduler")

    # «📚 Поступенчатая инструкция» — Hermes background refinement loop.
    # Idles cheaply when «Гермес дорабатывает» is OFF.
    try:
        from .addons.playbook import hermes_loop as _pb_hermes_loop
        from .storage import storage as _storage

        def _resolve_owner_chat():
            with contextlib.suppress(Exception):
                return _storage.get_owner_id()
            return None

        pb_hermes_task = asyncio.create_task(
            _pb_hermes_loop(bot, _resolve_owner_chat), name="playbook-hermes"
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to start playbook Hermes loop")

    # Mailbox addon background poller — pushes new emails to the chat
    # configured in /Проверка почты → Уведомления when any toggle is on.
    addon_tasks: list[asyncio.Task[None]] = []
    try:
        from .addons.mailbox.handlers import poller_loop as _mailbox_poller_loop

        def _resolve_chat_id():
            from .addons import state as _addon_state
            return _addon_state.get("mailbox", "notify_chat_id", None)

        addon_tasks.append(
            asyncio.create_task(
                _mailbox_poller_loop(bot, get_chat_id=_resolve_chat_id),
                name="mailbox-poller",
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to start mailbox poller")

    # Регенерация addon — background self-analysis loop. Cheap when the
    # ``addons.regen.bg_enabled`` toggle is OFF; when ON it scans the bot's
    # own code on a cadence and DMs the owner about new issues (sim/real).
    try:
        from .addons.regen.background import regen_bg_loop as _regen_bg_loop

        def _resolve_regen_chat_id():
            from .addons import state as _addon_state

            return _addon_state.get("regen", "notify_chat_id", None)

        addon_tasks.append(
            asyncio.create_task(
                _regen_bg_loop(bot, get_chat_id=_resolve_regen_chat_id),
                name="regen-bg",
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to start regen background loop")

    # Algorithm addon scheduler — runs saved algorithms whose
    # ``interval_minutes > 0`` on their cadence. Lives next to the
    # mailbox poller so it shares the same task-lifecycle handling.
    try:
        from .addons.algorithm.scheduler import scheduler_loop as _algo_scheduler

        addon_tasks.append(
            asyncio.create_task(
                _algo_scheduler(bot), name="algorithm-scheduler",
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to start algorithm scheduler")

    # ТЗ-1 §4.7: кэшируем bot_username при старте, чтобы deep-link в
    # 409 tg_only-ответах Fable5 работал ещё до первого TG-callback.
    try:
        from .addons import state as _addon_state

        _me = await bot.get_me()
        if _me.username:
            _addon_state.set_("gateway", "bot_username", _me.username)
    except Exception:  # noqa: BLE001
        logger.exception("failed to cache bot_username for gateway deeplinks")

    # Этапы 2–3 (части A/B): автостарт адаптеров Discord/VK, если владелец
    # уже сохранил токены. Отсутствие библиотек/токена не роняет бот —
    # адаптеры сами выставляют статус error/off и молча выходят.
    try:
        from .gateway.adapters import discord_gw as _discord_gw

        if _discord_gw.get_token():
            await _discord_gw.start()
    except Exception:  # noqa: BLE001
        logger.exception("discord adapter autostart failed")
    try:
        from .gateway.adapters import vk_gw as _vk_gw

        if _vk_gw.get_token():
            await _vk_gw.start()
    except Exception:  # noqa: BLE001
        logger.exception("vk adapter autostart failed")

    logger.info("starting polling with %d workers", len(workers))
    try:
        await dp.start_polling(bot)
    except Exception:  # noqa: BLE001
        # Polling crashed (most commonly: TelegramUnauthorizedError when
        # the BOT_TOKEN env var is wrong). Log the full trace and keep
        # the health server alive so Render keeps the deploy "live" and
        # the user can fix BOT_TOKEN in env vars without losing the
        # entire service. Without this the container exits, Render
        # restarts it in a loop, and runtime logs scroll past faster
        # than the user can copy them.
        logger.exception(
            "dp.start_polling crashed — check BOT_TOKEN in Render env "
            "vars. Health server stays up so you can patch the token "
            "and redeploy."
        )
        # Block forever so the container doesn't exit. Render will keep
        # /healthz green; the user reads the trace above and fixes the
        # token.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
    finally:
        # ТЗ-1 §5 (R9): graceful shutdown адаптеров Discord/VK при
        # остановке процесса. Токены остаются сохранёнными — при
        # следующем старте адаптеры поднимутся автоматически.
        for _mod_name in ("discord_gw", "vk_gw"):
            try:
                import importlib

                _mod = importlib.import_module(
                    f"bot.gateway.adapters.{_mod_name}"
                )
                await _mod.stop()
            except Exception:  # noqa: BLE001
                logger.exception("%s graceful shutdown failed", _mod_name)
        for w in workers:
            w.stop()
        keepalive_task.cancel()
        heartbeat_task.cancel()
        ctx_timer_task.cancel()
        for task in addon_tasks:
            task.cancel()
        for task in (*worker_tasks, keepalive_task, heartbeat_task, ctx_timer_task, *addon_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await runner.cleanup()
        await bot.session.close()


def _run_webhook() -> None:
    if not WEBHOOK_URL:
        raise RuntimeError(
            "PUBLIC_URL is not set; cannot run in webhook mode. "
            "Set BOT_MODE=polling or provide PUBLIC_URL."
        )

    bot = _build_bot()
    dp = _build_dispatcher()
    workers: list[Worker] = []
    worker_tasks: list[asyncio.Task[None]] = []
    bg_tasks: list[asyncio.Task[None]] = []

    async def _on_startup(app: web.Application) -> None:
        logger.info("setting webhook to %s", WEBHOOK_URL)
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        await _register_bot_commands(bot)
        ensure_capabilities_recorded()
        queue = get_default_queue()
        await queue.init()
        workers.extend(_build_workers())
        for w in workers:
            worker_tasks.append(asyncio.create_task(w.run(), name=f"worker:{w.kind}"))
        bg_tasks.append(asyncio.create_task(heartbeat_loop(), name="heartbeat"))
        # Render Free counts only INCOMING HTTP toward its 15-min idle
        # timer. Webhook traffic from Telegram qualifies, but a chatty
        # bot can still go quiet for long stretches. Run the same
        # self-ping loop the polling branch uses so Render doesn't spin
        # the service down between conversations.
        bg_tasks.append(asyncio.create_task(_keep_alive_loop(), name="keepalive"))
        # ЧАСТЬ 18.6: инкрементальная синхронизация Master Room и в
        # webhook-режиме. Без DATABASE_URL — полный no-op; ошибка старта
        # sync не блокирует запуск бота.
        try:
            from .addons.masterroom.sync import incremental as _pdsync_inc

            _pdsync_inc.start_incremental_sync()
        except Exception:  # noqa: BLE001
            logger.exception("failed to start pdsync incremental sync")
        # Algorithm scheduler also runs in webhook mode so periodic
        # algorithms fire regardless of which transport the deploy uses.
        try:
            from .addons.algorithm.scheduler import (
                scheduler_loop as _algo_scheduler,
            )

            bg_tasks.append(
                asyncio.create_task(
                    _algo_scheduler(bot), name="algorithm-scheduler",
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to start algorithm scheduler")
        # ТЗ-1 R9: автостарт адаптеров Discord/VK и в webhook-режиме —
        # тот же паттерн, что в polling-ветке (_run_polling).
        try:
            from .addons import state as _addon_state

            _me = await bot.get_me()
            if _me.username:
                _addon_state.set_("gateway", "bot_username", _me.username)
        except Exception:  # noqa: BLE001
            logger.exception("failed to cache bot_username for gateway deeplinks")
        try:
            from .gateway.adapters import discord_gw as _discord_gw

            if _discord_gw.get_token():
                await _discord_gw.start()
        except Exception:  # noqa: BLE001
            logger.exception("discord adapter autostart failed")
        try:
            from .gateway.adapters import vk_gw as _vk_gw

            if _vk_gw.get_token():
                await _vk_gw.start()
        except Exception:  # noqa: BLE001
            logger.exception("vk adapter autostart failed")
        logger.info(
            "started %d workers + heartbeat + keep-alive + algorithm",
            len(workers),
        )

    async def _on_cleanup(app: web.Application) -> None:
        logger.info("removing webhook")
        # ТЗ-1 §5 (R9): graceful shutdown адаптеров и в webhook-режиме.
        for _mod_name in ("discord_gw", "vk_gw"):
            try:
                import importlib

                _mod = importlib.import_module(
                    f"bot.gateway.adapters.{_mod_name}"
                )
                await _mod.stop()
            except Exception:  # noqa: BLE001
                logger.exception("%s graceful shutdown failed", _mod_name)
        for w in workers:
            w.stop()
        for task in bg_tasks:
            task.cancel()
        for task in (*worker_tasks, *bg_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await bot.delete_webhook()
        finally:
            await bot.session.close()

    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/healthz", _health)
    # Fable5 live-access endpoints — same as polling mode.
    try:
        from .addons.fable5.server import register_http_endpoints as _fable5_register
        _fable5_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Fable5 endpoints (webhook mode)")
    # «🧪 Тест» web preview surface — same mount pattern as Fable5.
    try:
        from .addons.test.server import register_http_endpoints as _test_register
        _test_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Test endpoints (webhook mode)")
    # Master Room namespace placeholder (see polling mode for context).
    try:
        from .addons.masterroom.server import (
            register_http_endpoints as _mr_register,
            reserve_defaults as _mr_reserve,
        )
        _mr_register(app)
        _mr_reserve()
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Master Room endpoints (webhook mode)")
    # «👁 VISION» web dashboard — same as polling mode.
    try:
        from .addons.vision.server import register_http_endpoints as _vision_register
        _vision_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Vision endpoints (webhook mode)")
    # «🗺 Карта проекта» (graphify) — same as polling mode.
    try:
        from .addons.projectmap.server import register_http_endpoints as _pmap_register
        _pmap_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Project-map endpoints (webhook mode)")
    # «🗺 План карта» — same as polling mode.
    try:
        from .addons.plan_map.server import register_http_endpoints as _planmap_register
        _planmap_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Plan-map endpoints (webhook mode)")
    # «🧭 Гид» — same as polling mode.
    try:
        from .addons.guide.server import register_http_endpoints as _guide_register
        _guide_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Guide endpoints (webhook mode)")
    # «🎓 Обучалка» — same as polling mode.
    try:
        from .addons.obuchalka.server import register_http_endpoints as _obuch_register
        _obuch_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Obuchalka endpoints (webhook mode)")
    # «🎯 Дебаты v3» embedded debate engine HTTP surface.
    try:
        from .addons.debates.debate_server import register_http_endpoints as _debate_register
        _debate_register(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register Debate-v3 endpoints (webhook mode)")
    # 🪙 Private-mode CryptoBot pay webhook (auto-credit). No-ops unless ON + token.
    try:
        from .addons.private_mode.crypto_webhook import register_crypto_webhook
        register_crypto_webhook(app)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register crypto webhook (webhook mode)")
    app["fable5_bot"] = bot
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    logger.info("starting webhook server on 0.0.0.0:%s, path %s", PORT, WEBHOOK_PATH)
    web.run_app(app, host="0.0.0.0", port=PORT)


async def _run_dormant() -> None:
    """Health-only server for services whose BOT_TOKEN hasn't been set yet.

    Render Free requires the process to bind a port (health check) within
    a few minutes or marks the deploy failed. So even without a token we
    spin up an aiohttp server on ``/`` and ``/healthz``, log a warning,
    and idle forever. The user just edits ``BOT_TOKEN`` in the Render env
    vars and the service auto-restarts into normal polling mode.

    Self-ping intentionally stays disabled in dormant mode — there's no
    bot to keep alive, the service may safely sleep.
    """
    logger.warning(
        "BOT_TOKEN is empty — entering DORMANT mode. Health server only, "
        "no Telegram polling. Set BOT_TOKEN in Render env vars to "
        "activate this bot."
    )
    health_app = web.Application()
    health_app.router.add_get("/", _health)
    health_app.router.add_get("/healthz", _health)
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("dormant health server listening on 0.0.0.0:%s", PORT)
    try:
        # Block forever. The process exits on signal (Render redeploy).
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def _startup_selfcheck() -> None:
    """🛡 Runtime analog of the Docker build guard, toggled in ⚙️ Настройки.

    When ON (default), verify the critical modules are importable at boot and
    log LOUDLY if any is missing — so a broken/empty deploy screams in the logs
    instead of dying silently with «No module named bot.main». Off = skip.
    """
    try:
        from .addons import state as addon_state
        if not bool(addon_state.get("system", "startup_selfcheck", True)):
            return
    except Exception:  # noqa: BLE001 — never let the check block boot
        return
    import importlib.util
    critical = ["bot.main", "bot.wizard", "bot.agent", "bot.handlers", "bot.addons"]
    missing = []
    for mod in critical:
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)
        except Exception:  # noqa: BLE001
            missing.append(mod)
    if missing:
        msg = ("[selfcheck] FATAL: критичные модули отсутствуют в образе: "
               + ", ".join(missing) + " — проверь COPY bot/ и передеплой с очисткой кэша.")
        print(msg, flush=True)
        logger.error(msg)
    else:
        logger.info("[selfcheck] OK: критичные модули на месте")


def main() -> None:
    import sys
    if "--coder-run" in sys.argv:
        idx = sys.argv.index("--coder-run")
        if idx + 1 < len(sys.argv):
            task_desc = sys.argv[idx + 1]
        else:
            print("Error: --coder-run requires a task description argument.")
            sys.exit(1)
            
        print(f"[CLI Coder] Starting coder run for task: {task_desc}", flush=True)
        from .coder.persistence import DB_PATH, init_db
        init_db(DB_PATH)
        
        import random, time, sqlite3
        run_id = f"run_cli_{random.randint(100, 999)}"
        
        from .addons.coder.handlers import get_workers, get_budget, run_fsm_loop
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute(
                "INSERT INTO coder_runs (run_id, task_desc, state, workers_count, budget_cap, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task_desc, "INIT", get_workers(), get_budget(), time.strftime("%Y-%m-%d %H:%M:%S"))
            )
        conn.close()
        
        print(f"[CLI Coder] Project {run_id} initialized. Starting FSM loop...", flush=True)
        asyncio.run(run_fsm_loop(run_id, task_desc, 0, None))
        print(f"[CLI Coder] Project {run_id} finished FSM execution.", flush=True)
        return

    if BOT_PERSONA not in known_keys():
        logger.warning(
            "BOT_PERSONA=%r is not in the known roster %s — falling back to 'boss'. "
            "Set BOT_PERSONA correctly in Render env vars for this service.",
            BOT_PERSONA,
            known_keys(),
        )
    _startup_selfcheck()
    persona = get_persona()
    logger.info(
        "boot: persona=%s (%s, %s/%s)",
        persona.key,
        persona.display_name,
        persona.department,
        persona.rank,
    )

    if not BOT_TOKEN:
        # Service deployed without a token yet — run a stub health server
        # so Render keeps the deploy alive, and let the user add a token
        # in the dashboard later.
        asyncio.run(_run_dormant())
        return

    owner = storage.get_owner_id()
    if owner is None and not ALLOWED_USER_IDS:
        logger.info(
            "No owner claimed yet and ALLOWED_USER_IDS is empty. "
            "First user to send /start in Telegram becomes the owner."
        )
    elif owner is not None:
        logger.info("Owner already claimed: telegram id %s", owner)

    # Webhook mode requires PUBLIC_URL. If the user picked webhook but didn't
    # set the URL (typical right after a one-click cloud deploy), silently
    # fall back to polling so the bot still comes up.
    if MODE == "webhook" and not WEBHOOK_URL:
        logger.warning(
            "BOT_MODE=webhook but PUBLIC_URL is empty — falling back to polling. "
            "Set PUBLIC_URL=https://<your-host> to switch back."
        )
        asyncio.run(_run_polling())
    elif MODE == "polling":
        asyncio.run(_run_polling())
    else:
        _run_webhook()


if __name__ == "__main__":
    main()
