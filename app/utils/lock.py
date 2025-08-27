import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_LOCK_DIR = Path(os.getenv("JM_LOCK_DIR", "./.locks"))
DEFAULT_LOCK_DIR.mkdir(parents=True, exist_ok=True)

@contextmanager
def file_lock(name: str, timeout: float = 300.0, poll_interval: float = 0.25) -> Iterator[None]:
    """A simple cross-thread/process lock via atomic directory creation.

    We prefer directory creation because it's atomic on most filesystems and
    avoids platform differences of fcntl.
    """
    lock_path = DEFAULT_LOCK_DIR / f"{name}.lock"
    start = time.time()
    while True:
        try:
            # atomic: create directory; fail if exists
            lock_path.mkdir(exist_ok=False)
            break
        except FileExistsError:
            if (time.time() - start) > timeout:
                raise TimeoutError(f"Timeout acquiring lock: {lock_path}")
            time.sleep(poll_interval)
    try:
        yield
    finally:
        # Best effort cleanup
        try:
            for p in lock_path.iterdir():
                # allow storing temp marker files if needed later
                try:
                    p.unlink()
                except OSError:
                    pass
            lock_path.rmdir()
        except OSError:
            pass
