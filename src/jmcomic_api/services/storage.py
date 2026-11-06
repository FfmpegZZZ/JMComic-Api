"""Local storage primitives: confinement, immutable manifests and file leases."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from fastapi import HTTPException
from PIL import Image

from jmcomic_api import metrics


def confined(root: str | Path, path: str | Path) -> Path:
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(422, "File path must stay inside the configured storage root")
    return path


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".jmapi-", suffix=".json.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_attributes(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def disk_admission(root: Path, minimum_mb: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < minimum_mb * 1024**2:
        metrics.DISK_REJECTIONS.inc()
        raise HTTPException(
            503, "Insufficient disk space; try again later", headers={"Retry-After": "30"}
        )


class FileLeases:
    def __init__(self) -> None:
        self._refs: Counter[Path] = Counter()
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def hold(self, path: Path):
        path = path.resolve()
        with self._lock:
            self._refs[path] += 1
        try:
            yield
        finally:
            with self._lock:
                self._refs[path] -= 1
                if not self._refs[path]:
                    del self._refs[path]

    def remove(self, path: Path) -> bool:
        with self._lock:
            if self._refs.get(path.resolve(), 0):
                return False
            path.unlink(missing_ok=True)
            return True

    def active_under(self, path: Path) -> bool:
        with self._lock:
            return any(p.is_relative_to(path.resolve()) for p in self._refs)


class FilePin:
    """Transfer a lease from cache lookup to the returned artifact's lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._lease = leases.hold(path)
        self._lease.__enter__()

    def close(self):
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.__exit__(None, None, None)

    def __del__(self):
        self.close()


leases = FileLeases()


def manifest_path(root: Path, album_id: str) -> Path:
    return confined(root, root / ".jmapi" / f"{album_id}.json")


def make_manifest(root: Path, album_id: str, title: str, paths: list[Path], expected: int):
    entries = []
    for path in paths:
        path = confined(root, path)
        with Image.open(path) as image:
            image.verify()
        attrs = file_attributes(path)
        entries.append(
            dict(
                path=str(path.relative_to(root.resolve())),
                size=attrs[0],
                mtime_ns=attrs[1],
                digest=digest_file(path),
            )
        )
    if not entries or len(entries) != expected or len({e["path"] for e in entries}) != expected:
        raise ValueError(f"incomplete image manifest: {len(entries)}/{expected}")
    data = dict(version=2, album_id=album_id, title=title, expected_pages=expected, entries=entries)
    # Attributes detect changes; content/order define PDF identity.
    data["fingerprint"] = identity([(e["path"], e["digest"]) for e in entries])
    return data


def validate_manifest(root: Path, data) -> bool:
    try:
        if (
            data["version"] != 2
            or data["expected_pages"] <= 0
            or len(data["entries"]) != data["expected_pages"]
            or not isinstance(data["title"], str)
            or not isinstance(data["fingerprint"], str)
        ):
            return False
        for entry in data["entries"]:
            path = confined(root, root / entry["path"])
            stat = path.stat()
            if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime_ns"]:
                return False
            # A build verifies content as well, including same-size edits.
            if digest_file(path) != entry["digest"]:
                return False
            with Image.open(path) as image:
                image.verify()
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def cleanup(roots: list[Path], *, ttl=0, max_bytes=0):
    """Only v2 PDFs are optional eviction candidates; never delete legacy files."""
    now = time.time()
    candidates = []
    for root in set(roots):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
                if path.is_dir():
                    if (
                        path.name.startswith(".jmapi-")
                        and path.name.endswith(".tmp")
                        and now - stat.st_mtime > 86400
                        and not leases.active_under(path.parent)
                    ):
                        shutil.rmtree(path)
                    continue
                if not path.is_file():
                    continue
                if path.name.startswith(".jmapi-") and path.name.endswith(".tmp"):
                    if now - stat.st_mtime > 86400 and not leases.active_under(path.parent):
                        leases.remove(path)
                elif (
                    path.suffix == ".pdf"
                    and "v2" in path.relative_to(root).parts
                    and "pages" not in path.parts
                ):
                    candidates.append((stat.st_atime, stat.st_size, path))
            except OSError:
                continue
    total = sum(size for _, size, _ in candidates)
    for modified, size, path in sorted(candidates):
        if ((ttl and now - modified > ttl) or (max_bytes and total > max_bytes)) and leases.remove(
            path
        ):
            total -= size
