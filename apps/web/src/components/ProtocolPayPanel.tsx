import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Paper,
  PasswordInput,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
  TextInput,
  Timeline,
  Checkbox,
  SegmentedControl,
} from "@mantine/core";
import { IconAlertCircle, IconCheck, IconClock, IconX } from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { CheckoutLinkCard } from "./CheckoutLinkCard";
import { PlusProtocolPayPanel } from "./PlusProtocolPayPanel";
import { StripeCardFields, type StripeCardHandle } from "../card-payment/CardFields";

import { formatAmount, payProtocol, payOrder, getOrder, verifyOrder, type BillingInput, type ProtocolPayResult, type Order } from "../api/client";
import { randomBillingExample } from "../card-payment/billingExamples";

interface Props {
  orderId: string;
  bindBeforePay?: boolean;
  browserPayment?: boolean;
  submissionDisabled?: boolean;
  /** 订单创建时选的地区，用于预填账单国家 */
  country: string;
  /** 应付金额（最小货币单位）与币种，来自 /team/quote */
  amount?: string | null;
  currency?: string | null;
  quotationError?: string | null;
  /** 本轮实际使用的出口（只显示国家与 ISP，不显示账号密码） */
  exitLabel?: string;
  /** Stripe 公钥；有它才能挂载 Stripe 输入框 */
  publishableKey?: string | null;
  checkoutUrl?: string | null;
  /** 从 accessToken 解析出的账户邮箱，用于预填账单邮箱 */
  sessionEmail?: string | null;
  initialBilling?: Partial<BillingInput> | null;
  /** 订单是否带优惠活动：带的话浏览器 pm_ 要声明 allow_redisplay */
  requireSaveChoice?: boolean;
  /** 取 pm_ 的方式；由上层持有，提链前就能选 */
  pmMode: "server" | "stripe";
  onPmMode?: (mode: "server" | "stripe") => void;
  disabled?: boolean;
  onBusyChange?: (busy: boolean) => void;
  onDone?: (result: ProtocolPayResult) => void;
  /** 重新向 Stripe 读一次金额（报价超时会用） */
  onRefreshQuote?: () => Promise<void> | void;
}


interface CardDraft {
  number: string;
  exp_month: string;
  exp_year: string;
  cvc: string;
}

const EMPTY_CARD: CardDraft = { number: "", exp_month: "", exp_year: "", cvc: "" };

/** 结果分类：把服务端 state 翻成人话，并标出下一步该干什么。 */
function describe(result: ProtocolPayResult) {
  switch (result.state) {
    case "paid":
      return {
        color: "green" as const,
        icon: <IconCheck size={18} />,
        title: "支付成功",
        hint: `PaymentIntent 状态 ${result.intent_status ?? "succeeded"}`,
      };
    case "card_declined":
      return {
        color: "red" as const,
        icon: <IconX size={18} />,
        title: "卡片被拒",
        hint: [
          result.code && `code=${result.code}`,
          result.decline_code && `decline_code=${result.decline_code}`,
          result.charge_id && `charge=${result.charge_id}`,
          result.message,
        ].filter(Boolean).join(" · "),
      };
    case "captcha_rejected":
      return {
        color: "orange" as const,
        icon: <IconAlertCircle size={18} />,
        title: "hCaptcha 验证未通过",
        hint: [
          result.code && `code=${result.code}`,
          result.message,
          "Stripe 要求二次人机验证，当前出口 IP 未通过评分；换代理 SID 再试。",
        ].filter(Boolean).join(" · "),
      };
    case "challenge_required":
      return {
        color: "yellow" as const,
        icon: <IconClock size={18} />,
        title: "被 hCaptcha 人机验证拦截",
        hint: [
          "Stripe 在 confirm 阶段返回 intent_confirmation_challenge（hCaptcha，不是 3DS）",
          "纯协议不做求解，本单已放弃、未扣款",
          result.intent_status && `PaymentIntent 状态 ${result.intent_status}`,
        ].filter(Boolean).join(" · "),
      };
    default:
      return {
        color: "gray" as const,
        icon: <IconClock size={18} />,
        title: `未知结果：${result.state}`,
        hint: [result.code, result.decline_code, result.message].filter(Boolean).join(" · "),
      };
  }
}

const STEPS = [
  { title: "指纹注册", description: "m.stripe.com/6 × 4 → guid/muid/sid" },
  { title: "init", description: "取金额、hCaptcha sitekey 与 rqdata" },
  { title: "elements / 地址", description: "elements/sessions + tax_region" },
  { title: "confirm", description: "402 拒卡 / 200 challenge / 成功" },
  { title: "poll", description: "轮询终态" },
];

function LegacyProtocolPayPanel({
  orderId, country, amount, currency, quotationError, exitLabel, sessionEmail, initialBilling,
  publishableKey, pmMode, onPmMode, requireSaveChoice = false,
  checkoutUrl, disabled, submissionDisabled, browserPayment = false, onBusyChange, onDone, onRefreshQuote,
}: Props) {
  const [card, setCard] = useState<CardDraft>(EMPTY_CARD);
  // 账单地址在五个免税州里随机生成（避免每次同一地址）；
  // 邮箱优先用 accessToken 里解析出的账户邮箱。
  const [billing, setBilling] = useState<BillingInput>(() => {
    const base: BillingInput = {
      email: (sessionEmail ?? "").trim(),
      name: "",
      line1: "", city: "", state: "", postal_code: "",
      country: country || "US",
    };
    return initialBilling ? { ...base, ...initialBilling } : browserPayment ? base : randomBillingExample(base);
  });
  // 取 pm_ 的方式由上层持有（提链前就能选），这里只保留输入框句柄
  const [stripeCard, setStripeCard] = useState<StripeCardHandle | null>(null);
  const [proxy, setProxy] = useState("");
  // 协议链路的可选环节，全部默认关闭 = 与 scripts/run_cards.py 一致的批量模式
  const [steps, setSteps] = useState({
    fingerprint: false,
    dwell: false,
    elements: false,
    tax_region: false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ProtocolPayResult | null>(null);
  const [paymentSubmitted, setPaymentSubmitted] = useState(false);
  function applyBrowserOrder(next: Order) {
    const state = ["active", "delivered"].includes(next.status) ? "paid"
      : next.status === "requires_action" ? "challenge_required"
      : next.status === "failed" ? "card_declined" : "pending";
    const out: ProtocolPayResult = { state, message: next.last_error, order: next };
    setResult(out); onDone?.(out);
  }
  async function queryBrowserPayment() {
    if (busy || disabled) return;
    setBusy(true); setError(null);
    try { applyBrowserOrder(await verifyOrder(orderId)); }
    catch (err) { setError(err instanceof Error ? err.message : "支付状态读取失败"); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    onBusyChange?.(busy);
    return () => { onBusyChange?.(false); };
  }, [busy, onBusyChange]);

  const changeCard = (field: keyof CardDraft) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setCard({ ...card, [field]: event.currentTarget.value });
  const changeBilling = (field: keyof BillingInput) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setBilling({ ...billing, [field]: event.currentTarget.value });

  async function run() {
    if (disabled || busy || (browserPayment && (paymentSubmitted || submissionDisabled))) return;
    setError(null);
    setResult(null);

    if (!billing.name.trim() || !billing.line1.trim() || !billing.city.trim() || !billing.postal_code.trim()) {
      return setError("账单地址要填全：姓名、街道、城市、邮编。");
    }
    if (!(billing.email ?? "").trim()) {
      return setError("邮箱必须填写：Stripe 会把收据发到这个地址，缺失会影响风控判定。");
    }
    if (billing.country === "US" || billing.country === "CA") {
      if (!billing.state.trim()) return setError(`${billing.country} 账单地址必须填州/省。`);
    }

    const payloadBilling = { ...billing, country: (billing.country || "US").toUpperCase() };
    const shared = { billing: payloadBilling, proxy: proxy.trim() || undefined, steps };

    // ── 方式一：浏览器 Stripe 输入框换 pm_ ──────────────
    if (pmMode === "stripe") {
      if (!stripeCard) {
        return setError("Stripe 输入框还没准备好，请等几秒或刷新页面再试。");
      }
      setBusy(true);
      try {
        const pmId = await stripeCard.tokenize({
          name: payloadBilling.name,
          email: payloadBilling.email || undefined,
          line1: payloadBilling.line1,
          city: payloadBilling.city,
          state: payloadBilling.state,
          postal_code: payloadBilling.postal_code,
          country: payloadBilling.country,
        });
        if (browserPayment) {
          setPaymentSubmitted(true);
          try { applyBrowserOrder(await payOrder(orderId, pmId, payloadBilling)); }
          catch (err) {
            const latest = await getOrder(orderId).catch(() => null);
            if (latest && latest.status !== "awaiting_payment") applyBrowserOrder(latest);
            throw err;
          }
        } else {
          const out = await payProtocol(orderId, { ...shared, payment_method_id: pmId });
          setResult(out); onDone?.(out);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Stripe 输入框支付失败");
      } finally {
        setBusy(false);
      }
      return;
    }

    // ── 方式二：服务端提交卡号（协议模式）──────────────
    const number = card.number.replace(/[\s-]/g, "");
    if (!/^\d{12,19}$/.test(number)) return setError("卡号格式不对（12–19 位数字）。");
    if (!/^\d{3,4}$/.test(card.cvc)) return setError("CVC 格式不对（3–4 位）。");
    if (!/^\d{1,2}$/.test(card.exp_month)) return setError("到期月份填 MM。");
    if (!/^\d{2}$|^\d{4}$/.test(card.exp_year)) return setError("到期年份填 YY 或 YYYY。");

    setBusy(true);
    try {
      const out = await payProtocol(orderId, {
        ...shared,
        card: { number, cvc: card.cvc, exp_month: card.exp_month, exp_year: card.exp_year },
      });
      setResult(out);
      onDone?.(out);
    } catch (err) {
      setError(err instanceof Error ? err.message : "协议支付失败");
    } finally {
      setBusy(false);
    }
  }

  const view = result ? describe(result) : null;

  const price = formatAmount(amount, currency);

  return (
    <Stack gap="md" maw={720} mx="auto">
      {/* 金额放最上面：确认支付之前先让人看清要付多少 */}
      <Paper withBorder p="lg" radius="md" className="protocol-amount">
        <Group justify="space-between" align="flex-end" wrap="nowrap">
          <div>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>应付金额</Text>
            <Text fz={32} fw={700} lh={1.1} mt={4}>
              {price ?? (quotationError ? "读取失败" : "读取中…")}
            </Text>
            <Text size="xs" c="dimmed" mt={4}>
              {country}
              {currency ? ` · ${currency.toUpperCase()}` : ""}
              {exitLabel ? ` · 出口 ${exitLabel}` : ""}
            </Text>
          </div>
          <Badge size="lg" variant="light" color={price ? "green" : "gray"}>
            {price ? "待支付" : "待报价"}
          </Badge>
        </Group>
        {quotationError && (
          <Alert color="red" variant="light" mt="sm" icon={<IconAlertCircle size={16} />}>
            金额读取失败：{quotationError}
          </Alert>
        )}
        {!price && !quotationError && (
          <Group justify="space-between" align="center" mt="sm" wrap="nowrap">
            <Text size="xs" c="dimmed">
              正在向 Stripe 读取应付金额（init + tax_region），读到后按钮才可用。若一直不动，点右侧刷新。
            </Text>
            {onRefreshQuote && (
              <Button type="button" size="xs" variant="light" disabled={busy || disabled} onClick={() => void onRefreshQuote()}>
                刷新金额
              </Button>
            )}
          </Group>
        )}
        {quotationError && onRefreshQuote && (
          <Button type="button" size="xs" variant="light" mt="xs" disabled={busy || disabled} onClick={() => void onRefreshQuote()}>
            重新读取金额
          </Button>
        )}
      </Paper>

      {!browserPayment && <CheckoutLinkCard url={checkoutUrl} />}

      {browserPayment ? <Alert color="blue">输入框模式：卡号只进入 Stripe 安全输入框，浏览器取得支付方式后由服务端确认付款。</Alert> : <><Alert color="blue" variant="light" icon={<IconAlertCircle size={18} />} title="两种提交方式">
        <Text size="xs">
          <b>协议提交</b>：卡号发到本服务，服务端直接向 Stripe 换 pm_。可批量、无人值守，
          卡号不落库、不打日志。
        </Text>
        <Text size="xs" mt={4}>
          <b>Stripe 输入框</b>：卡号只进入 Stripe 的 iframe，浏览器直接换 pm_，
          指纹与 Radar 信号和真实用户一致。
        </Text>
        <Text size="xs" mt={4} c="dimmed">
          两种方式的服务端链路相同：pm_ → ctoken_ → confirm → PaymentIntent。
        </Text>
      </Alert>

      <SegmentedControl
        fullWidth
        mb="md"
        value={pmMode}
        onChange={(value) => onPmMode?.(value as "server" | "stripe")}
        disabled={busy || disabled}
        data={[
          { value: "server", label: "协议提交（服务端提交卡号）" },
          { value: "stripe", label: "Stripe 输入框（浏览器换 pm_）" },
        ]}
      /></>}

      {pmMode === "stripe" ? (
        <Paper withBorder p="md" radius="md">
          <Stack gap="sm">
            {publishableKey ? (
              <StripeCardFields
                publishableKey={publishableKey}
                amount={amount}
                currency={currency}
                checkoutUrl={checkoutUrl}
                requirePaymentMethod
                requireSaveChoice={requireSaveChoice}
                disabled={busy || disabled || (browserPayment && paymentSubmitted)}
                onReady={setStripeCard}
              />
            ) : (
              <Alert color="orange" variant="light" icon={<IconAlertCircle size={16} />}>
                这个订单没有 publishable_key，挂不了 Stripe 输入框。请改用「协议提交」。
              </Alert>
            )}
          </Stack>
        </Paper>
      ) : (
        <Paper withBorder p="md" radius="md">
          <Stack gap="sm">
            <TextInput label="卡号" value={card.number} onChange={changeCard("number")}
              inputMode="numeric" autoComplete="off" disabled={busy || disabled} placeholder="4242424242424242" />
            <SimpleGrid cols={{ base: 1, xs: 3 }}>
              <TextInput label="到期月份" placeholder="MM" value={card.exp_month}
                onChange={changeCard("exp_month")} inputMode="numeric" maxLength={2} disabled={busy || disabled} />
              <TextInput label="到期年份" placeholder="YYYY" value={card.exp_year}
                onChange={changeCard("exp_year")} inputMode="numeric" maxLength={4} disabled={busy || disabled} />
              <PasswordInput label="CVC" value={card.cvc} onChange={changeCard("cvc")}
                inputMode="numeric" maxLength={4} disabled={busy || disabled} />
            </SimpleGrid>
          </Stack>
        </Paper>
      )}

      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Text fw={600}>账单地址</Text>
            <Group gap="xs">
              <Badge variant="light">{billing.country} / {billing.state}</Badge>
              <Button type="button" size="xs" variant="light" data-testid="random-billing-address"
                disabled={busy || disabled || (browserPayment && paymentSubmitted)}
                onClick={() => setBilling((current) => randomBillingExample(current))}>随机生成地址</Button>
            </Group>
          </Group>
          {!browserPayment && <><Text size="xs" c="dimmed">
            默认从五个免税州（AK / DE / MT / NH / OR）随机生成，可手动修改。
            邮箱取自 accessToken 解析结果，会作为 Stripe 收据地址。
          </Text>
          </>}
          <Text size="xs" c="dimmed">账单国家独立于订单地区。随机地址保留姓名和邮箱，可继续手动修改；订单币种保持不变。</Text>
          <SimpleGrid cols={{ base: 1, xs: 2 }}>
            <TextInput label="持卡人姓名" value={billing.name} onChange={changeBilling("name")} disabled={busy || disabled} />
            <TextInput
              label="邮箱"
              value={billing.email ?? ""}
              onChange={changeBilling("email")}
              disabled={busy || disabled}
              placeholder="name@example.com"
              description={
                !sessionEmail
                  ? "accessToken 里没解析到邮箱，请手动填写"
                  : (billing.email ?? "").trim() === sessionEmail.trim()
                    ? "已从 accessToken 自动填入，可直接修改"
                    : `已手动修改；账户邮箱是 ${sessionEmail}`
              }
            />
          </SimpleGrid>
          <TextInput label="街道地址" value={billing.line1} onChange={changeBilling("line1")} disabled={busy || disabled} />
          <SimpleGrid cols={{ base: 1, xs: 3 }}>
            <TextInput label="城市" value={billing.city} onChange={changeBilling("city")} disabled={busy || disabled} />
            <TextInput label="州 / 省" value={billing.state} onChange={changeBilling("state")} disabled={busy || disabled} />
            <TextInput label="邮编" value={billing.postal_code} onChange={changeBilling("postal_code")} disabled={busy || disabled} />
            <TextInput label="账单国家" value={billing.country} onChange={changeBilling("country")}
              maxLength={2} disabled={busy || disabled || (browserPayment && paymentSubmitted)} />
          </SimpleGrid>
        </Stack>
      </Paper>

      {!browserPayment && <><Paper withBorder p="md" radius="md">
        <Stack gap="xs">
          <Group justify="space-between">
            <Text fw={600} size="sm">额外环节（默认全关）</Text>
            <Badge variant="light" size="sm">批量模式</Badge>
          </Group>
          <Text size="xs" c="dimmed">
            默认链路与批量脚本一致：init → payment_methods → confirm。
            勾选后逐项加回更像真人的环节，会拉长耗时。
          </Text>
          <Checkbox size="sm" checked={steps.fingerprint} disabled={busy || disabled}
            onChange={(e) => setSteps({ ...steps, fingerprint: e.currentTarget.checked })}
            label="指纹注册（m.stripe.com/6 × 4 + allowed_origins）" />
          <Checkbox size="sm" checked={steps.dwell} disabled={busy || disabled}
            onChange={(e) => setSteps({ ...steps, dwell: e.currentTarget.checked })}
            label="init 后停留 10–16 秒（模拟阅读）" />
          <Checkbox size="sm" checked={steps.elements} disabled={busy || disabled}
            onChange={(e) => setSteps({ ...steps, elements: e.currentTarget.checked })}
            label="elements/sessions（Stripe 元素初始化）" />
          <Checkbox size="sm" checked={steps.tax_region} disabled={busy || disabled}
            onChange={(e) => setSteps({ ...steps, tax_region: e.currentTarget.checked })}
            label="提交 tax_region（按账单国家重算税额）" />
        </Stack>
      </Paper>

      <Textarea
        label="指定代理（可选）"
        description="留空则用「代理设置」里 checkout 的网关，并按账单国家自动对齐地区。"
        placeholder="http://user:pass@host:3010"
        value={proxy}
        onChange={(event) => setProxy(event.currentTarget.value)}
        autosize
        minRows={1}
        maxRows={2}
        disabled={busy || disabled}
      /></>}

      <Group justify="space-between" align="center">
        <Text size="xs" c="dimmed">
          {price
            ? `确认支付 ${price}；币种与税额以 Stripe 计算为准。`
            : "金额尚未读到，按钮暂不可用。"}
        </Text>
        <Button
          type="button"
          size="md"
          loading={busy}
          disabled={disabled || busy || !price || (browserPayment && (paymentSubmitted || submissionDisabled))}
          onClick={() => void run()}
        >
          {busy ? "提交中…" : !price ? "等待报价" : `确认支付 ${price}`}
        </Button>
      </Group>

      {browserPayment && paymentSubmitted && <Button type="button" variant="default" disabled={busy || disabled} onClick={() => void queryBrowserPayment()}>查询支付状态</Button>}
      {busy && !browserPayment && (
        <Paper withBorder p="md" radius="md">
          <Text size="sm" fw={600} mb="xs">服务端正在跑这条链路</Text>
          <Timeline active={3} bulletSize={18} lineWidth={2}>
            {STEPS.map((step) => (
              <Timeline.Item key={step.title} title={step.title}>
                <Text size="xs" c="dimmed">{step.description}</Text>
              </Timeline.Item>
            ))}
          </Timeline>
        </Paper>
      )}

      {error && (
        <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />} title="请求失败" role="alert">
          {error}
        </Alert>
      )}

      {result && view && !browserPayment && (
        <Alert color={view.color} variant="light" icon={view.icon} title={view.title} role="status">
          <Stack gap={6}>
            <Text size="sm">{view.hint}</Text>
            <Code block style={{ fontSize: 12 }}>
              {JSON.stringify(result, null, 2)}
            </Code>
          </Stack>
        </Alert>
      )}
      {result && browserPayment && <Alert color={result.state === "paid" ? "green" : "blue"} role="status">{result.message || `支付状态：${result.order?.status || result.state}`}</Alert>}
    </Stack>
  );
}

export function ProtocolPayPanel(props: Props) {
  return props.bindBeforePay ? <PlusProtocolPayPanel {...props} /> : <LegacyProtocolPayPanel {...props} />;
}
