from decimal import Decimal
import asyncio
import json
from types import SimpleNamespace

import pytest

from gss_contracts.errors import DomainError
from gss_contracts import BillingMode, CheckoutRequest, Plan
from gss_checkout.adapter import UpstreamCheckoutAdapter
from gss_core.chatgpt import ChatGPTClient


BILLING = {"name": "Fixture Holder", "email": "fixture@example.invalid", "line1": "1 Fixture Street",
           "city": "Makati", "state": "NCR", "postal_code": "1226", "country": "PH"}


class Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class HTTP:
    def __init__(self, tax_response):
        self.headers = {}
        self.posts = []
        self.tax_response = tax_response

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/taxes"):
            return self.tax_response
        return Response({"checkout_session_id": "oaics_fixture", "publishable_key": "pk_test_fixture",
                         "processor_entity": "openai_llc", "amount_total": 100000, "currency": "PHP"})


def client(monkeypatch, tax_response):
    value = ChatGPTClient(access_token="fixture-token", account_id="fixture-account")
    value._http = HTTP(tax_response)
    monkeypatch.setattr(value, "prepare_risk", lambda **kwargs: None)
    monkeypatch.setattr(value, "_quote_from_context", lambda **kwargs: (Decimal("1000"), "PHP"))
    monkeypatch.setattr("gss_core.chatgpt.address_for", lambda *args: pytest.fail("Manual billing must not use a random address"))
    return value


def test_checkout_quotes_submitted_address_and_returns_only_upstream_total(monkeypatch):
    value = client(monkeypatch, Response({"amount_total": 123456, "currency": "PHP"}))
    created = value.create_checkout(plan="plus", country="PH", billing=BILLING)
    assert created.amount == Decimal("1234.56") and created.currency == "PHP"
    assert created.billing == BILLING
    assert len(value._http.posts) == 2
    route, call = value._http.posts[-1]
    assert route.endswith("/checkout/taxes")
    assert call["json"]["billing_address"] == {key: BILLING[key] for key in ("line1", "city", "state", "postal_code", "country")}
    assert call["json"]["billing_name"] == BILLING["name"]
    assert call["json"]["checkout_session_id"] == "oaics_fixture"
    assert not any("confirm" in route or "payment_methods" in route for route, _ in value._http.posts)


def test_tax_update_acknowledgment_reads_updated_checkout_quote(monkeypatch):
    value = client(monkeypatch, Response({"success": True}))
    quotes = iter(((Decimal("1000"), "PHP"), (Decimal("1234.56"), "PHP")))
    monkeypatch.setattr(value, "_quote_from_context", lambda **kwargs: next(quotes))
    created = value.create_checkout(plan="plus", country="PH", billing=BILLING)
    assert created.amount == Decimal("1234.56") and created.quote_error == ""


@pytest.mark.parametrize("response", [Response({}), Response({"error": "address rejected"}, 400)])
def test_quote_failure_preserves_created_checkout_without_a_guessed_amount(monkeypatch, response):
    value = client(monkeypatch, response)
    monkeypatch.setattr(value, "_quote_from_context", lambda **kwargs: (None, "PHP"))
    created = value.create_checkout(plan="plus", country="PH", billing=BILLING)
    assert created.checkout_session_id == "oaics_fixture"
    assert created.amount is None and created.quote_error
    assert created.billing == BILLING


def test_manual_quote_connection_failure_is_not_silently_ignored(monkeypatch):
    value = client(monkeypatch, Response({}))
    def failure(*args, **kwargs):
        raise TimeoutError("fixture timeout")
    monkeypatch.setattr(value._http, "post", failure)
    with pytest.raises(DomainError, match="报价失败"):
        value._quote_from_taxes(checkout_id="oaics_fixture", page_url="https://chatgpt.com/", currency="PHP", billing=BILLING, strict=True)


@pytest.mark.parametrize("amount", [Decimal("1234.56"), None])
def test_adapter_preserves_manual_billing_and_never_falls_back_to_list_price(monkeypatch, amount):
    request = CheckoutRequest(session="fixture", plan=Plan.PLUS, billing_country="PH",
                              billing_mode=BillingMode.STANDARD, billing=dict(BILLING))
    adapter = UpstreamCheckoutAdapter()
    monkeypatch.setattr("gss_checkout.adapter.align_proxy_region", lambda proxy, country: None)
    monkeypatch.setattr("gss_checkout.adapter.session_proxy", lambda *args: None)
    monkeypatch.setattr("gss_checkout.adapter.configured_proxy", lambda *args: None)
    def create(req, proxy):
        assert req.billing == BILLING
        return SimpleNamespace(checkout_url="https://chatgpt.com/checkout/fixture", currency="PHP", amount=amount,
                               checkout_session_id="oaics_fixture", publishable_key="pk_test_fixture",
                               customer_session_client_secret=None, checkout_kind="custom", confirm_return_url="",
                               billing=dict(req.billing))
    monkeypatch.setattr(adapter, "_create", create)
    monkeypatch.setattr("gss_checkout.adapter.address_for", lambda *args: pytest.fail("Address was replaced"))
    result = asyncio.run(adapter.create_checkout(request))
    assert result.billing == BILLING and result.amount == amount
    assert bool(result.quote_error) is (amount is None)
