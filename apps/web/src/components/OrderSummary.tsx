import { Badge, Divider, Group, Paper, Stack, Text, Title } from "@mantine/core";

import { formatAmount, isHostedOrder, type Order, type Plan } from "../api/client";
import { FAILED_STATUSES, SUCCESS_STATUSES } from "../api/history";
import { planLabel, regionLabel } from "../plans";

export const STATUS_LABELS: Record<string, string> = {
  created: "已创建",
  pending_checkout: "正在生成结账",
  awaiting_payment: "等待支付",
  paying: "支付中",
  requires_action: "需要支付验证",
  unknown: "状态待确认",
  verifying: "正在确认订阅",
  active: "订阅已生效",
  delivered: "已开通",
  rejected: "已拒绝",
  failed: "失败",
};

export function statusColor(status: string): string {
  if (SUCCESS_STATUSES.includes(status)) return "green";
  if (FAILED_STATUSES.includes(status)) return "red";
  return "yellow";
}

interface Props {
  plan: Plan;
  country: string;
  order: Order | null;
  seats?: number;
}

export function OrderSummary({ plan, country, order, seats }: Props) {
  const due = order ? formatAmount(order.amount, order.currency) : null;
  return (
    <Paper withBorder p="lg" radius="md">
      <Stack gap="sm">
        <Group justify="space-between" align="center">
          <Title order={5}>订单摘要</Title>
          {order && (
            <Badge color={statusColor(order.status)} variant="light">
              {STATUS_LABELS[order.status] ?? order.status}
            </Badge>
          )}
        </Group>
        <Divider />
        <Row label="套餐" value={planLabel(plan)} />
        <Row label={plan === "team" ? "创建结账地区" : "地区"} value={country ? regionLabel(country) : "未选择"} muted={!country} />
        {plan === "team" && <Row label="席位" value={`${order?.seat_quantity ?? seats ?? "-"} 人`} />}
        <Divider />
        <Group justify="space-between" align="baseline">
          <Text size="sm" c="dimmed">
            应付金额
          </Text>
          {due ? (
            <Text fw={700} size="xl">
              {due}
            </Text>
          ) : (
            <Text size="sm" c="dimmed" ta="right">
              {!order
                ? plan === "plus" ? "填写账单地址并创建结账后显示" : "创建结账后显示"
                : isHostedOrder(order)
                  ? "尚未取得 Stripe 报价"
                  : "未从结账拿到金额"}
            </Text>
          )}
        </Group>
        {order?.quote_error && (
          <Text size="xs" c="orange">{order.quote_error}</Text>
        )}
        {plan === "team" && due && (
          <Text size="xs" c="dimmed">Stripe 当前报价；确认前会按账单地址重算税费。</Text>
        )}
        {order && (
          <Text size="xs" c="dimmed" style={{ wordBreak: "break-all" }}>
            订单 {order.id}
          </Text>
        )}
      </Stack>
    </Paper>
  );
}

function Row({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <Group justify="space-between" wrap="nowrap">
      <Text size="sm" c="dimmed">
        {label}
      </Text>
      <Text size="sm" fw={500} c={muted ? "dimmed" : undefined}>
        {value}
      </Text>
    </Group>
  );
}
