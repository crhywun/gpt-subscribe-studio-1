import json
import subprocess
from types import SimpleNamespace

import pytest

from gss_core.sentinel import last_failure, mint


def call():
    return mint(flow="fixture", device_id="fixture", user_agent="fixture")


@pytest.mark.parametrize("error,reason", [(FileNotFoundError(), "node_missing"),
                                         (subprocess.TimeoutExpired("node", 1), "node_timeout")])
def test_runtime_failures_are_distinguished(monkeypatch, error, reason):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr("gss_core.sentinel.subprocess.run", run)
    assert call() == {}
    assert last_failure()["reason"] == reason


def test_bridge_failure_diagnostic_does_not_log_proxy_password_cookies_or_raw_output(monkeypatch, caplog):
    proxy = "socks5h://fixture-user:p%40ssword@proxy.example.invalid:32000"
    error = "proto2: request failed " + proxy + " cookie-secret p@ssword"
    output = {"error": error, "raw": "private-driver-content"}
    monkeypatch.setattr("gss_core.sentinel.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps(output).encode(), stderr=b"", returncode=1))
    assert mint(flow="fixture", device_id="fixture", user_agent="fixture", proxy=proxy,
                cookie_header="fixture=cookie-secret") == {}
    assert last_failure()["reason"] == "bridge_error"
    for private in ("fixture-user", "p%40ssword", "p@ssword", "cookie-secret", "private-driver-content"):
        assert private not in caplog.text and private not in str(last_failure())
    assert "proto2" in caplog.text


def test_success_keeps_original_token_and_clears_failure(monkeypatch):
    token = {"p": "fixture-proof", "c": "fixture-challenge", "id": "fixture", "flow": "fixture"}
    output = {"main": json.dumps(token), "so": json.dumps({"so": "fixture-secondary"})}
    monkeypatch.setattr("gss_core.sentinel.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps(output).encode(), stderr=b"", returncode=0))
    result = call()
    assert {key: result[key] for key in token} == token
    assert result["_so"] == {"so": "fixture-secondary"}
    assert last_failure() == {}
