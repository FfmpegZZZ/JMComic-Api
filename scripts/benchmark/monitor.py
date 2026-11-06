"""Independent RSS sampler so a blocked server loop cannot hide memory peaks."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main():
    output = Path(sys.argv[1])
    epoch_path = output.with_suffix(".epoch")
    epoch = ""
    peak_rss = peak_threads = 0
    while True:
        try:
            current_epoch = epoch_path.read_text()
        except OSError:
            current_epoch = ""
        if current_epoch != epoch:
            epoch = current_epoch
            peak_rss = peak_threads = 0
        rss = threads = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes()
                if b"uvicorn" not in command and b"jmcomic_api.services.pdf_worker" not in command:
                    continue
                for line in (entry / "status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        rss += int(line.split()[1]) * 1024
                    elif line.startswith("Threads:"):
                        threads += int(line.split()[1])
            except (OSError, ValueError):
                continue
        peak_rss = max(peak_rss, rss)
        peak_threads = max(peak_threads, threads)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                dict(
                    rss_bytes=rss,
                    threads=threads,
                    peak_rss_bytes=peak_rss,
                    peak_threads=peak_threads,
                    epoch=epoch,
                )
            )
        )
        os.replace(temporary, output)
        time.sleep(0.01)


if __name__ == "__main__":
    main()
