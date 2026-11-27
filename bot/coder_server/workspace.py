"""Isolated workspaces and strict path/archive validation."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

_DENIED = {".git", ".env", ".ssh", "node_modules", "__pycache__"}


class WorkspaceViolation(ValueError):
    pass


class Workspace:
    def __init__(self, root: str | Path, task_id: str):
        if not task_id.replace("_", "").replace("-", "").isalnum():
            raise WorkspaceViolation("invalid task id")
        self.root = Path(root).resolve() / task_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def resolve(self, relative: str, *, allow_missing: bool = True) -> Path:
        pure = PurePosixPath(relative.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or any(p in _DENIED for p in pure.parts):
            raise WorkspaceViolation("path is outside allowed workspace")
        candidate = self.root.joinpath(*pure.parts)
        parent = candidate.parent.resolve(strict=False)
        if os.path.commonpath((str(self.root), str(parent))) != str(self.root):
            raise WorkspaceViolation("path escaped workspace")
        probe = self.root
        for part in pure.parts:
            probe = probe / part
            if probe.is_symlink():
                raise WorkspaceViolation("symlink access is forbidden")
        if not allow_missing and not candidate.exists():
            raise FileNotFoundError(relative)
        return candidate

    def read(self, relative: str, max_bytes: int = 2_000_000) -> bytes:
        path = self.resolve(relative, allow_missing=False)
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise WorkspaceViolation("file is not readable or exceeds limit")
        return path.read_bytes()

    def write_atomic(self, relative: str, data: bytes, max_bytes: int = 5_000_000) -> Path:
        if len(data) > max_bytes:
            raise WorkspaceViolation("write exceeds limit")
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.prepared")
        temp.write_bytes(data)
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
        temp.replace(path)
        return path

    def extract_zip(self, archive: str, destination: str = "", *, max_files: int = 2000, max_bytes: int = 100_000_000) -> list[str]:
        source, target = self.resolve(archive, allow_missing=False), self.resolve(destination or ".")
        written: list[str] = []
        with zipfile.ZipFile(source) as zf:
            infos = zf.infolist()
            if len(infos) > max_files or sum(i.file_size for i in infos) > max_bytes:
                raise WorkspaceViolation("unsafe archive size")
            for info in infos:
                if info.is_dir():
                    continue
                name = PurePosixPath(info.filename)
                if name.is_absolute() or ".." in name.parts or any(p in _DENIED for p in name.parts):
                    raise WorkspaceViolation("unsafe archive member")
                if info.compress_size and info.file_size / info.compress_size > 200:
                    raise WorkspaceViolation("suspicious compression ratio")
                output = self.resolve(str(PurePosixPath(destination) / name))
                output.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, output.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                written.append(str(output.relative_to(self.root)))
        return written

    def digest(self) -> str:
        h = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink() and not any(x in _DENIED for x in path.parts):
                h.update(str(path.relative_to(self.root)).encode())
                h.update(path.read_bytes())
        return h.hexdigest()
