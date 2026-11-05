"""Current single-checkout protocol with fake transport only, no recorded secrets."""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import sys
import threading
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.chatgpt import APP_BASE, STRIPE_BASE, ChatGPTClient, expected_amount_minor
from gss_core.vault import vault


@pytest.fixture(autouse=True)
def _legacy_engine(monkeypatch):
    """This module pins the pre-HAR checkout_v2 contract on purpose.

    The HAR-faithful engine (default since 2026-10-30) is covered by
    tests/unit/test_plus_har_protocol.py; here the legacy path must keep
    behaving exactly as before.
    """
    monkeypatch.setenv("GSS_PLUS_ENGINE", "legacy")


@pytest.fixture
def flow(monkeypatch):
    state = {"calls": [], "violations": [], "account": "account_fixture", "minor": 1363,
             "page_status": "open", "payment_status": "unpaid", "pi_status": "requires_confirmation",
             "after_confirm": "succeeded", "active": True, "closed": 0, "created": 0}
    body = {"card": {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2040"},
            "billing": {"name": "Fixture Holder", "email": "fixture@example.test", "line1": "Fixture Street",
                        "city": "Portland", "state": "OR", "postal_code": "97211", "country": "US"},
            "expected_amount": "1363", "expected_currency": "JPY"}
    checkout_id = "oaics_direct_fixture"
    def record(name, **details):
        state["calls"].append({"name": name, **details})
    def block(*args, **kwargs):
        state["violations"].append("real network attempt")
        raise AssertionError("offline test attempted network")
    socket_connect = socket.socket.connect
    def guard_connect(connection, address):
        # Windows asyncio implements its internal wakeup socketpair over loopback TCP.
        # HTTP/curl transports remain blocked below, as do all external socket connects.
        if sys.platform == "win32" and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return socket_connect(connection, address)
        return block(connection, address)
    monkeypatch.setattr(socket, "create_connection", block)
    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    from curl_cffi import requests
    monkeypatch.setattr(requests.Session, "request", block)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", block)
    def response(data, status=200):
        return SimpleNamespace(status_code=status, text=json.dumps(data), json=lambda: copy.deepcopy(data))
    def page():
        return {"checkout_session_id": state.get("response_checkout_id", checkout_id),
                "publishable_key": state.get("response_key", "pk_test_direct"), "account_id": state.get("response_account", state["account"]),
                "customer_session_client_secret": state.get("response_cuss", "cuss_fixture"),
                "status": state["page_status"], "payment_status": state["payment_status"],
                "billing_details": {"country": "US", "currency": state.get("response_currency", "JPY")},
                "checkout_state": {"currency": state.get("response_currency", "JPY"),
                    "total": {"subtotal": {"minorUnitsAmount": 2727}, "discount": {"minorUnitsAmount": 1364},
                              "total": {"minorUnitsAmount": state["minor"]}}},
                "client_secret": "pi_direct_fixture_secret_fixture" if state["page_status"] == "complete" else None}
    def intent():
        return {"id": state.get("pi_response_id", "pi_direct_fixture"), "status": state["pi_status"],
                "customer": state.get("pi_customer", "cus_fixture"), "payment_method": state.get("pi_pm", "pm_fixture"),
                "amount": state.get("pi_amount", state["minor"]), "currency": state.get("pi_currency", "jpy"),
                "next_action": {"type": "use_stripe_sdk", "use_stripe_sdk": {"secret": "not-returned-fixture"}},
                "last_payment_error": {"code": "card_declined", "decline_code": "insufficient_funds", "message": "Fixture insufficient funds"}}
    class Transport:
        def __init__(self, service): self.service = service
        def get(self, url, **kwargs):
            if self.service == "app" and url == APP_BASE + "/backend-api/payments/checkout/openai_llc/" + checkout_id:
                record("context_get")
                return response(page())
            if self.service == "stripe" and url == STRIPE_BASE + "/v1/elements/sessions":
                record("elements_get", params=kwargs["params"])
                assert kwargs["params"]["customer_session_client_secret"] == "cuss_fixture"
                assert kwargs["params"]["deferred_intent[amount]"] == str(state["minor"])
                return response({"customer": "cus_fixture"})
            if self.service == "stripe" and url == STRIPE_BASE + "/v1/payment_intents/pi_direct_fixture":
                record("pi_get")
                assert kwargs["params"]["client_secret"] == "pi_direct_fixture_secret_fixture"
                return response(intent())
            if self.service == "app" and url == APP_BASE + "/backend-api/subscriptions":
                record("subscription_get")
                return response({"plan_type": state.get("subscription_plan", "plus"), "is_active": state["active"],
                    "entitlement": {"has_active_subscription": state["active"], "subscription_plan": "chatgptplusplan"}})
            state["violations"].append("unexpected GET " + url)
            raise AssertionError(url)
        def post(self, url, **kwargs):
            if self.service == "app" and url == APP_BASE + "/backend-api/payments/checkout/taxes":
                record("taxes", body=kwargs["json"])
                assert kwargs["json"]["checkout_session_id"] == checkout_id
                assert kwargs["json"]["billing_country"] == "US"
                assert kwargs["json"]["currency"].lower() == "jpy"
                value = page()
                if state.get("missing_total"): value["checkout_state"]["total"].pop("total")
                return response({"checkout_session": value}, state.get("tax_http", 200))
            if self.service == "app" and url == APP_BASE + "/backend-api/payments/checkout/update_tax_id":
                record("update_tax_id")
                assert kwargs["json"] == {"checkout_session_id": checkout_id, "tax_id_info": None}
                return response({"success": True})
            if self.service == "stripe" and url == STRIPE_BASE + "/v1/confirmation_tokens":
                form = {key: values[0] for key, values in parse_qs(kwargs["data"], keep_blank_values=True).items()}
                record("token_create", body=form)
                assert form["key"] == "pk_test_direct"
                assert form["payment_method_data[card][number]"] == body["card"]["number"]
                assert form["payment_method_data[billing_details][address][country]"] == "US"
                assert not any("hcaptcha" in k or "user_agent" in k or "time_on_page" in k for k in form)
                if state.get("token_entered"):
                    state["token_entered"].set()
                    assert state["token_release"].wait(5)
                if state.get("token_lost"): raise TimeoutError("fixture token lost")
                if state.get("token_error"):
                    return response({"error": state["token_error"]}, state.get("token_http", 400))
                return response({"id": "ctoken_fixture"})
            if self.service == "app" and url == APP_BASE + "/backend-api/payments/checkout/confirm":
                record("app_confirm", body=kwargs["json"])
                assert kwargs["json"] == {"checkout_session_id": checkout_id, "confirm_token": "ctoken_fixture",
                    "save_payment_method_for_reuse": True, "selected_payment_method_type": "card"}
                assert type(kwargs["json"]["save_payment_method_for_reuse"]) is bool
                if state.get("app_lost"): raise TimeoutError("fixture app confirm lost")
                if state.get("legacy_success"): return response({"status": "success"})
                return response({"status": "success", "type": "payment_intent", "client_secret": "pi_direct_fixture_secret_fixture",
                                 "confirm_return_url": state.get("return_url", APP_BASE + "/checkout/verify?checkout_session_id=" + checkout_id)})
            if self.service == "stripe" and url == STRIPE_BASE + "/v1/payment_intents/pi_direct_fixture/confirm":
                form = {key: values[0] for key, values in parse_qs(kwargs["data"], keep_blank_values=True).items()}
                record("pi_confirm", body=form)
                assert form["confirmation_token"] == "ctoken_fixture"
                assert "payment_method" not in form
                assert form["client_secret"] == "pi_direct_fixture_secret_fixture"
                state["pi_status"] = state["after_confirm"]
                if state["pi_status"] == "succeeded" and not state.get("page_delayed"):
                    state.update(page_status="complete", payment_status="paid")
                if state.get("pi_lost"): raise TimeoutError("fixture PI response lost")
                return response(intent())
            if self.service == "app" and url == APP_BASE + "/backend-api/payments/checkout_session":
                record("ack", body=kwargs["json"])
                assert kwargs["json"] == {"processor_entity": "openai_llc", "stripe_checkout_session_id": checkout_id}
                if state.get("ack_lost"): raise TimeoutError("fixture acknowledgment lost")
                return response({"success": True, "created_new_subscription": False})
            state["violations"].append("unexpected POST " + url)
            raise AssertionError(url)
    class Client(ChatGPTClient):
        def __init__(self):
            state["created"] += 1
            self.account_id = state["account"]
            self.access_token = "fixture-access"
            self.session_token = ""
            self._http = Transport("app")
            self._stripe_http = Transport("stripe")
            self.timeout = 1
        def refresh_session(self): return False
        def hydrate(self): pass
        def _route_headers(self, route, **kwargs): return {}
        def close(self):
            state["closed"] += 1
            if state.get("worker_done"): state["worker_done"].set()
    monkeypatch.setattr(ChatGPTClient, "from_raw_session", staticmethod(lambda raw, proxy=None: Client()))
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    with TestClient(create_app()) as api:
        created = api.post("/api/orders", json={"plan": "plus", "billing_country": "JP", "session": "fixture-session"})
        assert created.status_code == 201, created.text
        oid = created.json()["id"]
        view = store.view(oid)
        view.checkout_session_id = checkout_id
        view.checkout_url = APP_BASE + "/checkout/openai_llc/" + checkout_id
        view.publishable_key = "pk_test_direct"
        view.customer_session_client_secret = "cuss_fixture"
        view.amount = "1363"
        view.currency = "JPY"
        view.confirm_return_url = APP_BASE + "/checkout/verify?checkout_session_id=" + checkout_id
        secret = vault.get(oid)
        secret.account_id = "account_fixture"
        secret.checkout_session_id = checkout_id
        secret.publishable_key = "pk_test_direct"
        monkeypatch.setenv("GSS_USE_FAKE", "0")
        yield SimpleNamespace(api=api, root=f"/api/orders/{oid}/bind-and-pay-protocol", oid=oid, body=body, state=state)
        vault.drop(oid)
        store.clear()
    assert not state["violations"], state["violations"]
    assert state["created"] == state["closed"]


def post(flow, body=None):
    result = flow.api.post(flow.root, json=body or flow.body)
    assert result.status_code == 200, result.text
    return result.json()


def get(flow):
    result = flow.api.get(flow.root)
    assert result.status_code == 200, result.text
    return result.json()


def names(flow): return [c["name"] for c in flow.state["calls"]]


def test_unused_get_is_local_and_has_no_writes(flow):
    assert get(flow)["state"] == "not_started"
    assert not flow.state["calls"]


def test_direct_checkout_uses_one_token_and_original_context_then_delivers(flow):
    result = post(flow)
    assert result["state"] == "paid"
    assert result["order"]["status"] == "delivered"
    assert result["order"]["billing_country"] == "JP"
    assert result["order"]["currency"] == "JPY"
    assert result["order"]["amount"] == "1363"
    assert names(flow) == ["context_get", "taxes", "update_tax_id", "elements_get", "token_create", "app_confirm", "context_get", "pi_get", "pi_confirm", "context_get", "ack", "subscription_get"]
    before = list(flow.state["calls"])
    assert get(flow) == post(flow) == result
    assert flow.state["calls"] == before
    assert vault.get(flow.oid) is None


def test_changed_tax_quote_requires_user_confirmation_and_no_card_write(flow):
    store.view(flow.oid).amount = "1499"
    flow.body["expected_amount"] = "1499"
    result = post(flow)
    assert (result["state"], result["stage"], result["order"]["amount"]) == ("pending", "checkout", "1363")
    assert "token_create" not in names(flow)
    before = names(flow).count("taxes")
    assert get(flow)["stage"] == "checkout"
    assert names(flow).count("taxes") == before
    flow.body["expected_amount"] = "1363"
    assert post(flow)["state"] == "paid"


@pytest.mark.parametrize("change", ["missing_total", "tax_http", "response_currency", "response_checkout_id", "response_key", "response_account", "response_cuss"])
def test_invalid_checkout_or_quote_never_submits_card(flow, change):
    flow.state[change] = {"missing_total": True, "tax_http": 500, "response_currency": "USD", "response_checkout_id": "oaics_other",
                          "response_key": "pk_test_other", "response_account": "account_other", "response_cuss": "cuss_other"}[change]
    result = post(flow)
    assert result["state"] == "unknown"
    assert "token_create" not in names(flow)
    assert "app_confirm" not in names(flow)


@pytest.mark.parametrize("lost", ["token_lost", "app_lost"])
def test_unknown_financial_response_is_read_only_and_not_replayed(flow, lost):
    flow.state[lost] = True
    assert post(flow)["state"] == "unknown"
    before = {n: names(flow).count(n) for n in ["taxes", "token_create", "app_confirm", "pi_confirm", "ack"]}
    assert get(flow)["state"] == post(flow)["state"] == "unknown"
    assert before == {n: names(flow).count(n) for n in before}


@pytest.mark.parametrize("status,state", [(400, "integration_error"), (403, "integration_error"), (402, "card_declined"), (500, "unknown")])
def test_real_token_rejection_is_exposed_and_never_replayed(flow, status, state):
    flow.state.update(token_http=status, token_error={"code": "card_declined", "decline_code": "insufficient_funds",
        "message": "This integration surface is unsupported for publishable key tokenization" if status in {400,403} else "Fixture insufficient funds"})
    result = post(flow)
    assert result["state"] == state
    assert "app_confirm" not in names(flow)
    assert get(flow)["state"] == post(flow)["state"] == state
    assert names(flow).count("token_create") == 1


@pytest.mark.parametrize("status,state", [("requires_action", "challenge_required"), ("processing", "pending"), ("requires_payment_method", "card_declined")])
def test_pi_status_never_becomes_fake_success(flow, status, state):
    flow.state["after_confirm"] = status
    result = post(flow)
    assert result["state"] == state
    assert result["order"]["status"] != "delivered"
    if status == "requires_payment_method": assert result["decline_code"] == "insufficient_funds"
    if status == "requires_action": assert result["next_action"] == {"type": "use_stripe_sdk"}
    before = names(flow).count("pi_confirm")
    get(flow)
    post(flow)
    assert names(flow).count("pi_confirm") == before
    assert "ack" not in names(flow)


@pytest.mark.parametrize("mismatch", ["pi_response_id", "pi_currency", "pi_amount"])
def test_foreign_intent_or_amount_is_not_confirmed(flow, mismatch):
    flow.state[mismatch] = {"pi_response_id": "pi_other", "pi_currency": "usd", "pi_amount": 9999}[mismatch]
    result = post(flow)
    assert result["state"] == "unknown"
    assert "pi_confirm" not in names(flow)


def test_app_success_without_intent_is_not_a_payment(flow):
    flow.state["legacy_success"] = True
    assert post(flow)["state"] == "unknown"
    assert "pi_confirm" not in names(flow)
    assert get(flow)["state"] == "unknown"
    assert names(flow).count("app_confirm") == 1


def test_return_url_must_be_the_live_merchant_url(flow):
    flow.state["return_url"] = "https://untrusted.example/return"
    assert post(flow)["state"] == "unknown"
    assert "pi_confirm" not in names(flow)


def test_paid_context_without_active_subscription_stays_pending(flow):
    flow.state["active"] = False
    result = post(flow)
    assert (result["state"], result["stage"]) == ("pending", "payment")
    assert result["order"]["status"] != "delivered"
    assert vault.get(flow.oid) is not None
    flow.state["active"] = True
    assert get(flow)["state"] == "paid"
    assert names(flow).count("ack") == names(flow).count("pi_confirm") == 1


def test_get_does_not_acknowledge_delayed_paid_context(flow):
    flow.state["page_delayed"] = True
    assert post(flow)["state"] == "pending"
    flow.state.update(page_status="complete", payment_status="paid")
    result = get(flow)
    assert (result["state"], result["stage"]) == ("pending", "checkout")
    assert "ack" not in names(flow)
    assert post(flow)["state"] == "paid"
    assert names(flow).count("pi_confirm") == names(flow).count("app_confirm") == names(flow).count("ack") == 1


def test_lost_ack_is_reconciled_without_duplicate_ack(flow):
    flow.state.update(ack_lost=True, active=False)
    assert post(flow)["state"] == "unknown"
    flow.state["active"] = True
    assert get(flow)["state"] == "paid"
    assert names(flow).count("ack") == 1


def test_lost_stripe_response_only_reconciles_actual_context(flow):
    flow.state["pi_lost"] = True
    assert post(flow)["state"] == "paid"
    assert names(flow).count("pi_confirm") == 1


def test_legacy_setup_context_cannot_silently_switch(flow):
    vault.get(flow.oid).saved_card_context = {"protocol_flow": True, "state": "requires_action"}
    assert flow.api.post(flow.root,json=flow.body).status_code == 409
    assert flow.api.get(flow.root).status_code == 409
    assert not names(flow)


@pytest.mark.parametrize("suffix,method", [("/saved-card/prepare","post"),("/saved-card","get"),("/verify","post"),("/pay","post"),("/pay-protocol","post")])
def test_legacy_routes_block_current_pending_flow(flow,suffix,method):
    flow.state["after_confirm"] = "processing"
    assert post(flow)["state"] == "pending"
    body = {"payment_method_token":"pm_fixture"} if suffix == "/pay" else {"payment_method_id":"pm_fixture","billing":flow.body["billing"]} if suffix == "/pay-protocol" else None
    before = list(flow.state["calls"])
    response = getattr(flow.api,method)(f"/api/orders/{flow.oid}"+suffix,**({"json":body} if body else {}))
    assert response.status_code == 409, response.text
    assert flow.state["calls"] == before


def test_changed_details_rejected_and_sensitive_fields_not_cached(flow):
    flow.state["after_confirm"] = "processing"
    result = post(flow)
    changed = copy.deepcopy(flow.body)
    changed["card"]["cvc"] = "456"
    assert flow.api.post(flow.root,json=changed).status_code == 409
    secret = vault.get(flow.oid)
    for payload in [secret.saved_card_context,vars(secret),result,vars(store.view(flow.oid))]:
        text = json.dumps(payload,default=str)
        assert flow.body["card"]["number"] not in text
        assert '"cvc"' not in text
        assert '"number"' not in text


def test_fake_mode_prevents_upstream_payment(flow,monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE","1")
    assert flow.api.post(flow.root,json=flow.body).status_code == 409
    assert not names(flow)


def test_canceled_request_keeps_worker_serialized(flow):
    flow.state.update(token_entered=threading.Event(), token_release=threading.Event(), worker_done=threading.Event())
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=flow.api.app),base_url="http://testserver") as client:
            original = asyncio.create_task(client.post(flow.root,json=flow.body))
            try:
                assert await asyncio.to_thread(flow.state["token_entered"].wait,3)
                original.cancel()
                with pytest.raises(asyncio.CancelledError): await original
                retried = await client.post(flow.root,json=flow.body)
                assert retried.status_code == 200
                assert retried.json()["intent_status"] == "processing"
                assert names(flow).count("token_create") == 1
                assert "app_confirm" not in names(flow)
            finally:
                flow.state["token_release"].set()
                assert await asyncio.to_thread(flow.state["worker_done"].wait,3)
            checked = await client.get(flow.root)
            assert checked.json()["state"] in {"paid","pending"}
    asyncio.run(exercise())
    assert names(flow).count("token_create") == names(flow).count("app_confirm") == names(flow).count("pi_confirm") == 1


@pytest.mark.parametrize("minor", [0, 1363, 1499])
def test_final_checkout_total_precedes_subtotal_and_preserves_zero(minor):
    payload = {"checkout_session":{"checkout_state":{"total":{"subtotal":{"minorUnitsAmount":2727},"total":{"minorUnitsAmount":minor}}}}}
    assert expected_amount_minor(payload) == minor


@pytest.mark.parametrize("paid", [False, True])
def test_already_completed_checkout_never_creates_a_second_financial_attempt(flow, paid):
    flow.state.update(page_status="complete", payment_status="paid" if paid else "unpaid")
    result = post(flow)
    assert result["state"] == ("paid" if paid else "unknown")
    assert "token_create" not in names(flow)
    assert "app_confirm" not in names(flow)
    assert "pi_confirm" not in names(flow)


def test_expired_checkout_is_not_submitted_again(flow):
    flow.state["page_status"] = "expired"
    assert post(flow)["state"] == "request_rejected"
    assert "token_create" not in names(flow)


@pytest.mark.parametrize("minor", [0, 1363])
def test_conflicting_currency_fields_never_become_a_valid_quote(minor):
    from gss_contracts.errors import DomainError
    from gss_core.checkout_protocol import _checkout_quote
    payload = {"billing_details":{"currency":"USD"},"checkout_state":{"currency":"JPY","total":{"total":{"minorUnitsAmount":minor}}}}
    with pytest.raises(DomainError):
        _checkout_quote(payload, "JPY")
