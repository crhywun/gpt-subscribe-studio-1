import assert from "node:assert/strict";
import test from "node:test";
import { MantineProvider } from "@mantine/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  bindAndPayProtocol,
  createOrder,
  getBindAndPayProtocol,
  type BindAndPayProtocolInput,
  type BindAndPayProtocolResult,
  type Order,
} from "../src/api/client";
import { PROTOCOL_STAGE_LABELS, protocolBillingError, protocolCardError, protocolIsPaid, protocolPostReady, protocolStateLabel } from "../src/card-payment/protocolBind";
import { ProtocolPayPanel } from "../src/components/ProtocolPayPanel";
import { ResultPanel } from "../src/components/ResultPanel";
import { OrderNew } from "../src/pages/OrderNew";

const order: Order = {
  id: "fixture", plan: "plus", billing_country: "DE", status: "awaiting_payment",
  amount: "20", currency: "usd", checkout_url: "https://checkout.example.invalid/fixture",
};
const input: BindAndPayProtocolInput = {
  card: { number: "4242424242424242", cvc: "123", exp_month: "12", exp_year: "2030" }, // release-check: ignore
  billing: { name: "Fixture Holder", email: "fixture@example.invalid", line1: "1 Fixture Street", city: "Berlin", state: "BE", postal_code: "10115", country: "DE" },
  expected_amount: "20", expected_currency: "usd",
};

function renderPanel() {
  return renderToStaticMarkup(<MantineProvider><form><ProtocolPayPanel orderId={order.id}
    bindBeforePay country="DE" sessionEmail="fixture@example.invalid" amount="20" currency="usd"
    publishableKey="pk_test_fixture" checkoutUrl={order.checkout_url} pmMode="stripe" /></form></MantineProvider>);
}

test("Plus bindBeforePay renders server card inputs and never renders Stripe or saved-card components", () => {
  const html = renderPanel();
  assert.ok(html.includes('data-testid="plus-protocol-pay-panel"'));
  assert.ok(html.includes('data-testid="protocol-card-number"'));
  assert.ok(html.includes("协议支付"));
  assert.ok(html.includes("查询协议状态"));
  assert.ok(html.includes("沿用当前订单 checkout"));
  assert.ok(html.includes("confirmation_tokens"));
  assert.ok(html.includes("ChatGPT confirm"));
  assert.ok(html.includes("PaymentIntent confirm"));
  assert.ok(html.includes("GET checkout"));
  assert.ok(html.includes("付款确认后仍需等待订阅生效"));
  for (const marker of ["协议绑卡并支付", "绑卡验证", "先绑卡", "重新提链", "再次提链", "完成绑卡", "保存卡片成功"]) {
    assert.ok(!html.includes(marker), marker);
  }
  assert.ok(!html.includes("随机生成地址"));
  assert.ok(!html.includes('data-testid="random-billing-address"'));
  for (const marker of ["plus-saved-card-panel", "stripe-setup-element", "stripe-payment-element", "Stripe 输入框", "指定代理", "指纹注册", "solver"]) {
    assert.ok(!html.includes(marker), marker);
  }
  assert.ok(!html.includes(String(order.checkout_url)));
  assert.ok(!html.includes("<iframe"));
  assert.equal((html.match(/<form\b/g) || []).length, 1);
  assert.equal((html.match(/<button\b/g) || []).length, (html.match(/<button\b[^>]*type="button"/g) || []).length);
  assert.ok(!html.includes('type="submit"'));
});

test("Plus starts with empty card and real-address fields while prefiling only account email and country", () => {
  const html = renderPanel();
  assert.ok(html.includes('value="fixture@example.invalid"'));
  assert.ok(html.includes('value="DE"'));
  assert.ok((html.match(/value=""/g) || []).length >= 9);
  assert.ok(!html.includes("Fixture Holder"));
  assert.ok(!html.includes("1 Fixture Street"));
  assert.ok(!html.includes("4242424242424242")); // release-check: ignore
});

test("new Plus order configuration retains both protocol and Stripe input modes", () => {
  const html = renderToStaticMarkup(<MantineProvider><OrderNew onRecord={() => {}} /></MantineProvider>);
  assert.ok(html.includes("协议提交（服务端提交卡号）"));
  assert.ok(html.includes("Stripe 输入框（浏览器换 pm_）"));
  assert.ok(html.includes("账单地址（创建结账前填写）"));
  assert.ok(html.includes('data-testid="checkout-billing-form"'));
  assert.ok(html.includes("confirmation_tokens"));
  assert.ok(html.includes("ChatGPT confirm"));
  assert.ok(html.includes("PaymentIntent confirm"));
  assert.ok(html.includes("GET checkout"));
  for (const marker of ["协议绑卡并支付", "先绑卡", "重新提链", "再次提链", "服务端绑卡"]) {
    assert.ok(!html.includes(marker), marker);
  }
  assert.ok(!html.includes("plus-saved-card-panel"));
});

test("Plus payment retains the billing address used to obtain the quote", () => {
  const html = renderToStaticMarkup(<MantineProvider><ProtocolPayPanel orderId="manual-fixture"
    bindBeforePay country="PH" initialBilling={input.billing} amount="1234.56" currency="PHP" pmMode="server" /></MantineProvider>);
  assert.ok(html.includes('value="Fixture Holder"'));
  assert.ok(html.includes('value="1 Fixture Street"'));
  assert.ok(html.includes("本次报价使用创建结账时填写的地址"));
  assert.ok(!html.includes('data-testid="random-billing-address"'));
});

test("created checkout with unavailable quote remains visible and cannot submit payment", () => {
  const html = renderToStaticMarkup(<MantineProvider><ProtocolPayPanel orderId="quote-pending-fixture"
    bindBeforePay country="PH" initialBilling={input.billing} amount={null} currency="PHP" pmMode="server"
    quotationError="报价暂未取得" onRefreshQuote={async () => {}} /></MantineProvider>);
  assert.ok(html.includes('data-testid="plus-protocol-pay-panel"'));
  assert.ok(html.includes("报价暂未取得") && html.includes("刷新报价"));
  const paymentButton = (html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) || []).find((button) => button.includes("协议支付"));
  assert.match(paymentButton || "", /\bdisabled\b/);
});

test("creating checkout submits manual billing without submitting a card or payment", async () => {
  const originalFetch = globalThis.fetch;
  let body: Record<string, unknown> = {};
  globalThis.fetch = async (url, init) => {
    assert.equal(String(url), "/api/orders");
    body = JSON.parse(String(init?.body));
    return new Response(JSON.stringify(order), { status: 201 });
  };
  try {
    await createOrder({plan: "plus", billing_country: "PH", session: "fixture-session", billing: input.billing});
    assert.deepEqual(body.billing, input.billing);
    assert.equal(body.billing_country, "PH");
    assert.ok(!("card" in body) && !("expected_amount" in body));
  } finally { globalThis.fetch = originalFetch; }
});

test("Plus result pages retain actual action state without offering a browser checkout link", () => {
  const html = renderToStaticMarkup(<MantineProvider><ResultPanel protocolOnly order={{ ...order, status: "requires_action",
    next_action: { url: "https://verify.example.invalid/fixture", type: "use_stripe_sdk" } }} onReset={() => {}} /></MantineProvider>);
  assert.ok(!html.includes("https://verify.example.invalid/fixture"));
  assert.ok(!html.includes("打开支付验证页面"));
  assert.ok(!html.includes("开通成功"));
});

test("bind-and-pay POST sends the original card, real billing and explicitly confirmed quote only", async () => {
  const originalFetch = globalThis.fetch;
  const calls: { url: string; init?: RequestInit }[] = [];
  const expected: BindAndPayProtocolResult = { state: "challenge_required", stage: "setup", order: { ...order, status: "requires_action" }, next_action: { type: "use_stripe_sdk" } };
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(expected), { status: 200 });
  };
  try {
    const out = await bindAndPayProtocol(order.id, input);
    assert.deepEqual(out, expected);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/orders/fixture/bind-and-pay-protocol");
    assert.equal(calls[0].init?.method, "POST");
    assert.deepEqual(JSON.parse(String(calls[0].init?.body)), input);
    assert.deepEqual(Object.keys(JSON.parse(String(calls[0].init?.body))).sort(), ["billing", "card", "expected_amount", "expected_currency"]);
    assert.equal(out.order?.status, "requires_action");
  } finally { globalThis.fetch = originalFetch; }
});

test("protocol status uses a single GET without replaying card or payment submission", async () => {
  const originalFetch = globalThis.fetch;
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ state: "pending", stage: "checkout", order: { ...order, amount: "21" } }), { status: 200 });
  };
  try {
    const out = await getBindAndPayProtocol(order.id);
    assert.equal(out.order?.amount, "21");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/orders/fixture/bind-and-pay-protocol");
    assert.equal(calls[0].init?.method, undefined);
    assert.equal(calls[0].init?.body, undefined);
  } finally { globalThis.fetch = originalFetch; }
});

test("unknown POST transport results do not cause an automatic retry or an automatic GET", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (url) => { calls.push(String(url)); throw new Error("fixture connection lost"); };
  try {
    await assert.rejects(bindAndPayProtocol(order.id, input), /fixture connection lost/);
    assert.deepEqual(calls, ["/api/orders/fixture/bind-and-pay-protocol"]);
  } finally { globalThis.fetch = originalFetch; }
});

test("GET errors stay read-only and expose the server response", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls += 1;
    assert.equal(init?.method, undefined);
    assert.equal(init?.body, undefined);
    return new Response(JSON.stringify({ detail: "fixture status temporarily unavailable" }), { status: 503 });
  };
  try {
    await assert.rejects(getBindAndPayProtocol(order.id), /fixture status temporarily unavailable/);
    assert.equal(calls, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test("only an untouched order or an explicit checkout continuation is ready for a POST", () => {
  assert.equal(protocolPostReady(null), true);
  assert.equal(protocolPostReady({ state: "not_started", stage: "setup" }), true);
  assert.equal(protocolPostReady({ state: "pending", stage: "checkout" }), true);
  for (const state of ["unknown", "challenge_required", "requires_action", "processing", "saved", "succeeded", "paid", "card_declined", "integration_error", "request_rejected"]) {
    for (const stage of ["setup", "sync", "checkout", "payment"] as const) assert.equal(protocolPostReady({ state, stage }), false, `${state}/${stage}`);
  }
  assert.equal(protocolPostReady({ state: "pending", stage: "setup" }), false);
  assert.equal(protocolPostReady({ state: "pending", stage: "sync" }), false);
  assert.equal(protocolPostReady({ state: "pending", stage: "payment" }), false);
});

test("protocol setup keeps its API stage while displaying payment preparation", () => {
  assert.equal(PROTOCOL_STAGE_LABELS.setup, "支付方式准备");
  assert.equal(PROTOCOL_STAGE_LABELS.sync, "付款状态同步");
  assert.equal(PROTOCOL_STAGE_LABELS.checkout, "结账与金额确认");
  assert.equal(PROTOCOL_STAGE_LABELS.payment, "支付确认");
});

test("payment confirmation pending subscription activation is not paid or ready for replay", () => {
  const pending: BindAndPayProtocolResult = {
    state: "pending", stage: "payment", message: "付款已确认，等待订阅生效",
    intent_status: "succeeded",
    order: { ...order, status: "verifying", subscription: { active: false } },
  };
  assert.equal(protocolIsPaid(pending), false);
  assert.equal(protocolPostReady(pending), false);
  assert.equal(protocolStateLabel(pending.state), "pending");
  for (const state of ["unknown", "processing", "saved", "succeeded", "challenge_required"]) {
    assert.equal(protocolIsPaid({ ...pending, state }), false, state);
  }
  assert.equal(protocolIsPaid({ ...pending, state: "paid" }), true);
  const html = renderToStaticMarkup(<MantineProvider><ResultPanel protocolOnly
    order={pending.order!} onReset={() => {}} /></MantineProvider>);
  assert.ok(!html.includes("开通成功"));
  assert.ok(!html.includes("订阅已生效"));
});

test("payment pending status preserves the protocol shape and performs one read-only GET", async () => {
  const originalFetch = globalThis.fetch;
  const calls: { url: string; init?: RequestInit }[] = [];
  const expected: BindAndPayProtocolResult = {
    state: "pending", stage: "payment", message: "付款已确认，等待订阅生效",
    order: { ...order, status: "verifying", subscription: { active: false } }, next_action: null,
  };
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(expected), { status: 200 });
  };
  try {
    const out = await getBindAndPayProtocol(order.id);
    assert.deepEqual(out, expected);
    assert.equal(protocolIsPaid(out), false);
    assert.equal(protocolPostReady(out), false);
    assert.deepEqual(calls, [{ url: "/api/orders/fixture/bind-and-pay-protocol", init: undefined }]);
  } finally { globalThis.fetch = originalFetch; }
});

test("US and CA real billing require both state and postal code", () => {
  for (const country of ["US", "CA"]) {
    assert.ok(protocolBillingError({ ...input.billing, country, state: "", postal_code: "" }));
    assert.ok(protocolBillingError({ ...input.billing, country, state: "CA", postal_code: "" }));
    assert.ok(protocolBillingError({ ...input.billing, country, state: "", postal_code: "90210" }));
    assert.equal(protocolBillingError({ ...input.billing, country, state: "CA", postal_code: "90210" }), null);
  }
  assert.equal(protocolBillingError(input.billing), null);
});

test("card validation rejects invalid months and partial years before an API call", () => {
  assert.equal(protocolCardError(input.card), null);
  assert.ok(protocolCardError({ ...input.card, exp_month: "00" }));
  assert.ok(protocolCardError({ ...input.card, exp_month: "13" }));
  assert.ok(protocolCardError({ ...input.card, exp_year: "203" }));
});


test("Stripe input mode remains available without the server raw-card form", () => {
  const html = renderToStaticMarkup(<MantineProvider><form><ProtocolPayPanel orderId={order.id}
    browserPayment country="DE" sessionEmail="fixture@example.invalid" amount="20" currency="usd"
    publishableKey="pk_test_fixture" checkoutUrl={order.checkout_url} pmMode="stripe" /></form></MantineProvider>);
  assert.ok(html.includes("输入框模式"));
  assert.ok(!html.includes("plus-protocol-pay-panel"));
  assert.ok(!html.includes('data-testid="protocol-card-number"'));
  assert.ok(!html.includes("协议支付"));
  assert.ok(!html.includes("指定代理"));
  assert.ok(!html.includes("默认从五个免税州"));
  assert.ok(html.includes('value="DE"'));
  assert.ok(html.includes("随机生成地址"));
  assert.ok(html.includes('data-testid="random-billing-address"'));
  assert.ok(html.includes("账单国家独立于订单地区"));
  assert.ok(!html.includes("readonly"));
  assert.ok(html.includes("确认支付"));
});

test("Stripe input result retains the normal verification link", () => {
  const html = renderToStaticMarkup(<MantineProvider><ResultPanel order={{ ...order, status: "requires_action",
    next_action: { url: "https://verify.example.invalid/fixture", type: "use_stripe_sdk" } }} onReset={() => {}} /></MantineProvider>);
  assert.ok(html.includes("https://verify.example.invalid/fixture"));
  assert.ok(html.includes("打开支付验证页面"));
  assert.ok(!html.includes("开通成功"));
});


test("explicit integration rejection stays distinct from unknown and blocks payment replay", () => {
  assert.equal(protocolStateLabel("integration_error"), "商户集成接口未启用");
  assert.equal(protocolStateLabel("request_rejected"), "Stripe 已明确拒绝当前请求");
  assert.equal(protocolStateLabel("unknown"), "unknown");
  assert.equal(protocolPostReady({ state: "integration_error", stage: "setup" }), false);
  assert.equal(protocolPostReady({ state: "request_rejected", stage: "setup" }), false);
});
