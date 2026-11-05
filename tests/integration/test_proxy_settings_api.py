from __future__ import annotations

from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from gss_api.main import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(tmp_path / "proxy-gateways.json"))
    return TestClient(create_app())


RAW = "http://acct-region-US-sid-AAAA1111:topsecret@gw.example.com:3010"


def test_parse_previews_without_saving(client: TestClient) -> None:
    resp = client.post("/api/settings/proxies/parse", json={"raw": RAW})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["host"] == "gw.example.com" and body["region"] == "US" and body["sid"] == "AAAA1111"
    assert body["has_region_param"] is True
    assert "topsecret" not in resp.text
    assert client.get("/api/settings/proxies").json()["checkout"]["host"] == ""


def test_save_applies_to_both_stages_and_hides_password(client: TestClient) -> None:
    resp = client.put("/api/settings/proxies", json={"raw": RAW, "enabled": True})
    assert resp.status_code == 200, resp.text
    assert "topsecret" not in resp.text
    saved = client.get("/api/settings/proxies").json()
    for role in ("checkout", "approve"):
        assert saved[role]["host"] == "gw.example.com"
        assert saved[role]["enabled"] is True
        assert saved[role]["has_password"] is True
    assert "topsecret" not in client.get("/api/settings/proxies").text


def test_save_replaces_previous_region_instead_of_keeping_it(client: TestClient) -> None:
    client.put("/api/settings/proxies", json={"raw": RAW})
    client.put("/api/settings/proxies", json={"raw": "http://acct-region-JP-sid-BBBB2222:pw@gw.example.com:3010"})
    saved = client.get("/api/settings/proxies").json()["checkout"]
    assert (saved["region"], saved["sid"]) == ("JP", "BBBB2222")


def test_bad_input_is_400(client: TestClient) -> None:
    assert client.post("/api/settings/proxies/parse", json={"raw": "nonsense"}).status_code == 400
    assert client.put("/api/settings/proxies", json={"raw": ""}).status_code == 400


def test_123proxy_country_and_session_round_trip(client: TestClient) -> None:
    raw = "socks5h://account-sess_a8F3kP9xQ2mL_15%2Bph:topsecret@residential.123proxy.cn:32000"
    preview = client.post("/api/settings/proxies/parse", json={"raw": raw})
    assert preview.status_code == 200
    assert preview.json()["routing_format"] == "123proxy"
    assert preview.json()["has_region_param"] and preview.json()["has_sid_param"]
    assert "topsecret" not in preview.text and "account" not in preview.text
    response = client.put("/api/settings/proxies", json={"raw": raw, "enabled": True})
    assert response.status_code == 200
    for saved in client.get("/api/settings/proxies").json().values():
        assert (saved["scheme"], saved["region"], saved["sid"]) == ("socks5h", "PH", "a8F3kP9xQ2mL")
        assert saved["enabled"] is True and saved["routing_format"] == "123proxy"


def test_proxy_timeout_explains_protocol_prefix(client: TestClient, monkeypatch) -> None:
    client.put("/api/settings/proxies", json={"raw": RAW})

    async def timeout(_proxy_url):
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr("gss_api.routers.proxies.probe_gateway", timeout)
    response = client.post("/api/settings/proxies/checkout/test")
    assert response.status_code == 502
    assert "socks5h://" in response.json()["detail"] and "topsecret" not in response.text
