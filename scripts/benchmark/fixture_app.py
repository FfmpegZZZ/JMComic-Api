"""Offline-only harness, mounted into comparison containers; never in runtime image.

Exercises the actual public PDF routes. Only upstream I/O is replaced. IDs 10
and 11 supply sparse 10/100 MiB files for transport memory comparisons.
"""

# ruff: noqa: E402
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(os.environ.get("BENCH_ROOT", "/data"))
ROOT.mkdir(parents=True, exist_ok=True)
os.environ.update(
    JMAPI_PDF_DIR=str(ROOT / os.environ.get("BENCH_VARIANT", "optimized") / "pdf"),
    JMAPI_PDF_SHARD_CACHE_DIR=str(ROOT / os.environ.get("BENCH_VARIANT", "optimized") / "shards"),
    JMAPI_OPTION_FILE=str(ROOT / "option.yml"),
    JMAPI_MIN_FREE_DISK_MB="0",
)
(ROOT / "option.yml").write_text("dir_rule:\n  base_dir: " + str(ROOT / "images") + "\n")
from jmcomic_api.services import jm_client
from jmcomic_api.services.album import PdfArtifact
from jmcomic_api.services.jm_client import JmRuntime

runtime = JmRuntime(
    opt=SimpleNamespace(dir_rule=SimpleNamespace(base_dir=ROOT / "images")), client=None
)


class OfflineResult:
    def __init__(self, album_id, paths):
        self.detail = SimpleNamespace(name=f"Fixed {album_id}", page_count=len(paths))
        self.manifest = SimpleNamespace(image_filepath_list=[str(p) for p in paths])

    def __iter__(self):
        yield self.detail
        yield None


async def download(album_id, opt, **kwargs):
    folder = ROOT / "images" / f"[{album_id}]Fixed {album_id}"
    paths = sorted(folder.glob("*.jpg"))
    if not paths:
        raise FileNotFoundError("Run prepare.py to create the fixed dataset")
    return OfflineResult(album_id, paths)


jm_client.download_with_retry = download
from jmcomic_api import app as app_module

app_module.load_runtime = lambda _: runtime
app_module.install_album_dirname_advice = lambda: None
app = app_module.create_app()
original_lifespan = app.router.lifespan_context
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    async with original_lifespan(app):
        service = app.state.album_service
        real_build = service.get_or_build

        async def build(album_id, *args, **kwargs):
            if album_id in ("10", "11"):
                return PdfArtifact(ROOT / f"transport-{album_id}.pdf", "中文传输测试.pdf")
            return await real_build(album_id, *args, **kwargs)

        service.get_or_build = build
        resources_path.parent.mkdir(parents=True, exist_ok=True)
        monitor = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("monitor.py")),
            str(resources_path),
        )
        try:
            yield
        finally:
            monitor.terminate()
            try:
                await asyncio.wait_for(monitor.wait(), 2)
            except TimeoutError:
                monitor.kill()
                await monitor.wait()


app.router.lifespan_context = lifespan


resources_path = ROOT / os.environ.get("BENCH_VARIANT", "optimized") / "resources.json"


@app.get("/__benchmark/resources", include_in_schema=False)
async def resources():
    try:
        return json.loads(resources_path.read_text())
    except (OSError, ValueError):
        return dict(rss_bytes=0, threads=0, peak_rss_bytes=0, peak_threads=0)


@app.post("/__benchmark/reset-metrics", include_in_schema=False)
async def reset_metrics():
    resources_path.with_suffix(".epoch").write_text(str(time.time_ns()))
    await asyncio.sleep(0.03)  # Let the independent monitor observe the epoch.
    return dict(success=True)


@app.post("/__benchmark/reset", include_in_schema=False)
async def reset(page_cache: bool = False):
    # Only fixed harness paths; use while no workload is active.
    for folder in (
        ROOT / os.environ.get("BENCH_VARIANT", "optimized") / "pdf",
        ROOT / os.environ.get("BENCH_VARIANT", "optimized") / "shards",
    ):
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if "pages" in path.parts and not page_cache:
                continue
            if path.suffix in (".pdf", ".json"):
                path.unlink()
    return dict(success=True)
