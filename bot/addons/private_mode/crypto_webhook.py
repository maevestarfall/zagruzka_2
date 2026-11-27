"""CryptoBot (Crypto Pay API) webhook — instant auto-credit for paid invoices.

This is the optional alternative to the «✅ Я оплатил — проверить» button: when
the owner turns the webhook ON (Оплата → Вебхук CryptoBot) and pastes the shown
URL into their @CryptoBot app settings, every paid invoice is credited the
moment Crypto Pay notifies us — no button press needed.

Built on the official ``aiocryptopay`` webhook mechanism (``@crypto.pay_handler``
+ ``crypto.get_updates`` aiohttp route), so signature verification and update
parsing are handled by the library, not hand-rolled.

Mounted from :mod:`bot.main` (both polling and webhook modes expose an aiohttp
server, so the same secret route works in either mode).
"""

from __future__ import annotations

import contextlib
import logging

from ...storage import storage

logger = logging.getLogger(__name__)

# Secret-ish, stable path. The owner pastes ``<public_url><PATH>`` into the
# @CryptoBot app's webhook settings.
CRYPTO_WEBHOOK_PATH = "/private-mode/crypto-webhook"


def webhook_url(public_base: str) -> str:
    """Full URL to paste into the CryptoBot app settings."""
    base = (public_base or "").rstrip("/")
    return f"{base}{CRYPTO_WEBHOOK_PATH}" if base else CRYPTO_WEBHOOK_PATH


def register_crypto_webhook(app) -> bool:
    """Mount the CryptoBot pay webhook onto ``app`` when enabled + token set.

    Returns ``True`` when the route was mounted. Safe to call unconditionally
    from both polling and webhook server setup — it no-ops when the feature is
    off, the token is missing, or aiocryptopay isn't installed. The aiogram
    ``Bot`` is read from ``app["fable5_bot"]`` at request time (set by main.py
    after the bot is built), so this can run before the bot exists.
    """
    try:
        if not storage.pm_get_crypto_webhook_enabled():
            return False
        token = storage.pm_get_crypto_token()
        if not token:
            logger.info("crypto webhook enabled but no token set — skipping mount")
            return False
    except Exception:  # noqa: BLE001 — storage not ready
        return False

    try:
        from aiocryptopay import AioCryptoPay, Networks
    except Exception:  # noqa: BLE001
        logger.warning("aiocryptopay not installed — crypto webhook not mounted")
        return False

    crypto = AioCryptoPay(token=token, network=Networks.MAIN_NET)

    @crypto.pay_handler()
    async def _on_invoice_paid(update, app) -> None:  # noqa: ANN001
        try:
            invoice = getattr(update, "payload", None)
            if invoice is None:
                return
            invoice_id = getattr(invoice, "invoice_id", None)
            guest_payload = getattr(invoice, "payload", "") or ""
            uid = int(guest_payload) if str(guest_payload).lstrip("-").isdigit() else None
            if invoice_id is None or uid is None:
                logger.warning("crypto webhook: missing invoice_id/uid (payload=%r)", guest_payload)
                return
            # Anti-double: the «check» button and the webhook share the same ledger.
            if storage.pm_crypto_was_credited(invoice_id):
                return
            usd = storage.pm_get_usd_per_slot()
            storage.pm_crypto_mark_credited(invoice_id, uid)
            new_bal = storage.pm_add_balance(uid, usd)
            bot = app.get("fable5_bot") if hasattr(app, "get") else None
            if bot is not None:
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        uid,
                        f"✅ Оплата криптой получена. Начислено ${usd:g}. "
                        f"Баланс: ${new_bal:g}.\nДоступ открыт — пользуйся!",
                    )
        except Exception:  # noqa: BLE001 — never let a webhook error 500 the route
            logger.exception("crypto webhook handler error")

    app.router.add_post(CRYPTO_WEBHOOK_PATH, crypto.get_updates)
    # keep the client alive for the app's lifetime + close it on shutdown
    app["_private_mode_crypto_client"] = crypto

    async def _close_crypto(_app) -> None:
        with contextlib.suppress(Exception):
            await crypto.close()

    app.on_cleanup.append(_close_crypto)
    logger.info("crypto pay webhook mounted at %s", CRYPTO_WEBHOOK_PATH)
    return True
