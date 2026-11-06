from __future__ import annotations

import concurrent.futures

import pytest
import yaml
from curl_cffi.requests import Session

from jmcomic_api.services.jm_client import load_runtime


@pytest.mark.parametrize("explicit", [None, "curl_cffi", "curl_cffi_session"])
def test_default_session_reuse_explicit_override_and_transport_options(tmp_path, explicit):
    config = dict(
        log=False,
        client=dict(
            impl="api",
            domain=["example.invalid"],
            postman=dict(
                meta_data=dict(
                    cookies={"test": "offline"},
                    proxies={"https": "http://127.0.0.1:7890"},
                    timeout=7,
                ),
            ),
        ),
    )
    if explicit:
        config["client"]["postman"]["type"] = explicit
    path = tmp_path / "option.yml"
    path.write_text(yaml.safe_dump(config))
    runtime = load_runtime(path)
    try:
        expected = explicit or "curl_cffi_session"
        assert runtime.opt.client.postman.src_dict["type"] == expected
        downloads = runtime.opt.build_jm_client()
        assert downloads is runtime.opt.build_jm_client()
        assert downloads is not runtime.client
        assert runtime.client.retry_times == 1
        assert runtime.client.domain_retry_strategy is downloads.domain_retry_strategy
        if expected == "curl_cffi_session":
            session = runtime.client.postman.session
            assert isinstance(session, Session)
            assert session is not downloads.postman.session
            assert session.cookies.get("test") == "offline"
            assert session.proxies["https"] == "http://127.0.0.1:7890"
            assert session.timeout == 7
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(lambda: session.curl).result()
                second = pool.submit(lambda: session.curl).result()
                assert first is second
                assert first is not session.curl  # thread-local handles
    finally:
        runtime.retire()


def test_option_filepath_is_preserved_for_plugins(tmp_path):
    path = tmp_path / "option.yml"
    path.write_text(
        "log: false\nclient:\n  domain: [example.invalid]\n  postman:\n    meta_data:\n      cookies: {test: offline}\n"
    )
    runtime = load_runtime(path)
    try:
        assert runtime.opt.filepath == str(path)
    finally:
        runtime.retire()


def test_explicit_retry_count_and_yaml_timeout_pair_are_preserved(tmp_path):
    path = tmp_path / "option.yml"
    path.write_text(
        "log: false\nclient:\n  retry_times: 4\n  domain: [example.invalid]\n"
        "  postman:\n    meta_data:\n      timeout: [2, 8]\n      cookies: {test: offline}\n"
    )
    runtime = load_runtime(path)
    try:
        assert runtime.client.retry_times == 4
        assert runtime.client.postman.session.timeout == (2, 8)
    finally:
        runtime.retire()
