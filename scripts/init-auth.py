"""Create local account encryption state without ever overwriting a key."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, default=Path("secrets/session.key"))
    parser.add_argument("--state-dir", type=Path, default=Path("state/sessions"))
    args = parser.parse_args()
    args.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.key_file.exists():
        raise SystemExit("Key already exists; keep it to preserve existing account sessions.")
    fd = os.open(args.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    print(f"Created key: {args.key_file.resolve()}")
    print(f"Session directory: {args.state_dir.resolve()}")
    print("Run the container with the same UID/GID as these files' owner.")


if __name__ == "__main__":
    main()
