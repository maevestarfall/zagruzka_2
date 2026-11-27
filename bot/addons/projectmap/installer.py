# Project-map installer: clone + set up graphify under the mounted disk.
# Co-authored with CoCo
"""Download graphify (https://github.com/safishamsi/graphify) into /data.

Mirrors the lazy pattern used for agent-browser / bash2mp4: nothing is
baked into the Docker image; the first time the owner presses «Скачать
graphify» we clone the repo under the persisted ``/data`` disk and run a
best-effort setup (npm/pip auto-detected). It survives redeploys.

The interactive graph itself is rendered by our own self-contained HTML
(see :mod:`.server`) built from an AST scan, so the feature works even if
graphify's own build fails — graphify assets are used when present.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import store

logger = logging.getLogger(__name__)

GRAPHIFY_REPO = "https://github.com/safishamsi/graphify"


def _data_dir() -> Path:
    try:
        from ...config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:  # noqa: BLE001
        return Path("/data")


def graphify_dir() -> Path:
    return _data_dir() / "projects" / "graphify"


def is_installed() -> bool:
    d = graphify_dir()
    return d.is_dir() and any(d.iterdir()) if d.exists() else False


def project_root() -> str:
    """Root of the bot's own source (…/lilush-bot), used as the default map."""
    # this file: bot/addons/projectmap/installer.py  → parents[3] == repo root
    return str(Path(__file__).resolve().parents[3])


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def install_sync() -> dict:
    """Clone + set up graphify. Blocking — call via ``asyncio.to_thread``.

    Returns ``{"ok": bool, "status": str, "log": str}`` and updates the
    persisted install status so the UI can reflect it.
    """
    store.set_install("installing")
    d = graphify_dir()
    d.parent.mkdir(parents=True, exist_ok=True)
    log_parts: list[str] = []

    if is_installed():
        rc, out = _run(["git", "-C", str(d), "pull", "--ff-only"], timeout=180)
        log_parts.append(f"$ git pull → {rc}\n{out[-800:]}")
    else:
        rc, out = _run(["git", "clone", "--depth=1", GRAPHIFY_REPO, str(d)], timeout=300)
        log_parts.append(f"$ git clone → {rc}\n{out[-800:]}")
        if rc != 0:
            store.set_install("error")
            return {"ok": False, "status": "error", "log": "\n".join(log_parts)}

    # Best-effort dependency setup — auto-detect Node vs Python project.
    if (d / "package.json").is_file():
        rc, out = _run(["npm", "install", "--no-audit", "--no-fund"], cwd=d, timeout=600)
        log_parts.append(f"$ npm install → {rc}\n{out[-600:]}")
    if (d / "requirements.txt").is_file():
        rc, out = _run(["pip", "install", "--no-cache-dir", "-r", "requirements.txt"], cwd=d, timeout=600)
        log_parts.append(f"$ pip install → {rc}\n{out[-600:]}")

    store.set_install("ready")
    return {"ok": True, "status": "ready", "log": "\n".join(log_parts)}
