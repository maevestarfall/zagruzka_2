"""Tests for the Master Room full-project ZIP export builder.

Covers :func:`bot.addons.masterroom.project_zip.build_project_zip`:

* the archive is a valid, non-empty zip
* heavy / sensitive dirs (.git, node_modules, .venv, __pycache__, …) are excluded
* junk files (*.pyc, *.db, .env) are excluded
* a known source file (bot/main.py) is present

Builds against a synthetic temp repo (not the real tree) so the test is fast,
deterministic, and independent of the project's current size.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from bot.addons.masterroom.project_zip import build_project_zip, project_root


def _make_fake_repo(root: Path) -> None:
    # files that SHOULD be included
    (root / "bot").mkdir(parents=True)
    (root / "bot" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    # dirs that MUST be excluded
    for junk_dir in (".git", "node_modules", ".venv", "__pycache__",
                     ".pytest_cache", ".ruff_cache", "session"):
        d = root / junk_dir
        d.mkdir()
        (d / "junk.txt").write_text("nope", encoding="utf-8")
    # files that MUST be excluded
    (root / "data.db").write_text("sqlite", encoding="utf-8")
    (root / "cache.pyc").write_text("bytecode", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")


def test_build_project_zip_excludes_junk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_fake_repo(repo)
    out = tmp_path / "out"

    zip_path = build_project_zip(out_dir=str(out), repo_root=repo)

    assert zipfile.is_zipfile(zip_path)
    names = set(zipfile.ZipFile(zip_path).namelist())

    # included
    assert "bot/main.py" in names
    assert "README.md" in names

    # excluded dirs — nothing from them
    for junk_dir in (".git", "node_modules", ".venv", "__pycache__",
                     ".pytest_cache", ".ruff_cache", "session"):
        assert not any(n.startswith(junk_dir + "/") for n in names), junk_dir

    # excluded files
    assert "data.db" not in names
    assert "cache.pyc" not in names
    assert ".env" not in names


def test_build_project_zip_not_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_fake_repo(repo)
    zip_path = build_project_zip(out_dir=str(tmp_path / "out"), repo_root=repo)
    assert len(zipfile.ZipFile(zip_path).namelist()) >= 2


def test_project_root_points_at_repo() -> None:
    # project_root() should resolve to the dir that actually contains bot/
    root = project_root()
    assert (root / "bot").is_dir()
