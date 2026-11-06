"""Run the offline comparison on a Linux Docker host, including cgroup CPU samples."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from itertools import pairwise
from pathlib import Path

import compare


def cgroup(container):
    pid = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container], text=True
    ).strip()
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("cgroup v2 is required for resource measurement")


def cpu_usage(root):
    return (
        int(
            dict(line.split() for line in (root / "cpu.stat").read_text().splitlines())[
                "usage_usec"
            ]
        )
        / 1e6
    )


class Resources:
    def __init__(self, root):
        self.root = root
        self.samples = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample)

    def sample(self):
        while not self.stop.is_set():
            self.samples.append(
                (
                    time.monotonic(),
                    cpu_usage(self.root),
                    int((self.root / "memory.current").read_text()),
                )
            )
            self.stop.wait(0.1)

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop.set()
        self.thread.join()
        samples = self.samples
        if len(samples) < 2:
            return {}
        cpu = samples[-1][1] - samples[0][1]
        elapsed = samples[-1][0] - samples[0][0]
        return dict(
            cpu_seconds=cpu,
            elapsed_seconds=elapsed,
            average_cores=cpu / elapsed,
            peak_cores=max((b[1] - a[1]) / (b[0] - a[0]) for a, b in pairwise(samples)),
            peak_cgroup_memory_bytes=max(s[2] for s in samples),
            sample_interval_seconds=0.1,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="jmapi-arm")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    original = compare.measured
    for variant, port in (("baseline", 18700), ("optimized", 18701)):
        root = cgroup(f"{args.project}-{variant}-1")

        def measured(base, path, root=root):
            monitor = Resources(root)
            monitor.start()
            try:
                result = original(base, path)
            finally:
                resources = monitor.finish()
            result["cgroup"] = resources
            print(json.dumps(dict(path=path, **result)), flush=True)
            return result

        compare.measured = measured
        report = compare.compare(f"http://127.0.0.1:{port}", 5)
        (args.output / f"{variant}.json").write_text(json.dumps(report, indent=2))
        print(f"Completed {variant}", flush=True)


if __name__ == "__main__":
    main()
