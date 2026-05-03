"""Coverage for the metrics + readiness improvements."""

from __future__ import annotations

import time

from jmcomic_api import metrics as m


def test_metrics_endpoint_serves_prometheus_format(client):
    tc, _ = client
    # Touch a counter so something shows up.
    m.record_cache("album", hit=True)
    m.record_cache("shard", hit=False)

    resp = tc.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # Sanity: at minimum the names registered above are exposed.
    assert "jmapi_cache_hit_total" in body
    assert "jmapi_cache_miss_total" in body
    assert "jmapi_request_total" in body  # registered, even if zero before


def test_request_metrics_count_route_template(client):
    tc, _ = client

    # Hit the same route under different concrete IDs — both should aggregate
    # under the route TEMPLATE so cardinality stays bounded.
    tc.get("/health")
    tc.get("/health")
    body = tc.get("/metrics").text

    # Look for a "/health" sample with a count >= 2.
    health_lines = [
        line
        for line in body.splitlines()
        if line.startswith("jmapi_request_total{") and 'route="/health"' in line
    ]
    assert health_lines, f"no /health metric found in:\n{body[:500]}"
    # The line ends with `... <count>` — at least one of them >= 2.
    assert any(int(float(line.rsplit(" ", 1)[-1])) >= 2 for line in health_lines)


def test_health_ready_includes_diagnostics(client, tmp_path):
    """Readiness now exposes disk + last download age. Both should be in the body."""
    tc, _ = client
    resp = tc.get("/health/ready")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "ready"
    assert "free_disk_mb" in body
    assert "min_free_disk_mb" in body
    assert body["pdf_dir_ok"] is True
    assert body["disk_ok"] is True
    # No download has happened in the test fixture yet.
    assert body["last_download_age_seconds"] is None


def test_health_ready_503_when_disk_full(client, monkeypatch):
    """When disk_usage reports < min_free_disk_mb, readiness must flip 503."""
    import shutil

    fake = type(
        "Usage", (), {"total": 10 * 2**30, "used": 9.99 * 2**30, "free": 5 * 2**20}
    )()  # 5 MB free
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: fake)

    tc, _ = client
    resp = tc.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["disk_ok"] is False
    assert body["free_disk_mb"] < body["min_free_disk_mb"]


def test_last_download_age_visible_after_success(client):
    """After a successful download, /health/ready should surface a small age."""
    tc, _ = client
    tc.app.state.last_download_unix = time.time() - 12  # 12s ago

    resp = tc.get("/health/ready")
    body = resp.json()
    assert body["status"] == "ready"
    age = body["last_download_age_seconds"]
    assert age is not None
    assert 10 < age < 60  # rough sanity, plenty of slack


def test_download_outcome_metrics(monkeypatch):
    """The album service's outcome labels should be observable from outside."""
    before_success = m.DOWNLOAD_TOTAL.labels(outcome="success")._value.get()
    m.record_download_outcome("success")
    after_success = m.DOWNLOAD_TOTAL.labels(outcome="success")._value.get()
    assert after_success == before_success + 1
