"""Additional live checks against the dedicated offline comparison containers."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from pypdf import PdfReader


def read(url, *, collect=True, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if collect:
            return response.status, dict(response.headers), response.read()
        while response.read(192 * 1024):
            pass
        return response.status


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def sustained_health(base, *, health_samples=100):
    stop = threading.Event()
    barrier = threading.Barrier(21)
    lock = threading.Lock()
    active = peak = completed = 0
    errors = []

    def load():
        nonlocal active, peak, completed
        barrier.wait()
        while not stop.is_set():
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                assert read(base + "/get_pdf/10?passwd=false", collect=False) == 200
                with lock:
                    completed += 1
            except Exception as error:
                with lock:
                    errors.append(str(error))
                stop.set()
            finally:
                with lock:
                    active -= 1

    def probe():
        latencies = []
        for _ in range(health_samples // 5):
            start = time.perf_counter()
            status, _, content = read(base + "/health/live", timeout=30)
            assert status == 200 and json.loads(content)["status"] == "ok"
            latencies.append(time.perf_counter() - start)
            time.sleep(0.05)
        return latencies

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        workers = [pool.submit(load) for _ in range(20)]
        barrier.wait()
        time.sleep(0.1)
        try:
            probes = [pool.submit(probe) for _ in range(5)]
            latencies = [value for future in probes for value in future.result()]
        finally:
            stop.set()
            for worker in workers:
                worker.result()
    return dict(
        duration_seconds=time.perf_counter() - started,
        load_workers=20,
        health_probe_workers=5,
        samples=len(latencies),
        completed_data_requests=completed,
        peak_active_client_requests=peak,
        errors=errors,
        median_seconds=percentile(latencies, 0.5),
        p95_seconds=percentile(latencies, 0.95),
        max_seconds=max(latencies),
        pass_target=not errors and percentile(latencies, 0.95) <= 0.2,
    )


def streamed_base64_digest(url):
    digest = hashlib.sha256()
    decoded_size = received = 0
    with urllib.request.urlopen(url, timeout=120) as response:
        prefix = bytearray()
        marker = b'"data":"'
        while marker not in prefix:
            chunk = response.read(8192)
            assert chunk and len(prefix) < 65536, "JSON data field missing"
            prefix.extend(chunk)
            received += len(chunk)
        header, data = bytes(prefix).split(marker, 1)
        metadata = json.loads(header + b'"data":""}')
        carry = b""
        while True:
            quote = data.find(b'"')
            encoded = data if quote < 0 else data[:quote]
            carry += encoded
            end = len(carry) // 4 * 4
            if end:
                decoded = base64.b64decode(carry[:end], validate=True)
                digest.update(decoded)
                decoded_size += len(decoded)
                carry = carry[end:]
            if quote >= 0:
                rest = response.read()
                received += len(rest)
                assert (data[quote:] + rest).strip() == b'"}'
                assert not carry
                break
            data = response.read(192 * 1024)
            assert data, "JSON response ended early"
            received += len(data)
        if response.headers.get("Content-Length"):
            assert received == int(response.headers["Content-Length"])
    return metadata, decoded_size, digest.hexdigest()


def sparse_digest(size):
    digest = hashlib.sha256()
    header = b"%PDF-1.4\n"
    digest.update(header)
    remaining = size - len(header)
    chunk = bytes(1024**2)
    while remaining:
        count = min(remaining, len(chunk))
        digest.update(chunk[:count])
        remaining -= count
    return digest.hexdigest()


def pdf_summary(content, *, password=None):
    with io.BytesIO(content) as stream:
        reader = PdfReader(stream)
        encrypted = reader.is_encrypted
        if password is not None:
            assert encrypted and reader.decrypt(password)
        else:
            assert not encrypted
        count = len(reader.pages)
        sampled = {}
        for index in sorted({0, count // 2, count - 1}):
            page = reader.pages[index]
            objects = page["/Resources"]["/XObject"].get_object()
            images = [
                value.get_object()
                for value in objects.values()
                if value.get_object().get("/Subtype") == "/Image"
            ]
            assert len(images) == 1
            with Image.open(io.BytesIO(images[0].get_data())) as image:
                sampled[str(index)] = dict(
                    size=list(image.size), pixels_sha256=hashlib.sha256(image.tobytes()).hexdigest()
                )
        reader.close()
    return dict(pages=count, encrypted=encrypted, sampled_images=sampled)


def functional(base, *, optimized):
    report = {}
    for album_id, size in (("10", 10 * 1024**2), ("11", 100 * 1024**2)):
        metadata, decoded_size, digest = streamed_base64_digest(
            base + f"/get_pdf/{album_id}?passwd=false"
        )
        assert metadata["success"] is True and metadata["name"] == "中文传输测试.pdf"
        assert decoded_size == size and digest == sparse_digest(size)
        report[f"transport_{size // 1024**2}MiB"] = dict(
            decoded_bytes=decoded_size, sha256=digest, passed=True
        )
    raw_url = base + "/get_pdf/100?pdf=true&passwd=false&quality=70"
    status, headers, content = read(raw_url)
    assert status == 200 and headers.get(
        "content-type", headers.get("Content-Type", "")
    ).startswith("application/pdf")
    plain_summary = pdf_summary(content)
    assert plain_summary["pages"] == 100
    raw_hash = hashlib.sha256(content).hexdigest()
    metadata, size, encoded_hash = streamed_base64_digest(
        base + "/get_pdf/100?passwd=false&quality=70"
    )
    assert encoded_hash == raw_hash and size == len(content)
    assert metadata["name"] == "[100] Fixed 100.q70.pdf"
    report["raw_vs_base64"] = dict(passed=True, pdf=plain_summary, sha256=raw_hash)
    _, _, shard = read(base + "/pdf/shard/500/2?pdf=true&passwd=false&quality=70")
    shard_summary = pdf_summary(shard)
    assert shard_summary["pages"] == 100
    report["shard_500_part_2"] = dict(passed=True, pdf=shard_summary)
    _, _, info = read(base + "/pdf/info/500")
    layout = json.loads(info)["data"]
    assert layout["total_pages"] == 500 and len(layout["shards"]) == 5
    report["shard_layout"] = dict(passed=True, total_pages=500, shards=5)
    _, _, plain_path = read(base + "/get_pdf_path/100?passwd=false&quality=70&Titletype=1")
    _, _, encrypted_path = read(base + "/get_pdf_path/100?passwd=true&quality=70&Titletype=1")
    plain_path, encrypted_path = json.loads(plain_path), json.loads(encrypted_path)
    assert plain_path["name"] == encrypted_path["name"] == "Fixed 100.q70.pdf"
    _, _, encrypted = read(base + "/get_pdf/100?pdf=true&passwd=true&quality=70&Titletype=1")
    encrypted_summary = pdf_summary(encrypted, password="100")
    assert encrypted_summary["pages"] == 100
    report["encryption"] = dict(
        passed=True,
        password="album ID",
        independent_paths=plain_path["data"] != encrypted_path["data"],
        plain_path=plain_path["data"],
        encrypted_path=encrypted_path["data"],
    )
    if optimized:
        assert plain_path["data"] != encrypted_path["data"]
        assert "/v2/100/" in plain_path["data"]
        for path in (
            "/get_pdf/invalid",
            "/get_pdf_path/invalid",
            "/pdf/info/invalid",
            "/pdf/shard/invalid/1",
            "/album/invalid",
        ):
            try:
                read(base + path)
                raise AssertionError(f"invalid ID accepted: {path}")
            except urllib.error.HTTPError as error:
                assert error.code == 422
                body = json.load(error)
                assert body["success"] is False and "detail" not in body
        status, _, normalized = read(
            base + "/get_pdf_path/jM00100?passwd=false&quality=70&Titletype=1"
        )
        assert status == 200 and json.loads(normalized)["data"] == plain_path["data"]
        report["id_validation"] = dict(passed=True, invalid_routes=5, canonical_jm_prefix=True)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="http://127.0.0.1:18700")
    parser.add_argument("--optimized", default="http://127.0.0.1:18701")
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/docker-2026-11-05"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {}
    for label, base in (("baseline", args.baseline), ("optimized", args.optimized)):
        print(f"{label}: sustained 20-worker load, 100 health samples", flush=True)
        health = sustained_health(base)
        print(json.dumps(health, indent=2), flush=True)
        print(f"{label}: verifying PDF, Base64, shards and encryption", flush=True)
        cases = functional(base, optimized=label == "optimized")
        report[label] = dict(health=health, functional=cases)
        (args.output / "live-verification.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(f"{label}: functional checks passed", flush=True)
    report["sampled_full_images_match"] = (
        report["baseline"]["functional"]["raw_vs_base64"]["pdf"]["sampled_images"]
        == report["optimized"]["functional"]["raw_vs_base64"]["pdf"]["sampled_images"]
    )
    report["sampled_shard_images_match"] = (
        report["baseline"]["functional"]["shard_500_part_2"]["pdf"]["sampled_images"]
        == report["optimized"]["functional"]["shard_500_part_2"]["pdf"]["sampled_images"]
    )
    (args.output / "live-verification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key.startswith("sampled_")}, indent=2
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
