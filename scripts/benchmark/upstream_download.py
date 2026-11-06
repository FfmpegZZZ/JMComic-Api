"""Five real cold downloads per variant using fresh, disposable Docker tmpfs storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from arm_runner import Resources, cgroup
from upstream_live import upstream_counts


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def download(base, path):
    started = time.perf_counter()
    size = 0
    digest = hashlib.sha256()
    with urllib.request.urlopen(base + path, timeout=180) as response:
        while chunk := response.read(192 * 1024):
            size += len(chunk)
            digest.update(chunk)
        return dict(
            status=response.status,
            seconds=time.perf_counter() - started,
            bytes=size,
            sha256=digest.hexdigest(),
        )


def trial(root, variant, repeat, album):
    name = f"jmapi-arm-download-{variant}-{repeat}"
    port = 18712 if variant == "baseline" else 18713
    base = f"http://127.0.0.1:{port}"
    image = f"jmcomic-api:bench-{variant}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--init",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--tmpfs",
        "/data:rw,size=1g,uid=10001,gid=10001",
        "-p",
        f"127.0.0.1:{port}:8699",
        "--mount",
        f"type=bind,src={root / 'live-option.yml'},dst=/app/live-option.yml,readonly",
        "-e",
        "JMAPI_OPTION_FILE=/app/live-option.yml",
        "-e",
        "JMAPI_PDF_DIR=/data/pdf",
        "-e",
        "JMAPI_PDF_SHARD_CACHE_DIR=/data/shards",
        "-e",
        "JMAPI_LOG_FORMAT=json",
        image,
        "uvicorn",
        "jmcomic_api.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8699",
        "--no-access-log",
        "--timeout-graceful-shutdown",
        "30",
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(base + "/health/ready", timeout=2) as response:
                    json.load(response)
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Download container did not become ready") from None
                time.sleep(0.2)
        path = f"/get_pdf/{album}?passwd=false&pdf=true&quality=70"
        before = upstream_counts(base)
        monitor = Resources(cgroup(name))
        monitor.start()
        try:
            cold = download(base, path)
        finally:
            resources = monitor.finish()
        cold["resources"] = resources
        after = upstream_counts(base)
        cold["upstream_delta"] = {key: value - before.get(key, 0) for key, value in after.items()}
        warm = download(base, path)
        with urllib.request.urlopen(
            base + f"/get_pdf_path/{album}?passwd=false&quality=70", timeout=30
        ) as response:
            pdf_path = json.load(response)["data"]
        metadata = json.loads(
            docker(
                "exec",
                name,
                "python",
                "-c",
                "import hashlib,json,sys;from pathlib import Path;from pypdf import PdfReader;"
                "p=Path(sys.argv[1]);"
                "f=p.open('rb');r=PdfReader(f);pages=len(r.pages);f.close();"
                "images=sorted(p for p in Path('/data/webp').rglob('*') if p.is_file() "
                "and p.suffix.lower() in ('.webp','.jpg','.jpeg','.png'));"
                "print(json.dumps(dict(pages=pages,images=len(images),"
                "image_bytes=sum(p.stat().st_size for p in images),"
                "image_sha256=hashlib.sha256(b''.join(hashlib.sha256(p.read_bytes()).digest() "
                "for p in images)).hexdigest())))",
                pdf_path,
            )
        )
        if metadata["pages"] != metadata["images"] or metadata["pages"] == 0:
            raise RuntimeError("Downloaded PDF page count mismatch")
        return dict(cold=cold, hot=warm, validation=metadata)
    finally:
        # Only this trial's disposable container/storage is removed; reports persist.
        docker("stop", "--timeout", "40", name)
        docker("rm", name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--album", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    root = Path.cwd()
    report = dict(
        album_id=args.album,
        repeats=5,
        storage="fresh 1 GiB tmpfs per trial",
        note="Cold API includes real network, decoding, PDF build and transfer.",
        variants={"baseline": [], "optimized": []},
    )
    for repeat in range(5):
        variants = ["baseline", "optimized"] if repeat % 2 == 0 else ["optimized", "baseline"]
        for variant in variants:
            try:
                result = trial(root, variant, repeat, args.album)
            except Exception as error:
                report["failure"] = dict(
                    variant=variant, repeat=repeat, error_type=type(error).__name__
                )
                args.output.write_text(json.dumps(report, indent=2))
                raise
            report["variants"][variant].append(result)
            args.output.write_text(json.dumps(report, indent=2))
            print(json.dumps(dict(variant=variant, repeat=repeat, **result)), flush=True)
            if any(
                'outcome="rate_limited"' in key and value > 0
                for key, value in result["cold"]["upstream_delta"].items()
            ):
                raise RuntimeError("Upstream rate limited; stopped repeated downloads")
            time.sleep(1)


if __name__ == "__main__":
    main()
