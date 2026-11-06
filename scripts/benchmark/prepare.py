"""Deterministic offline dataset; run once, share the same files read-only."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, default=Path("/data"))
args = parser.parse_args()
root = args.root
root.mkdir(parents=True, exist_ok=True)
for count in (100, 500):
    folder = root / "images" / f"[{count}]Fixed {count}"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        path = folder / f"{index:04}.jpg"
        if not path.exists():
            pixels = random.Random(20261105 + index).randbytes(512 * 768 * 3)
            with Image.frombytes("RGB", (512, 768), pixels) as image:
                image.save(path, quality=90)
for album_id, size in (("10", 10), ("11", 100)):
    path = root / f"transport-{album_id}.pdf"
    with path.open("wb") as stream:
        stream.write(b"%PDF-1.4\n")
        stream.truncate(size * 1024**2)
print("Prepared fixed 100/500 page datasets and 10/100 MiB transport files")
