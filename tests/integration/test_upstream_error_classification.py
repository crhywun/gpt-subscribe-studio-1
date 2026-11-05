from types import SimpleNamespace

import httpx
import pytest
from curl_cffi.requests.exceptions import ImpersonateError
from fastapi.testclient import TestClient

from gss_api.main import create_app


def _query_with_error(monkeypatch, error):
    class Account:
        async def promo_eligibility(self, _session):
            raise error

    monkeypatch.setattr("gss_api.routers.promo.build_pipeline", lambda: SimpleNamespace(deps=SimpleNamespace(account=Account())))
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        return client.post("/api/promo/eligibility", json={"session": "test-session"})


def test_unsupported_browser_is_not_reported_as_proxy_disconnect(monkeypatch):
    response = _query_with_error(monkeypatch, ImpersonateError("Impersonating chrome999 is not supported"))
    assert response.status_code == 500
    assert response.json()["code"] == "http_client_configuration_error"
    assert "浏览器版本" in response.json()["detail"]
    assert "住宅代理断连" not in response.text and "test-session" not in response.text


def test_transport_failure_is_reported_without_claiming_proxy_disconnect(monkeypatch):
    response = _query_with_error(monkeypatch, httpx.ConnectError("network unavailable"))
    assert response.status_code == 503
    assert "连接上游服务" in response.json()["detail"]
    assert "住宅代理断连" not in response.text


def test_upstream_http_status_is_not_reported_as_transport_failure(monkeypatch):
    request = httpx.Request("GET", "https://example.com/check")
    upstream = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError("upstream response", request=request, response=upstream)
    response = _query_with_error(monkeypatch, error)
    assert response.status_code == 500
    assert "代理" not in response.text
