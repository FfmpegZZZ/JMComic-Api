"""Exercise startup ordering without a Docker daemon or registry access."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def comparison_project(tmp_path):
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "project with spaces"
    (project / "scripts" / "benchmark").mkdir(parents=True)
    for relative in (
        "Dockerfile",
        "scripts/start-comparison.sh",
        "scripts/build-comparison.sh",
        "scripts/benchmark/Dockerfile",
    ):
        shutil.copyfile(source / relative, project / relative)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['BENCH_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'build' and os.environ.get('DOCKER_BUILDKIT') == '0':\n"
        "    dockerfile = pathlib.Path(args[args.index('-f') + 1] if '-f' in args else 'Dockerfile')\n"
        "    if any(line.startswith('RUN --mount=') for line in dockerfile.read_text().splitlines()):\n"
        "        print('the --mount option requires BuildKit', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "if args[0] == 'build' and args[args.index('-t') + 1] == os.environ.get('BENCH_TEST_FAIL_IMAGE'):\n"
        "    print('injected build failure', file=sys.stderr)\n"
        "    sys.exit(17)\n"
    )
    docker.chmod(0o755)
    git = binaries / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        "import io, sys, tarfile\n"
        "if sys.argv[1] == 'archive':\n"
        "    target = sys.argv[2].split('=', 1)[1]\n"
        "    with tarfile.open(target, 'w') as archive:\n"
        "        data = b'baseline source\\n'\n"
        "        entry = tarfile.TarInfo('README.md')\n"
        "        entry.size = len(data)\n"
        "        archive.addfile(entry, io.BytesIO(data))\n"
    )
    git.chmod(0o755)
    log = tmp_path / "docker.jsonl"
    environment = dict(
        os.environ,
        PATH=f"{binaries}{os.pathsep}{os.environ['PATH']}",
        BENCH_TEST_LOG=str(log),
        DOCKER_BUILDKIT="0",
    )
    return project, log, environment


def test_startup_builds_both_images_before_prepare_and_start(comparison_project, tmp_path):
    project, log, environment = comparison_project
    result = subprocess.run(
        ["sh", str(project / "scripts/start-comparison.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    builds = [call for call in calls if call[0] == "build"]
    assert [call[call.index("-t") + 1] for call in builds] == [
        "jmcomic-api:bench-baseline",
        "jmcomic-api:bench-optimized",
    ]
    compose = [call for call in calls if call[0] == "compose"]
    assert len(compose) == 2
    assert "run" in compose[0] and compose[0][-1] == "prepare"
    assert "up" in compose[1] and compose[1][-2:] == ["baseline", "optimized"]
    assert all(call[call.index("--pull") + 1] == "never" for call in compose)
    assert calls.index(builds[-1]) < calls.index(compose[0])
    assert (project / ".benchmark/baseline/README.md").read_text() == "baseline source\n"
    assert "18700" in result.stdout and "18701" in result.stdout


@pytest.mark.parametrize("image", ["jmcomic-api:bench-baseline", "jmcomic-api:bench-optimized"])
def test_build_failure_stops_startup_and_preserves_first_error(comparison_project, image):
    project, log, environment = comparison_project
    environment["BENCH_TEST_FAIL_IMAGE"] = image
    result = subprocess.run(
        ["sh", str(project / "scripts/start-comparison.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 17
    assert "injected build failure" in result.stderr
    assert "Comparison build failed" in result.stderr
    assert "first error above" in result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(call[0] == "compose" for call in calls)
    assert calls[-1][0] == "build"


def test_full_build_uses_classic_builder_compatible_files_for_all_images(comparison_project):
    project, log, environment = comparison_project
    result = subprocess.run(
        ["sh", str(project / "scripts/build-comparison.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    builds = [call for call in calls if call[0] == "build"]
    assert [call[call.index("-t") + 1] for call in builds] == [
        "jmcomic-api:bench-baseline",
        "jmcomic-api:bench-optimized",
        "jmcomic-api:test-py312",
        "jmcomic-api:test-py313",
        "jmcomic-api:optimized",
    ]
