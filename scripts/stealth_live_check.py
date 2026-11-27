#!/usr/bin/env python3
"""CLI: run the stealth fingerprint live-check in a real Chromium.

Usage:
    python -m scripts.stealth_live_check [URL]
    python scripts/stealth_live_check.py [URL] [--seed S] [--tz Europe/London] [--os mac]

Without a URL it probes about:blank (fully offline). Pass a detector URL
(e.g. a CreepJS / browserleaks page) to additionally load it in the
spoofed context. Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running both as a module and as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.addons import stealth_fp_check  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Stealth fingerprint live-check")
    ap.add_argument("url", nargs="?", default=None, help="optional page to load")
    ap.add_argument("--seed", default="live_check_seed")
    ap.add_argument("--tz", default="Europe/Berlin")
    ap.add_argument("--os", dest="os_hint", default="windows",
                    choices=["windows", "mac", "linux"])
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    args = ap.parse_args()

    result = stealth_fp_check.run_sync(
        seed=args.seed, os_hint=args.os_hint, timezone=args.tz,
        url=args.url, headless=not args.headful,
    )
    print(stealth_fp_check.format_report(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
