from fastapi.testclient import TestClient
import pytest
from decimal import Decimal
from types import SimpleNamespace

from gss_api.main import create_app
from gss_api.store import store
from gss_contracts.errors import DomainError, ErrorCode
from gss_contracts import CheckoutResult, Plan


BILLING = {"name": "Fixture Holder", "email": "fixture@example.invalid", "line1": "1 Fixture Street",
           "city": "Makati", "state": "NCR", "postal_code": "1226", "country": "PH"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(tmp_path / "proxy.json"))
    store.clear()
    with TestClient(create_app()) as value:
        yield value
    store.clear()


def test_create_order_retains_manual_billing_through_pipeline(client):
    billing = {**BILLING, "name": " Fixture Holder ", "country": "ph"}
    response = client.post("/api/orders", json={"session": '{"accessToken":"fixture"}', "plan": "plus",
                                               "billing_country": "PH", "billing": billing})
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["billing"] == BILLING
    assert order["billing_country"] == "PH" and order["currency"] == "PHP"
    assert order["status"] == "awaiting_payment"
    assert client.get('/api/orders/' + order['id']).json()["billing"] == BILLING


@pytest.mark.parametrize("billing", [{**BILLING, "line1": "   "}, {**BILLING, "country": "ZZ"},
                                     {**BILLING, "country": "US", "state": ""}])
def test_invalid_manual_address_does_not_create_order(client, billing):
    response = client.post("/api/orders", json={"session": '{"accessToken":"fixture"}', "plan": "plus",
                                               "billing_country": "PH", "billing": billing})
    assert response.status_code == 400
    assert store.orders == {}


def test_created_checkout_with_missing_quote_still_enters_payment_page(client, monkeypatch):
    async def checkout(req):
        return CheckoutResult(checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_fixture",
                              checkout_session_id="oaics_fixture", plan=Plan.PLUS, currency="PHP", amount=None,
                              billing=req.billing, quote_error="报价暂未取得")
    monkeypatch.setattr("gss_checkout.adapter.FakeCheckoutAdapter.create_checkout", lambda self, req: checkout(req))
    response = client.post("/api/orders", json={"session": '{"accessToken":"fixture"}', "plan": "plus",
                                               "billing_country": "PH", "billing": BILLING})
    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "awaiting_payment"
    assert order["checkout_url"] and order["amount"] is None
    assert order["quote_error"] == "报价暂未取得" and order["billing"] == BILLING


@pytest.mark.parametrize("fail", [False, True])
def test_refresh_quote_reuses_checkout_and_billing_without_payment(client, monkeypatch, fail):
    order = client.post("/api/orders", json={"session": '{"accessToken":"fixture"}', "plan": "plus",
                                            "billing_country": "PH", "billing": BILLING}).json()
    from gss_core.vault import vault
    secrets = vault.get(order["id"])
    calls = []
    class QuoteClient:
        def _quote_from_taxes(self, **kwargs):
            assert kwargs["checkout_id"] == secrets.checkout_session_id
            assert kwargs["billing"] == BILLING and kwargs["strict"] is True
            calls.append("quote")
            if fail:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "报价待重试")
            return Decimal("1234.56"), "PHP"
        def close(self):
            calls.append("closed")
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setattr("gss_api.routers.orders.ChatGPTClient.from_raw_session", lambda *args, **kwargs: QuoteClient())
    response = client.post(f'/api/orders/{order["id"]}/quote')
    assert response.status_code == 200
    updated = response.json()
    assert updated["status"] == "awaiting_payment" and updated["checkout_url"] == order["checkout_url"]
    assert updated["amount"] == (None if fail else "1234.56")
    assert bool(updated["quote_error"]) is fail
    assert calls == ["quote", "closed"]


def test_refresh_quote_cannot_run_after_payment_processing_started(client, monkeypatch):
    order = client.post("/api/orders", json={"session": '{"accessToken":"fixture"}', "plan": "plus",
                                            "billing_country": "PH", "billing": BILLING}).json()
    from gss_core.vault import vault
    vault.get(order["id"]).saved_card_context = {"protocol_flow": True}
    response = client.post(f'/api/orders/{order["id"]}/quote')
    assert response.status_code == 409
