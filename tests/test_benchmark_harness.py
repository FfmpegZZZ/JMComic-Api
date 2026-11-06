from __future__ import annotations

import runpy

from fastapi.testclient import TestClient
from PIL import Image


def test_offline_harness_real_endpoints_and_reset(tmp_path, monkeypatch):
    from jmcomic_api import app as app_module
    from jmcomic_api.deps import settings
    from jmcomic_api.services import jm_client

    monkeypatch.setenv("BENCH_ROOT", str(tmp_path))
    monkeypatch.setenv("BENCH_VARIANT", "test")
    # Track import-time replacements so this isolated harness cannot leak into
    # other tests. Production app never imports the harness.
    for key in (
        "JMAPI_PDF_DIR",
        "JMAPI_PDF_SHARD_CACHE_DIR",
        "JMAPI_OPTION_FILE",
        "JMAPI_MIN_FREE_DISK_MB",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(app_module, "load_runtime", app_module.load_runtime)
    monkeypatch.setattr(
        app_module, "install_album_dirname_advice", app_module.install_album_dirname_advice
    )
    monkeypatch.setattr(jm_client, "download_with_retry", jm_client.download_with_retry)
    settings.cache_clear()
    folder = tmp_path / "images" / "[100]Fixed 100"
    folder.mkdir(parents=True)
    for index in range(3):
        Image.new("RGB", (20 + index, 30)).save(folder / f"{index:04}.jpg")
    (tmp_path / "transport-10.pdf").write_bytes(b"transport")
    namespace = runpy.run_path("scripts/benchmark/fixture_app.py")
    with TestClient(namespace["app"]) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get("/get_pdf/100?pdf=true&passwd=false")
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")
        assert client.get("/pdf/shard/100/1?pdf=true&passwd=false").status_code == 200
        assert client.get("/get_pdf/10?pdf=true").content == b"transport"
        assert client.get("/__benchmark/resources").json()["rss_bytes"] >= 0
        assert client.post("/__benchmark/reset?page_cache=true").status_code == 200
        assert not list((tmp_path / "test" / "pdf").rglob("*.pdf"))
    settings.cache_clear()
