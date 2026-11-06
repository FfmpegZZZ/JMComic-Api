"""Read-only real account validation. Reports omit credentials, tokens and profile values."""

from __future__ import annotations

import argparse
import json
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from upstream_live import upstream_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(credentials_loaded=True, steps={}, changes="No favorite or attendance writes")
    token = None
    base = "http://127.0.0.1:18720"
    phase = "ready"

    def save():
        args.output.write_text(json.dumps(report, indent=2))

    def call(path, *, method="GET", body=None, expected=200, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated and token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data, status = json.load(response), response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            data = {}
        step = dict(status=status, seconds=time.perf_counter() - started)
        report["steps"][phase] = step
        save()
        if status != expected:
            raise RuntimeError("Unexpected status; response body intentionally omitted")
        return data

    save()
    try:
        call("/health/ready", authenticated=False)
        phase = "login"
        result = call("/auth/login", method="POST", body=credentials, authenticated=False)
        data = result["data"]
        token = data["access_token"]
        uid = str(data["profile"]["uid"])
        assert data["profile"]["username"].casefold() == credentials["username"].casefold()
        assert "s" not in data["profile"] and "password" not in data["profile"]
        report["steps"][phase]["identity_matches"] = True
        previous = data["fetched_at"]
        for index in range(2):
            phase = f"fresh_profile_{index + 1}"
            before = upstream_counts(base)
            result = call("/auth/me")["data"]
            after = upstream_counts(base)
            assert str(result["profile"]["uid"]) == uid and result["fetched_at"] > previous
            previous = result["fetched_at"]
            report["steps"][phase].update(
                refreshed=True,
                profile_fields=sorted(result["profile"]),
                upstream_http_attempts=sum(after.values()) - sum(before.values()),
            )
        phase = "favorites"
        favorites = call("/me/favorites?page=1")["data"]
        assert isinstance(favorites["results"], list) and isinstance(favorites["folders"], list)
        report["steps"][phase]["valid_shape"] = True
        phase = "daily_status"
        daily = call("/me/daily")["data"]
        assert isinstance(daily, dict)
        report["steps"][phase]["valid_shape"] = True
        records = list(args.state.glob("*.session"))
        assert len(records) == 1
        raw = records[0].read_bytes()
        assert all(value.encode() not in raw for value in (*credentials.values(), token))
        assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
        report["encrypted_storage"] = dict(plaintext_absent=True, mode="0600")
        phase = "restart"
        started = time.perf_counter()
        subprocess.run(
            ["docker", "restart", "--time", "40", "jmapi-arm-login-auth-1"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(base + "/health/ready", timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Account API did not become ready after restart") from None
                time.sleep(0.2)
        report["steps"][phase] = dict(success=True, seconds=time.perf_counter() - started)
        phase = "restored_cookie"
        call("/me/daily")
        phase = "restored_password_profile"
        restored = call("/auth/me")["data"]
        assert str(restored["profile"]["uid"]) == uid and restored["fetched_at"] > previous
        report["steps"][phase]["refreshed"] = True
        phase = "anonymous_personal_rejected"
        call("/auth/me", authenticated=False, expected=401)
        phase = "authenticated_catalog"
        call("/album/114514")
        phase = "anonymous_catalog"
        call("/album/114514", authenticated=False)
        report["passed"] = True
    except Exception as error:
        report["failure"] = dict(phase=phase, error_type=type(error).__name__)
        report["passed"] = False
    finally:
        if token:
            try:
                phase = "logout"
                call("/auth/session", method="DELETE")
                phase = "revoked_token_rejected"
                call("/auth/me", expected=401)
                assert not list(args.state.glob("*.session"))
                report["test_session_removed"] = True
            except Exception as error:
                report["cleanup_failure"] = dict(error_type=type(error).__name__)
                report["passed"] = False
        save()
        print(json.dumps(report), flush=True)
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
