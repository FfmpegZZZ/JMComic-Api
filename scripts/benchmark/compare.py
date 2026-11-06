"""Run from host: python3 scripts/benchmark/compare.py --output benchmark-results.

Only standard library required. Discards streamed response bodies so client
memory does not become part of the measurement. No real upstream requests.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import math
import statistics
import threading
import time
import urllib.request
from pathlib import Path


def request(url, method="GET", *, collect=True):
    with urllib.request.urlopen(
        urllib.request.Request(url, method=method), timeout=600
    ) as response:
        if collect:
            return json.load(response)
        size = 0
        while chunk := response.read(192 * 1024):
            size += len(chunk)
        return size


def p95(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def measured(base, path):
    previous_epoch = request(base + "/__benchmark/resources").get("epoch")
    request(base + "/__benchmark/reset-metrics", "POST")
    deadline = time.monotonic() + 10
    while True:
        resource = request(base + "/__benchmark/resources")
        epoch = resource.get("epoch")
        if epoch is not None and epoch != previous_epoch:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Resource monitor did not acknowledge the new measurement epoch")
        time.sleep(0.01)
    samples = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            with contextlib.suppress(OSError):
                sample = request(base + "/__benchmark/resources")
                if sample.get("epoch") == epoch:
                    samples.append(sample)
            stop.wait(0.02)

    probe = threading.Thread(target=sampler)
    probe.start()
    start = time.perf_counter()
    try:
        size = request(base + path, collect=False)
    finally:
        elapsed = time.perf_counter() - start
        stop.set()
        probe.join()
        final_sample = request(base + "/__benchmark/resources")
        if final_sample.get("epoch") != epoch:
            raise RuntimeError("Concurrent benchmark run reset the resource monitor")
        samples.append(final_sample)
    return dict(
        seconds=elapsed,
        response_bytes=size,
        peak_rss_bytes=max((s["peak_rss_bytes"] for s in samples), default=0),
        peak_threads=max((s["peak_threads"] for s in samples), default=0),
    )


def aggregate(runs):
    return dict(
        median_seconds=statistics.median(r["seconds"] for r in runs),
        p95_seconds=p95([r["seconds"] for r in runs]),
        peak_rss_bytes=max(r["peak_rss_bytes"] for r in runs),
        peak_threads=max(r["peak_threads"] for r in runs),
        response_bytes=runs[-1]["response_bytes"],
        runs=runs,
    )


def compare(base, repeats):
    request(base + "/health/ready")
    report = dict(base_url=base, repeats=repeats, datasets={})
    for pages in (100, 500):
        path = f"/get_pdf/{pages}?passwd=false&pdf=true&quality=70"
        request(base + "/__benchmark/reset?page_cache=true", "POST")
        warmup = measured(base, path)
        runs = []
        for _ in range(repeats):
            request(base + "/__benchmark/reset", "POST")
            runs.append(measured(base, path))
        hot = [measured(base, path) for _ in range(repeats)]
        shard = measured(base, f"/pdf/shard/{pages}/1?passwd=false&pdf=true&quality=70")
        report["datasets"][str(pages)] = dict(
            cold=warmup, warm_encoding=aggregate(runs), hot_pdf=aggregate(hot), shard=shard
        )
    memory = {}
    # Measure the small response before warming the large one, to avoid
    # contaminating its baseline with allocator retention from a larger load.
    for album_id, label in (("10", "10MiB"), ("11", "100MiB")):
        measured(base, f"/get_pdf/{album_id}?passwd=false")
        memory[label] = aggregate(
            [measured(base, f"/get_pdf/{album_id}?passwd=false") for _ in range(repeats)]
        )
    report["base64"] = memory
    report["base64_peak_increment_bytes"] = (
        memory["100MiB"]["peak_rss_bytes"] - memory["10MiB"]["peak_rss_bytes"]
    )
    report["base64_memory_pass"] = report["base64_peak_increment_bytes"] <= 16 * 1024**2
    latencies = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [
            pool.submit(request, base + "/get_pdf/10?passwd=false", collect=False)
            for _ in range(20)
        ]
        while not all(future.done() for future in futures):
            start = time.perf_counter()
            request(base + "/health/live")
            latencies.append(time.perf_counter() - start)
            time.sleep(0.02)
        for future in futures:
            future.result()
    report["health_under_20_requests"] = dict(
        samples=len(latencies), p95_seconds=p95(latencies), pass_target=p95(latencies) <= 0.2
    )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="http://127.0.0.1:18700")
    parser.add_argument("--optimized", default="http://127.0.0.1:18701")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results"))
    args = parser.parse_args()
    if args.repeats < 5:
        parser.error("At least five repetitions are required")
    args.output.mkdir(parents=True, exist_ok=True)
    for name, base in (("baseline", args.baseline), ("optimized", args.optimized)):
        print(f"Measuring {name} at {base}", flush=True)
        report = compare(base, args.repeats)
        (args.output / f"{name}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
