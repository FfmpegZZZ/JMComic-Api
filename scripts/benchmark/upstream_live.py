"""Measure real upstream queries through deployed APIs; never logs response bodies or credentials."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from arm_runner import Resources, cgroup
from compare import p95


def fetch(url, *, timeout=45):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
            return dict(
                seconds=time.perf_counter() - started,
                status=response.status,
                success=data.get("success", True),
            )
    except urllib.error.HTTPError as error:
        error.close()
        return dict(seconds=time.perf_counter() - started, status=error.code, success=False)
    except (OSError, ValueError) as error:
        return dict(
            seconds=time.perf_counter() - started,
            status=0,
            success=False,
            error_type=type(error).__name__,
        )


def upstream_counts(base):
    with urllib.request.urlopen(base + "/metrics", timeout=5) as response:
        lines = response.read().decode().splitlines()
    counts = {}
    for line in lines:
        if line.startswith("jmapi_upstream_requests_total{"):
            counts[line.split()[0]] = float(line.split()[1])
    return counts


def wave(base, paths, container):
    before = upstream_counts(base)
    monitor = Resources(cgroup(container))
    barrier = threading.Barrier(len(paths))
    health = []

    def worker(path):
        barrier.wait(timeout=10)
        return fetch(base + path)

    monitor.start()
    started = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as executor:
            futures = [executor.submit(worker, path) for path in paths]
            while True:
                health.append(fetch(base + "/health/live", timeout=5))
                if all(future.done() for future in futures):
                    break
                time.sleep(0.02)
            results = [future.result() for future in futures]
            elapsed = time.perf_counter() - started
    finally:
        resource = monitor.finish()
    after = upstream_counts(base)
    return dict(
        seconds=elapsed,
        requests=results,
        status_counts=dict(Counter(r["status"] for r in results)),
        health=health,
        resources=resource,
        upstream_delta={key: value - before.get(key, 0) for key, value in after.items()},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--album", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(
        album_id=args.album,
        repeats=5,
        ttl_seconds=0,
        note="Same machine; alternate variant order each repetition; anonymous; no proxy.",
        cases={},
        preflight={},
    )
    variants = [("baseline", 18710), ("optimized", 18711)]
    detail = "/album/" + args.album
    category = "/categories?time=week&category=single&order_by=latest&page="
    for name, port in variants:
        result = fetch(f"http://127.0.0.1:{port}" + detail)
        report["preflight"][name] = result
        if not result["success"]:
            args.output.write_text(json.dumps(report, indent=2))
            raise RuntimeError("Upstream preflight failed; see status in report")
    cases = [
        ("detail_single", [detail]),
        ("search_single", ["/search?query=test&page=1"]),
        ("categories_single", [category + "1"]),
        ("same_detail_8", [detail] * 8),
        ("same_detail_20", [detail] * 20),
        ("distinct_categories_8", [category + str(page) for page in range(1, 9)]),
        ("distinct_categories_20", [category + str(page) for page in range(1, 21)]),
    ]
    for case, paths in cases:
        report["cases"][case] = {name: [] for name, _ in variants}
        for repeat in range(5):
            for name, port in variants if repeat % 2 == 0 else variants[::-1]:
                result = wave(f"http://127.0.0.1:{port}", paths, f"jmapi-arm-live-{name}-1")
                report["cases"][case][name].append(result)
                args.output.write_text(json.dumps(report, indent=2))
                print(
                    json.dumps(
                        dict(
                            case=case,
                            repeat=repeat,
                            variant=name,
                            seconds=result["seconds"],
                            status=result["status_counts"],
                            upstream=result["upstream_delta"],
                        )
                    ),
                    flush=True,
                )
                if any(
                    'outcome="rate_limited"' in key and value > 0
                    for key, value in result["upstream_delta"].items()
                ):
                    raise RuntimeError("Upstream rate limited; stopped instead of increasing load")
                if any(not r["success"] for r in result["requests"]):
                    raise RuntimeError("Upstream errors; stopped instead of increasing load")
                time.sleep(0.5)
        for name, _ in variants:
            runs = report["cases"][case][name]
            durations = [r["seconds"] for r in runs]
            latencies = [q["seconds"] for r in runs for q in r["requests"]]
            print(
                json.dumps(
                    dict(
                        case=case,
                        variant=name,
                        median_batch=statistics.median(durations),
                        request_p95=p95(latencies),
                    )
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
