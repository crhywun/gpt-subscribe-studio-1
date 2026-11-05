import { Alert, Badge, Button, Checkbox, Code, Group, Paper, PasswordInput, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import {
  bindAndPayProtocol,
  formatAmount,
  getBindAndPayProtocol,
  type BindAndPayProtocolInput,
  type BindAndPayProtocolResult,
  type BillingInput,
  type ProtocolPayResult,
  type TeamCardPayload,
} from "../api/client";
import { PROTOCOL_STAGE_LABELS, protocolBillingError, protocolCardError, protocolIsPaid, protocolPostReady, protocolStateLabel } from "../card-payment/protocolBind";

interface Props {
  orderId: string;
  country: string;
  sessionEmail?: string | null;
  initialBilling?: Partial<BillingInput> | null;
  amount?: string | null;
  currency?: string | null;
  quotationError?: string | null;
  disabled?: boolean;
  submissionDisabled?: boolean;
  onBusyChange?: (busy: boolean) => void;
  onDone?: (result: ProtocolPayResult) => void;
  onRefreshQuote?: () => Promise<void> | void;
}

export function PlusProtocolPayPanel({
  orderId, country, sessionEmail, initialBilling, amount, currency, quotationError, disabled = false,
  submissionDisabled = false, onBusyChange, onDone, onRefreshQuote,
}: Props) {
  const [card, setCard] = useState<TeamCardPayload>({ number: "", cvc: "", exp_month: "", exp_year: "" });
  const [billing, setBilling] = useState<BillingInput>({
    name: "", email: sessionEmail || "", line1: "", city: "", state: "", postal_code: "", country: country || "US",
    ...initialBilling,
  });
  const [result, setResult] = useState<BindAndPayProtocolResult | null>(null);
  const [work, setWork] = useState<"submit" | "query" | "quote" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsQuery, setNeedsQuery] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [quoteConfirmed, setQuoteConfirmed] = useState(false);
  const busyRef = useRef(false);
  const epoch = useRef(0);
  const submittedDetails = useRef<Pick<BindAndPayProtocolInput, "card" | "billing"> | null>(null);
  const onBusyRef = useRef(onBusyChange);
  const onDoneRef = useRef(onDone);
  onBusyRef.current = onBusyChange;
  onDoneRef.current = onDone;

  useEffect(() => {
    epoch.current += 1;
    return () => { epoch.current += 1; onBusyRef.current?.(false); };
  }, [orderId]);

  const price = formatAmount(amount, currency);
  const hasQuote = amount != null && amount !== "" && Number.isFinite(Number(amount)) && Number(amount) >= 0 && Boolean(currency);
  const quoteChanged = result?.state === "pending" && result.stage === "checkout";
  const canSubmit = !disabled && !submissionDisabled && work === null && hasQuote && !needsQuery
    && protocolPostReady(result) && (!quoteChanged || quoteConfirmed);
  const fieldsDisabled = disabled || work !== null || submitted;
  const billingDisabled = fieldsDisabled || Boolean(initialBilling);

  function applyResult(out: BindAndPayProtocolResult) {
    setResult(out);
    setNeedsQuery(false);
    setQuoteConfirmed(false);
    if (out.state === "not_started") {
      setSubmitted(false);
      submittedDetails.current = null;
    }
    onDoneRef.current?.(out);
  }

  async function run(kind: "submit" | "query" | "quote", action: (isCurrent: () => boolean) => Promise<void>) {
    if (disabled || busyRef.current) return;
    busyRef.current = true;
    const current = epoch.current;
    const isCurrent = () => epoch.current === current;
    setWork(kind);
    setError(null);
    onBusyRef.current?.(true);
    try {
      await action(isCurrent);
    } catch (err) {
      if (isCurrent()) setError(err instanceof Error ? err.message : "请求结果待核实，请查询协议状态。");
    } finally {
      if (isCurrent()) {
        busyRef.current = false;
        setWork(null);
        onBusyRef.current?.(false);
      }
    }
  }

  function submitProtocol() {
    if (!canSubmit) return;
    const details = submittedDetails.current || {
      card: { ...card, number: card.number.replace(/[\s-]/g, "") },
      billing: {
        ...billing,
        name: billing.name.trim(), email: (billing.email || "").trim(), line1: billing.line1.trim(),
        city: billing.city.trim(), state: billing.state.trim(), postal_code: billing.postal_code.trim(),
        country: billing.country.trim().toUpperCase(),
      },
    };
    const invalid = protocolCardError(details.card) || protocolBillingError(details.billing);
    if (invalid) { setError(invalid); return; }
    void run("submit", async (isCurrent) => {
      setNeedsQuery(true);
      setSubmitted(true);
      submittedDetails.current = details;
      const out = await bindAndPayProtocol(orderId, {
        ...details, expected_amount: String(amount), expected_currency: String(currency),
      });
      if (isCurrent()) applyResult(out);
    });
  }

  function queryProtocol() {
    void run("query", async (isCurrent) => {
      const out = await getBindAndPayProtocol(orderId);
      if (isCurrent()) applyResult(out);
    });
  }

  const changeCard = (field: keyof TeamCardPayload) => (event: React.ChangeEvent<HTMLInputElement>) => {
    const value = event.currentTarget.value;
    setCard((previous) => ({ ...previous, [field]: value }));
  };
  const changeBilling = (field: keyof BillingInput) => (event: React.ChangeEvent<HTMLInputElement>) => {
    const value = event.currentTarget.value;
    setBilling((previous) => ({ ...previous, [field]: field === "country" ? value.toUpperCase() : value }));
  };

  return (
    <Stack gap="md" maw={720} mx="auto" data-testid="plus-protocol-pay-panel"
      onKeyDownCapture={(event) => {
        if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
          event.preventDefault();
          event.stopPropagation();
        }
      }}>
      <Paper withBorder p="lg" radius="md">
        <Group justify="space-between">
          <div>
            <Text size="sm" fw={600}>Plus 协议支付</Text>
            <Text size="xs" c="dimmed">应付金额</Text>
            <Text fz={30} fw={700}>{price || "待取得报价"}</Text>
          </div>
          <Badge variant="light">服务端协议</Badge>
        </Group>
        {quotationError && <Alert color="red" mt="sm">{quotationError}</Alert>}
        {onRefreshQuote && (
          <Button type="button" variant="light" size="xs" mt="sm" loading={work === "quote"}
            disabled={disabled || work !== null || submitted}
            onClick={() => void run("quote", async () => { await onRefreshQuote(); })}>刷新报价</Button>
        )}
      </Paper>
      <Text size="sm">填写持卡人的卡片和真实账单，确认金额后提交。服务端沿用当前订单 checkout，依次核算税费、创建 confirmation_tokens、完成 ChatGPT confirm 与 PaymentIntent confirm，再通过 GET checkout 核对 complete / paid 并查询订阅。</Text>
      <Text size="xs" c="dimmed">每个订单沿用首次提交的同一份卡片和账单，修改资料请重新开始。付款确认后仍需等待订阅生效；结果待核实时仅查询状态，不重放付款请求。</Text>
      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <TextInput label="卡号" value={card.number} onChange={changeCard("number")} inputMode="numeric" autoComplete="cc-number" disabled={fieldsDisabled} data-testid="protocol-card-number" />
          <SimpleGrid cols={{ base: 1, xs: 3 }}>
            <TextInput label="到期月份" value={card.exp_month} onChange={changeCard("exp_month")} inputMode="numeric" maxLength={2} autoComplete="cc-exp-month" disabled={fieldsDisabled} />
            <TextInput label="到期年份" value={card.exp_year} onChange={changeCard("exp_year")} inputMode="numeric" maxLength={4} autoComplete="cc-exp-year" disabled={fieldsDisabled} />
            <PasswordInput label="CVC" value={card.cvc} onChange={changeCard("cvc")} inputMode="numeric" maxLength={4} autoComplete="cc-csc" disabled={fieldsDisabled} />
          </SimpleGrid>
        </Stack>
      </Paper>
      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <Text fw={600}>账单地址</Text>
          <Text size="xs" c="dimmed">{initialBilling ? "本次报价使用创建结账时填写的地址。修改地址请重新创建结账。" : "请填写付款人的真实账单地址，税费以支付平台返回结果为准。"}</Text>
          <SimpleGrid cols={{ base: 1, xs: 2 }}>
            <TextInput label="持卡人姓名" value={billing.name} onChange={changeBilling("name")} autoComplete="cc-name" disabled={billingDisabled} />
            <TextInput label="账单邮箱" type="email" value={billing.email || ""} onChange={changeBilling("email")} autoComplete="email" disabled={billingDisabled} />
          </SimpleGrid>
          <TextInput label="街道地址" value={billing.line1} onChange={changeBilling("line1")} autoComplete="address-line1" disabled={billingDisabled} />
          <SimpleGrid cols={{ base: 1, xs: 2 }}>
            <TextInput label="城市" value={billing.city} onChange={changeBilling("city")} autoComplete="address-level2" disabled={billingDisabled} />
            <TextInput label="州 / 省" value={billing.state} onChange={changeBilling("state")} autoComplete="address-level1" disabled={billingDisabled} />
            <TextInput label="邮编" value={billing.postal_code} onChange={changeBilling("postal_code")} autoComplete="postal-code" disabled={billingDisabled} />
            <TextInput label="账单国家（两位代码）" value={billing.country} onChange={changeBilling("country")} maxLength={2} autoComplete="country" disabled={billingDisabled} />
          </SimpleGrid>
        </Stack>
      </Paper>
      {quoteChanged && (
        <Alert color="blue" title="请确认当前应付金额">
          <Text size="sm">当前金额为 {price}。核对后再次显式提交。</Text>
          <Checkbox mt="sm" label={`我已确认应付金额 ${price || ""}`} checked={quoteConfirmed}
            disabled={disabled || work !== null} onChange={(event) => setQuoteConfirmed(event.currentTarget.checked)} />
        </Alert>
      )}
      {(needsQuery || result?.state === "unknown") && <Alert color="yellow">请求结果待核实，请使用「查询协议状态」。</Alert>}
      {result && (
        <Alert color={protocolIsPaid(result) ? "green" : result.state === "integration_error" || result.state === "request_rejected" ? "red" : result.state === "challenge_required" ? "yellow" : "blue"} role="status">
          <Stack gap="xs">
            <Group gap="sm"><Text fw={600}>阶段：{PROTOCOL_STAGE_LABELS[result.stage]}</Text><Code>{result.stage}</Code></Group>
            <Group gap="sm"><Text fw={600}>状态：{protocolStateLabel(result.state)}</Text><Code>{result.state}</Code></Group>
            {result.message && <Text size="sm">{result.message}</Text>}
            {result.remediation && <Text size="sm">{result.remediation}</Text>}
            {result.upstream_message && <Code block style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{result.upstream_message}</Code>}
            {result.upstream_status && <Text size="xs">Stripe HTTP：{result.upstream_status}{result.upstream_code ? ` · ${result.upstream_code}` : ""}</Text>}
            {result.next_action?.type && <Text size="sm">待处理动作：{result.next_action.type}</Text>}
            {result.code && <Text size="sm">code：{result.code}</Text>}
            {result.decline_code && <Text size="sm">decline_code：{result.decline_code}</Text>}
            {result.state === "challenge_required" && <Text size="sm">当前结果需要上游验证。查询协议状态可读取后续进展。</Text>}
          </Stack>
        </Alert>
      )}
      <Group justify="space-between">
        <Button type="button" variant="default" loading={work === "query"} disabled={disabled || work !== null} onClick={queryProtocol}>查询协议状态</Button>
        <Button type="button" loading={work === "submit"} disabled={!canSubmit} onClick={submitProtocol}>协议支付{price ? ` ${price}` : ""}</Button>
      </Group>
      {work && <Text size="sm" role="status">{work === "query" ? "正在读取协议状态…" : work === "quote" ? "正在读取原结账的报价…" : "服务端正在处理本次请求…"}</Text>}
      {error && <Alert color="red" role="alert">{error}</Alert>}
    </Stack>
  );
}
