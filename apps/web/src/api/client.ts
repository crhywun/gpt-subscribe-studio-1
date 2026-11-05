export type Plan = "plus" | "go" | "pro_lite" | "pro" | "pro_max" | "team";

export interface Order {
  id: string;
  status: string;
  plan: string;
  billing_country: string;
  checkout_url?: string | null;
  publishable_key?: string | null;
  customer_session_client_secret?: string | null;
  amount?: string | null;
  currency?: string | null;
  billing?: {
    email?: string;
    name?: string;
    line1?: string;
    city?: string;
    state?: string;
    postal_code?: string;
    country?: string;
  } | null;
  last_error?: string | null;
  quote_error?: string | null;
  next_action?: { type?: string; url?: string | null; client_secret?: string | null; challenge_type?: string; checkout_url?: string } | null;
  subscription?: { active: boolean; expires_at?: string | null; channel?: string | null } | null;
  checkout_kind?: string | null;
  seat_quantity?: number | null;
  workspace_name?: string | null;
  promo_code?: string | null;
  promo_campaign_id?: string | null;
  price_interval?: string | null;
  confirm_return_url?: string | null;
  /** 从 accessToken 解析出的账户邮箱 */
  session_email?: string | null;
}

export type SavedCardState =
  | "not_started"
  | "requires_payment_method"
  | "requires_confirmation"
  | "requires_action"
  | "processing"
  | "canceled"
  | "sync_pending"
  | "saved"
  | "checkout_ready"
  | "prepare_unknown";

export interface SavedCardSummary {
  brand: string;
  last4: string;
}

export interface SavedCardStatus {
  state: SavedCardState;
  setup_status?: string | null;
  payment_method_id?: string | null;
  card?: SavedCardSummary | null;
  message?: string | null;
}

export interface SavedCardPreparation {
  state: string;
  setup_intent_id: string;
  client_secret: string;
  publishable_key: string;
}

export interface SavedCardCheckout {
  order: Order;
  payment_method_id: string;
  card?: SavedCardSummary | null;
}

const jsonHeaders = { "Content-Type": "application/json" };

async function readError(res: Response): Promise<string> {
  const detail = await res.json().catch(() => ({}));
  return detail?.detail?.message ?? detail?.detail ?? `请求失败（${res.status}）`;
}

export async function createOrder(input: {
  plan: Plan;
  billing_country: string;
  session: string;
  promo_code?: string;
  promo_campaign_id?: string;
  workspace_name?: string;
  seat_quantity?: number;
  price_interval?: "month" | "year";
  billing?: BillingInput;
}): Promise<Order> {
  const res = await fetch("/api/orders", {
    method: "POST",
    headers: { ...jsonHeaders, "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      plan: input.plan,
      billing_country: input.billing_country,
      session: input.session,
      promo_code: input.promo_code || "",
      promo_campaign_id: input.promo_campaign_id || "",
      workspace_name: input.workspace_name || "",
      seat_quantity: input.seat_quantity,
      price_interval: input.price_interval || "month",
      billing: input.billing,
      mode: {
        account_source: "customer_session",
        card_source: "customer",
        billing_mode: input.promo_campaign_id ? "promo" : "standard",
        risk_acknowledged: Boolean(input.promo_campaign_id),
      },
    }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Order;
}

export interface PromoCampaign {
  key: string;
  campaign_id: string;
  discount_percentage?: number | null;
  duration_num_periods?: number | null;
  duration_period?: string | null;
  title?: string | null;
  summary?: string | null;
  promotion_type?: string | null;
}

export interface PromoEligibility {
  account_id?: string | null;
  current_plan_type?: string | null;
  subscription_plan?: string | null;
  is_free: boolean;
  plus_eligible: boolean;
  campaigns: PromoCampaign[];
}

export async function checkPromoEligibility(session: string): Promise<PromoEligibility> {
  const res = await fetch("/api/promo/eligibility", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ session }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as PromoEligibility;
}

export interface BillingInput {
  email?: string;
  name: string;
  line1: string;
  city: string;
  state: string;
  postal_code: string;
  country: string;
}

export interface TeamHostedContext {
  context_id: string;
  session_id: string;
  publishable_key: string;
  eid: string;
  amount_minor: number;
  currency: string;
  checkout_config_id: string;
  elements_session_id: string;
  client_session_id: string;
  guid: string;
  muid: string;
  sid: string;
  customer_email?: string;
  passive_captcha?: { site_key?: string; rqdata?: string | null; token_timeout_seconds?: number };
}

async function fetchWithDeadline(url: string, init: RequestInit, ms: number, timeoutMessage: string): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, ms);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    // AbortError alone is ambiguous (network cancel vs deadline); report the deadline explicitly.
    if (timedOut) throw new Error(timeoutMessage);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export async function refreshCheckoutQuote(orderId: string): Promise<Order> {
  const res = await fetch(`/api/orders/${orderId}/quote`, { method: "POST", headers: jsonHeaders });
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export async function quoteTeam(orderId: string, billing?: BillingInput): Promise<Order> {
  const res = await fetchWithDeadline(`/api/orders/${orderId}/team/quote`, {
    method: "POST", headers: jsonHeaders,
    body: JSON.stringify({ billing, browser_locale: navigator.language || "en-US",
      browser_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" }),
  }, 25000, "读取 Stripe 报价超时；本步骤未提交付款确认，可稍后点击刷新应付金额");
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export async function prepareTeam(
  orderId: string, billing: BillingInput,
  options: { passiveCaptchaToken?: string; timeoutMs?: number } = {},
): Promise<{ order: Order; context: TeamHostedContext }> {
  const res = await fetchWithDeadline(`/api/orders/${orderId}/team/prepare`, {
    method: "POST", headers: jsonHeaders,
    body: JSON.stringify({ billing, browser_locale: navigator.language || "en-US",
      browser_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" }),
  }, options.timeoutMs ?? 40000, "Stripe 支付准备超时；本步骤未提交付款确认，可稍后重试");
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export async function pollTeam(orderId: string): Promise<Order> {
  const res = await fetch(`/api/orders/${orderId}/team/poll`, { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export interface TeamCardPayload {
  number: string;
  cvc: string;
  exp_month: string;
  exp_year: string;
}

export interface TeamConfirmOptions {
  team_context_id: string;
  passive_captcha_token?: string;
  rv_timestamp?: string;
  card?: TeamCardPayload;
}

export async function payOrder(
  orderId: string,
  paymentMethodToken: string,
  billing?: BillingInput,
  team?: TeamConfirmOptions,
): Promise<Order> {
  const res = await fetch(`/api/orders/${orderId}/pay`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ payment_method_token: paymentMethodToken, billing, ...team }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Order;
}

export async function verifyOrder(orderId: string, paid = false): Promise<Order> {
  const res = await fetch(`/api/orders/${orderId}/verify${paid ? "?paid=true" : ""}`, { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Order;
}

export interface ProtocolPayResult {
  state: string;
  code?: string | null;
  decline_code?: string | null;
  message?: string | null;
  intent_status?: string | null;
  charge_id?: string | null;
  payment_method_id?: string | null;
  stage?: "setup" | "sync" | "checkout" | "payment";
  order?: Order;
  next_action?: { type?: string } | null;
}

export interface BindAndPayProtocolInput {
  card: TeamCardPayload;
  billing: BillingInput;
  expected_amount: string;
  expected_currency: string;
}

export interface BindAndPayProtocolResult extends ProtocolPayResult {
  stage: "setup" | "sync" | "checkout" | "payment";
  upstream_message?: string;
  upstream_status?: number;
  upstream_code?: string;
  remediation?: string;
  documentation_url?: string;
}

export interface ProtocolPayInput {
  /** 服务端直接提交卡号（全协议）。与 payment_method_id 二选一。 */
  card?: { number: string; cvc: string; exp_month: string; exp_year: string };
  /** 浏览器 Stripe 输入框已换好的 pm_（混合模式）。与 card 二选一。 */
  payment_method_id?: string;
  billing: BillingInput;
  proxy?: string;
  /** 协议链路的可选环节，全部默认关闭 = 批量脚本模式 */
  steps?: {
    fingerprint?: boolean;
    dwell?: boolean;
    elements?: boolean;
    tax_region?: boolean;
  };
}

/**
 * 全协议模式：服务端直接和 Stripe 对话，不加载任何 Stripe.js、不开浏览器。
 * 卡号只在这一次请求里使用，服务端不落库、不打日志。
 * 链路较长（指纹注册 → init → elements → tax → hCaptcha → pm → confirm → poll），
 * 单次可能要 60–180 秒，所以这里给 5 分钟上限。
 */
export async function payProtocol(orderId: string, input: ProtocolPayInput): Promise<ProtocolPayResult> {
  const res = await fetchWithDeadline(`/api/orders/${orderId}/pay-protocol`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(input),
  }, 300000, "协议支付超时（服务端仍在跑，请稍后查询订单状态）");
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as ProtocolPayResult;
}

const ZERO_DECIMAL = new Set(["BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"]);

export async function bindAndPayProtocol(orderId: string, input: BindAndPayProtocolInput): Promise<BindAndPayProtocolResult> {
  const res = await fetchWithDeadline(`/api/orders/${encodeURIComponent(orderId)}/bind-and-pay-protocol`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(input),
  }, 300000, "协议支付结果待核实，请使用查询协议状态，暂勿重复提交");
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as BindAndPayProtocolResult;
}

export async function getBindAndPayProtocol(orderId: string): Promise<BindAndPayProtocolResult> {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/bind-and-pay-protocol`);
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as BindAndPayProtocolResult;
}

export function formatAmount(amount?: string | null, currency?: string | null): string | null {
  if (amount == null || amount === "") return null;
  const value = Number(amount);
  const code = (currency || "").trim().toUpperCase();
  if (!Number.isFinite(value)) return [amount, code].filter(Boolean).join(" ");
  if (code) {
    try {
      return new Intl.NumberFormat("en", {
        style: "currency",
        currency: code,
        minimumFractionDigits: ZERO_DECIMAL.has(code) ? 0 : 2,
        maximumFractionDigits: ZERO_DECIMAL.has(code) ? 0 : 2,
      }).format(value);
    } catch {
      /* unknown currency code */
    }
  }
  return code ? `${amount} ${code}` : amount;
}

export async function preparePlusSavedCard(orderId: string): Promise<SavedCardPreparation> {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/saved-card/prepare`, { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as SavedCardPreparation;
}

export async function getPlusSavedCard(orderId: string, signal?: AbortSignal): Promise<SavedCardStatus> {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/saved-card`, { signal });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as SavedCardStatus;
}

export async function refreshPlusSavedCardCheckout(orderId: string): Promise<SavedCardCheckout> {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/saved-card/refresh-checkout`, { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as SavedCardCheckout;
}

export async function getOrder(orderId: string): Promise<Order> {
  const res = await fetch(`/api/orders/${orderId}`);
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Order;
}

export function isHostedOrder(order: Pick<Order, "plan" | "checkout_kind">): boolean {
  return order.checkout_kind === "hosted" || order.plan === "team";
}

export interface ProxyInfo {
  host: string;
  port: number;
  scheme: string;
  region: string;
  sid: string;
  enabled: boolean;
  has_username: boolean;
  has_password: boolean;
  has_region_param?: boolean;
  has_sid_param?: boolean;
  routing_format?: "123proxy" | "standard";
}

export async function getProxies(): Promise<{ checkout: ProxyInfo; approve: ProxyInfo }> {
  const res = await fetch("/api/settings/proxies");
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export async function parseProxy(raw: string): Promise<ProxyInfo> {
  const res = await fetch("/api/settings/proxies/parse", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ raw }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export async function saveProxy(raw: string, enabled: boolean): Promise<ProxyInfo> {
  const res = await fetch("/api/settings/proxies", {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify({ raw, enabled }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}

export async function testProxy(): Promise<{ ip: string; elapsed_ms: number; region: string; sid: string }> {
  const res = await fetch("/api/settings/proxies/checkout/test", { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
  return await res.json();
}
