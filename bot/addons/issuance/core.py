"""Core card verification, high-performance async simulation, and Dolphin Anty profile logic."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
from typing import Any

import aiohttp

try:
    from curl_cffi.requests import AsyncSession
    USE_CURL_CFFI = True
except ImportError:
    USE_CURL_CFFI = False

logger = logging.getLogger(__name__)

ADDON = "issuance"


def validate_luhn(card_number: str) -> bool:
    """Validate a card number using the Luhn Algorithm (checksum)."""
    digits = [int(d) for d in card_number if d.isdigit()]
    if not digits:
        return False
    
    # Double every second digit from the right
    checksum = 0
    reverse_digits = digits[::-1]
    for i, digit in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = digit * 2
            if doubled > 9:
                doubled -= 9
            checksum += doubled
        else:
            checksum += digit
            
    return checksum % 10 == 0


def get_local_bin_fallback(bin_digits: str) -> dict[str, Any]:
    """Provide a highly realistic mock BIN response based on the card prefix."""
    first_digit = bin_digits[0] if bin_digits else ""
    
    scheme = "unknown"
    brand = "Standard"
    card_type = "credit"
    
    if first_digit == "4":
        scheme = "visa"
        brand = "Visa Classic"
    elif first_digit == "5":
        scheme = "mastercard"
        brand = "Mastercard Standard"
    elif first_digit == "3":
        scheme = "amex"
        brand = "American Express Gold"
        card_type = "charge"
    elif first_digit == "6":
        scheme = "discover"
        brand = "Discover"
    elif first_digit == "2":
        scheme = "mir"
        brand = "MIR Classic"
        
    countries = [
        {"name": "Russian Federation", "emoji": "🇷🇺", "alpha2": "RU", "currency": "RUB"},
        {"name": "United States", "emoji": "🇺🇸", "alpha2": "US", "currency": "USD"},
        {"name": "United Kingdom", "emoji": "🇬🇧", "alpha2": "GB", "currency": "GBP"},
        {"name": "Germany", "emoji": "🇩🇪", "alpha2": "DE", "currency": "EUR"},
        {"name": "Kazakhstan", "emoji": "🇰🇿", "alpha2": "KZ", "currency": "KZT"}
    ]
    
    # Seed randomness with the BIN to keep responses consistent for the same card
    try:
        seed = int(bin_digits[:6])
    except ValueError:
        seed = 123456
        
    rng = random.Random(seed)
    country = rng.choice(countries)
    
    banks = {
        "RU": ["Sberbank", "T-Bank (Tinkoff)", "VTB", "Alfa-Bank", "Raiffeisenbank"],
        "US": ["Chase", "Bank of America", "Citibank", "Wells Fargo", "Capital One"],
        "GB": ["Barclays", "HSBC", "Lloyds Bank", "NatWest"],
        "DE": ["Deutsche Bank", "Commerzbank", "Sparkasse"],
        "KZ": ["Kaspi Bank", "Halyk Bank", "ForteBank"]
    }
    
    bank_name = rng.choice(banks.get(country["alpha2"], ["Global Bank"]))
    
    return {
        "scheme": scheme,
        "type": card_type,
        "brand": brand,
        "country": country,
        "bank": {"name": bank_name}
    }


async def check_bin(session: Any, card_number: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    """Perform an asynchronous, semaphore-bounded BIN lookup using HandyAPI with local fallback."""
    clean_num = re.sub(r"\D", "", card_number)
    if len(clean_num) < 6:
        return {"error": "Too short"}
        
    bin_digits = clean_num[:6]
    url = f"https://data.handyapi.com/bin/{bin_digits}"
    
    async with semaphore:
        for attempt in range(2): # Retry once on transient network errors
            try:
                if USE_CURL_CFFI and isinstance(session, AsyncSession):
                    response = await session.get(url, timeout=2.5)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("Status") == "SUCCESS":
                            return {
                                "scheme": str(data.get("Scheme", "unknown")).lower(),
                                "type": str(data.get("Type", "credit")).lower(),
                                "brand": data.get("Brand", "Standard"),
                                "country": {
                                    "name": data.get("Country", {}).get("Name", "Unknown"),
                                    "emoji": data.get("Country", {}).get("Emoji", "🏳️"),
                                    "alpha2": data.get("Country", {}).get("A2", "US"),
                                    "currency": data.get("Country", {}).get("Currency", "USD")
                                },
                                "bank": {
                                    "name": data.get("Issuer", "Unknown Bank")
                                }
                            }
                    elif response.status_code == 429:
                        await asyncio.sleep(1.0 * (attempt + 1))
                else:
                    async with session.get(url, timeout=2.5) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data.get("Status") == "SUCCESS":
                                return {
                                    "scheme": str(data.get("Scheme", "unknown")).lower(),
                                    "type": str(data.get("Type", "credit")).lower(),
                                    "brand": data.get("Brand", "Standard"),
                                    "country": {
                                        "name": data.get("Country", {}).get("Name", "Unknown"),
                                        "emoji": data.get("Country", {}).get("Emoji", "🏳️"),
                                        "alpha2": data.get("Country", {}).get("A2", "US"),
                                        "currency": data.get("Country", {}).get("Currency", "USD")
                                    },
                                    "bank": {
                                        "name": data.get("Issuer", "Unknown Bank")
                                    }
                                }
                        elif response.status == 429:
                            await asyncio.sleep(1.0 * (attempt + 1))
            except Exception as e:
                logger.warning("HandyAPI lookup attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(0.5)
                
    return get_local_bin_fallback(bin_digits)


async def simulate_verification_async(
    session: Any, 
    card_number: str, 
    expiry: str, 
    cvv: str, 
    semaphore: asyncio.Semaphore
) -> dict[str, Any]:
    """Run a high-performance verification simulation (Luhn + Async BIN + Status Check)."""
    clean_num = re.sub(r"\D", "", card_number)
    
    # 1. Luhn Check
    is_valid_luhn = validate_luhn(clean_num)
    if not is_valid_luhn:
        return {
            "ok": False,
            "status": "DIE",
            "reason": "Invalid Luhn checksum (Математически неверный номер)",
            "bin_info": get_local_bin_fallback(clean_num[:6] if len(clean_num) >= 6 else "400000")
        }
        
    # 2. Asynchronous BIN Lookup
    bin_info = await check_bin(session, clean_num, semaphore)
    
    # 3. Status Simulation (LIVE / DIE)
    if clean_num.startswith("424242"):
        status = "LIVE"
        reason = "Approved (Тестовая карта Stripe - Одобрено)"
    elif clean_num.endswith("00"):
        status = "DIE"
        reason = "Declined (Карта заблокирована банком)"
    elif clean_num.endswith("99"):
        status = "DIE"
        reason = "Insufficient Funds (Недостаточно средств)"
    elif clean_num.endswith("88"):
        status = "DIE"
        reason = "Expired Card (Истёк срок действия)"
    else:
        # Pseudo-random but deterministic based on card number
        seed = int(clean_num[-6:]) if len(clean_num) >= 6 else 123456
        rng = random.Random(seed)
        
        if rng.random() < 0.75:
            status = "LIVE"
            reason = "Approved (Успешная авторизация)"
        else:
            declines = [
                "Declined (Отказ банка без объяснения причин)",
                "Insufficient Funds (Недостаточно средств)",
                "Incorrect CVV (Неверный CVV код)",
                "Restricted Card (Ограничение на онлайн-покупки)"
            ]
            status = "DIE"
            reason = rng.choice(declines)
            
    return {
        "ok": status == "LIVE",
        "status": status,
        "reason": reason,
        "bin_info": bin_info
    }


async def process_bulk_cards(cards: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """Process a bulk list of cards concurrently using connection pooling and semaphores."""
    # Limit concurrency to 5 to avoid rate-limiting or socket exhaustion
    semaphore = asyncio.Semaphore(5)
    results = []
    
    if USE_CURL_CFFI:
        try:
            logger.info("Processing bulk cards using curl_cffi (impersonate='chrome120')")
            async with AsyncSession(impersonate="chrome120") as session:
                tasks = []
                for card_num, expiry, cvv in cards:
                    tasks.append(simulate_verification_async(session, card_num, expiry, cvv, semaphore))
                results = await asyncio.gather(*tasks)
            return results
        except Exception as e:
            logger.warning("curl_cffi failed, falling back to aiohttp: %s", e)

    # Optimize network sockets with DNS caching
    connector = aiohttp.TCPConnector(
        limit=10,
        ttl_dns_cache=300,
        use_dns_cache=True
    )
    
    timeout = aiohttp.ClientTimeout(total=5.0)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = []
        for card_num, expiry, cvv in cards:
            tasks.append(simulate_verification_async(session, card_num, expiry, cvv, semaphore))
            
        # Execute concurrently and preserve order
        results = await asyncio.gather(*tasks)
        
    return results


async def simulate_verification(
    card_number: str, expiry: str, cvv: str
) -> dict[str, Any]:
    """Verify a single card.

    Thin convenience wrapper around :func:`simulate_verification_async`
    so callers (e.g. the handlers' single-card path) don't have to
    manage an HTTP session / semaphore themselves. Mirrors the session
    setup used in :func:`process_bulk_cards` (curl_cffi → aiohttp).
    """
    semaphore = asyncio.Semaphore(1)
    if USE_CURL_CFFI:
        try:
            async with AsyncSession(impersonate="chrome120") as session:
                return await simulate_verification_async(
                    session, card_number, expiry, cvv, semaphore
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("curl_cffi failed, falling back to aiohttp: %s", e)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, use_dns_cache=True)
    timeout = aiohttp.ClientTimeout(total=5.0)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        return await simulate_verification_async(
            session, card_number, expiry, cvv, semaphore
        )


# ---- DOLPHIN ANTY PROFILES & FINGERPRINTING LOGIC ----

PROFILE_PRESETS = {
    "win_intel_rtx3080": {
        "name": "Windows (Intel i7, RTX 3080)",
        "os": "Windows",
        "hardware": "Intel Core i7-12700K / NVIDIA GeForce RTX 3080",
        "seed": "win_intel_rtx3080_seed_abc"
    },
    "mac_m2_apple": {
        "name": "macOS (M2, Apple Silicon)",
        "os": "macOS",
        "hardware": "Apple M2 / Apple M2 GPU",
        "seed": "mac_m2_apple_seed_xyz"
    },
    "android_pixel7": {
        "name": "Android (Google Pixel 7)",
        "os": "Android",
        "hardware": "Google Tensor G2 / Mali-G710",
        "seed": "android_pixel7_seed_pqr"
    },
    "linux_ryzen_radeon": {
        "name": "Linux (AMD Ryzen, Radeon)",
        "os": "Linux",
        "hardware": "AMD Ryzen 7 5800X / AMD Radeon RX 6700 XT",
        "seed": "linux_ryzen_radeon_seed_mno"
    }
}


def get_fingerprint_seed() -> str:
    """Retrieve the configured fingerprint seed from settings, generating a default if empty."""
    from .. import state as addon_state
    seed = str(addon_state.get(ADDON, "fingerprint_seed", "") or "")
    if not seed:
        seed = f"rand_{secrets.token_hex(8)}"
        addon_state.set_(ADDON, "fingerprint_seed", seed)
    return seed


def set_fingerprint_seed(seed: str) -> None:
    """Save the fingerprint seed to settings."""
    from .. import state as addon_state
    addon_state.set_(ADDON, "fingerprint_seed", seed)


def generate_random_seed() -> str:
    """Generate and save a fresh random fingerprint seed."""
    seed = f"rand_{secrets.token_hex(8)}"
    set_fingerprint_seed(seed)
    return seed


def get_fingerprint_user_agent() -> str:
    """Retrieve the configured User-Agent from settings."""
    from .. import state as addon_state
    return str(addon_state.get(ADDON, "fingerprint_user_agent", "") or "")


def set_fingerprint_user_agent(ua: str) -> None:
    """Save the User-Agent to settings."""
    from .. import state as addon_state
    addon_state.set_(ADDON, "fingerprint_user_agent", ua)


def get_fingerprint_proxy() -> str:
    """Retrieve the configured Proxy from settings."""
    from .. import state as addon_state
    return str(addon_state.get(ADDON, "fingerprint_proxy", "") or "")


def set_fingerprint_proxy(proxy: str) -> None:
    """Save the Proxy to settings."""
    from .. import state as addon_state
    addon_state.set_(ADDON, "fingerprint_proxy", proxy)


def get_fingerprint_webgl_vendor() -> str:
    """Retrieve the configured WebGL Vendor from settings."""
    from .. import state as addon_state
    return str(addon_state.get(ADDON, "fingerprint_webgl_vendor", "") or "")


def set_fingerprint_webgl_vendor(vendor: str) -> None:
    """Save the WebGL Vendor to settings."""
    from .. import state as addon_state
    addon_state.set_(ADDON, "fingerprint_webgl_vendor", vendor)


def get_fingerprint_webgl_renderer() -> str:
    """Retrieve the configured WebGL Renderer from settings."""
    from .. import state as addon_state
    return str(addon_state.get(ADDON, "fingerprint_webgl_renderer", "") or "")


def get_fingerprint_timezone() -> str:
    """Optional timezone override for the stealth fingerprint.

    Empty → derive deterministically from the seed. Set this to match the
    active proxy / xray (xress) exit geo, e.g. 'Europe/Moscow'.
    """
    from .. import state as addon_state

    return str(addon_state.get(ADDON, "fingerprint_timezone", "") or "")


def set_fingerprint_timezone(tz: str) -> None:
    from .. import state as addon_state

    addon_state.set_(ADDON, "fingerprint_timezone", tz)


def get_fingerprint_os() -> str:
    """Optional OS family override ('windows'|'mac'|'linux'). Empty → seed."""
    from .. import state as addon_state

    return str(addon_state.get(ADDON, "fingerprint_os", "") or "")


def set_fingerprint_os(os_name: str) -> None:
    from .. import state as addon_state

    addon_state.set_(ADDON, "fingerprint_os", os_name)


def set_fingerprint_webgl_renderer(renderer: str) -> None:
    """Save the WebGL Renderer to settings."""
    from .. import state as addon_state
    addon_state.set_(ADDON, "fingerprint_webgl_renderer", renderer)
