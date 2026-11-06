"""Request one real album from both deployed APIs and validate PDF/Base64 equality."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
import urllib.request
from pathlib import Path

from arm_runner import Resources, cgroup
from upstream_download import docker, download
from upstream_live import upstream_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--album", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(album_id=args.album, variants={})
    for variant, port in (("baseline", 18710), ("optimized", 18711)):
        container = f"jmapi-arm-live-{variant}-1"
        base = f"http://127.0.0.1:{port}"
        started = time.perf_counter()
        with urllib.request.urlopen(base + f"/album/{args.album}", timeout=45) as response:
            detail = json.load(response)
            assert response.status == 200 and str(detail["data"]["id"]) == args.album
        detail_seconds = time.perf_counter() - started
        path = f"/get_pdf/{args.album}?passwd=false&pdf=true&quality=70"
        before = upstream_counts(base)
        monitor = Resources(cgroup(container))
        monitor.start()
        try:
            cold = download(base, path)
        finally:
            resources = monitor.finish()
        cold["resources"] = resources
        after = upstream_counts(base)
        cold["upstream_delta"] = {key: value - before.get(key, 0) for key, value in after.items()}
        hot = [download(base, path) for _ in range(5)]
        with urllib.request.urlopen(
            base + f"/get_pdf_path/{args.album}?passwd=false&quality=70", timeout=30
        ) as response:
            pdf_path = json.load(response)["data"]
        pages = int(
            docker(
                "exec",
                container,
                "python",
                "-c",
                "import sys;from pypdf import PdfReader;"
                "f=open(sys.argv[1],'rb');print(len(PdfReader(f).pages));f.close()",
                pdf_path,
            )
        )
        with urllib.request.urlopen(
            base + f"/get_pdf/{args.album}?passwd=false&quality=70", timeout=120
        ) as response:
            decoded = base64.b64decode(json.load(response)["data"], validate=True)
        digest = hashlib.sha256(decoded).hexdigest()
        assert len(decoded) == cold["bytes"] and digest == cold["sha256"]
        del decoded
        assert all(item["sha256"] == digest for item in hot)
        result = dict(
            detail_seconds=detail_seconds, cold=cold, hot=hot, pages=pages, base64_matches_pdf=True
        )
        report["variants"][variant] = result
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(variant=variant, **result)), flush=True)


if __name__ == "__main__":
    main()
