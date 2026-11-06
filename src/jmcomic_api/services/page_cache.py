"""Single-page encoding cache shared by full/shard worker processes.

Per-page locks protect readers/encoders; a short quota lock serializes byte
accounting and eviction. Reconcile once per build instead of scanning on every
page. Lock files remain so concurrent workers always lock the same inode.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import tempfile
import time
from pathlib import Path

from PIL import Image

from jmcomic_api.services.storage import atomic_json, digest_file, identity, read_json

ENCODER_VERSION = "pillow-pdf-v2"


def encode_page(path: Path, *, jpeg_quality=None, jpeg_optimize=True) -> bytes:
    with Image.open(path) as image, image.convert("RGB") as rgb, io.BytesIO() as buffer:
        options = dict(optimize=jpeg_optimize)
        if jpeg_quality is not None:
            options["quality"] = jpeg_quality
        rgb.save(buffer, format="PDF", **options)
        return buffer.getvalue()


def _entries(root: Path):
    candidates = []
    for path in root.glob("*.pdf"):
        with contextlib.suppress(OSError):
            stat = path.stat()
            candidates.append((stat.st_mtime_ns, stat.st_size, path))
    return candidates


@contextlib.contextmanager
def _quota_lock(root: Path):
    with (root / ".quota.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _usage(root: Path) -> int:
    state = read_json(root / ".usage.json")
    if isinstance(state, dict) and isinstance(state.get("bytes"), int) and state["bytes"] >= 0:
        return state["bytes"]
    return sum(size for _, size, _ in _entries(root))


def _evict_locked(root: Path, capacity: int) -> int:
    candidates = _entries(root)
    total = sum(size for _, size, _ in candidates)
    for _, size, path in sorted(candidates):
        if total <= capacity:
            break
        with path.with_suffix(".lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                path.unlink(missing_ok=True)
                total -= size
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return total


def evict(root: Path, capacity: int):
    """Reconcile actual usage and enforce quota; called once at build start."""
    root.mkdir(parents=True, exist_ok=True)
    with _quota_lock(root):
        atomic_json(root / ".usage.json", dict(bytes=_evict_locked(root, capacity)))


@contextlib.contextmanager
def page_stream(
    path: Path, *, root: Path | None, capacity: int, jpeg_quality=None, jpeg_optimize=True
):
    if root is None or capacity == 0:
        with io.BytesIO(
            encode_page(path, jpeg_quality=jpeg_quality, jpeg_optimize=jpeg_optimize)
        ) as stream:
            yield stream
        return
    root.mkdir(parents=True, exist_ok=True)
    key = identity([digest_file(path), jpeg_quality, jpeg_optimize, ENCODER_VERSION])
    cached = root / f"{key}.pdf"
    overflow = False
    try:
        with cached.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if not cached.exists():
                    fd, name = tempfile.mkstemp(prefix=".jmapi-page-", suffix=".tmp", dir=root)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(
                                encode_page(
                                    path, jpeg_quality=jpeg_quality, jpeg_optimize=jpeg_optimize
                                )
                            )
                        size = Path(name).stat().st_size
                        with _quota_lock(root):
                            # Read accounting before publication: an unindexed
                            # cache scan must not count this page twice.
                            total = _usage(root)
                            os.replace(name, cached)
                            total += size
                            if total > capacity:
                                total = _evict_locked(root, capacity)
                            atomic_json(root / ".usage.json", dict(bytes=total))
                            overflow = total > capacity
                    finally:
                        Path(name).unlink(missing_ok=True)
                os.utime(cached, ns=(time.time_ns(), time.time_ns()))
                with cached.open("rb") as stream:
                    yield stream
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        if overflow:
            # Oversized/in-use pages can only be removed after releasing the
            # reader lock. Hits below quota need no directory scan or ledger IO.
            evict(root, capacity)
