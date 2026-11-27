"""Full-project ZIP export for Master Room.

Server-side equivalent of the web inspector's ``DownloadZip.tsx`` (which used
JSZip to bundle the FILES list client-side). Here we walk the real repo root on
the server and bundle it with :mod:`zipfile`, reusing the same exclusion policy
as :func:`bot.addons.regen.outputs.build_fix_zip` so heavy / sensitive paths
never end up in the archive.

Excluded dirs: ``.venv``, ``node_modules``, ``.git``, ``__pycache__``,
``.pytest_cache``, ``.ruff_cache``, ``session``. Also skips ``*.pyc``, SQLite
DB files (``*.db`` / ``*.sqlite*``) and ``.env`` secrets.
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

# Same set build_fix_zip uses, so behaviour matches the rest of the project.
_EXCLUDE_DIRS = {
    ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".git", "session", "node_modules",
}
# File suffixes / names we never want in a shared backup.
_EXCLUDE_SUFFIXES = (".pyc", ".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm")
_EXCLUDE_NAMES = {".env"}


def project_root() -> Path:
    """Repo root = three levels up from this file (bot/addons/masterroom/…)."""
    return Path(__file__).resolve().parents[3]


def _is_excluded_file(name: str) -> bool:
    if name in _EXCLUDE_NAMES:
        return True
    low = name.lower()
    return any(low.endswith(suf) for suf in _EXCLUDE_SUFFIXES)


def build_project_zip(out_dir: str | None = None, repo_root: Path | None = None) -> str:
    """Create a ZIP of the whole project root and return its path.

    Walks ``repo_root`` (auto-detected when omitted), skipping the excluded
    dirs/files, and writes ``project_backup_<timestamp>.zip`` into ``out_dir``
    (defaults to ``<repo>/data`` so it lands on the persistent disk; falls back
    to ``/tmp`` when ``data`` isn't writable).
    """
    root = Path(repo_root) if repo_root else project_root()

    if out_dir is None:
        data_dir = root / "data"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            out_dir = str(data_dir)
        except Exception:  # noqa: BLE001
            out_dir = "/tmp"
    os.makedirs(out_dir, exist_ok=True)

    tag = time.strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(out_dir, f"project_backup_{tag}.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dp, dns, fns in os.walk(root):
            # prune excluded dirs in-place so os.walk doesn't descend into them
            dns[:] = [d for d in dns if d not in _EXCLUDE_DIRS]
            for fn in fns:
                if _is_excluded_file(fn):
                    continue
                fp = Path(dp) / fn
                # never include the archive we're currently writing
                if fp.resolve() == Path(zip_path).resolve():
                    continue
                try:
                    zf.write(fp, fp.relative_to(root))
                except Exception:  # noqa: BLE001 — unreadable/transient file, skip
                    continue
    return zip_path
