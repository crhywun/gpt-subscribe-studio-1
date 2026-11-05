import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Paper,
  PasswordInput,
  Stack,
  Switch,
  Text,
  Title,
} from "@mantine/core";
import { IconAlertCircle, IconCheck } from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { getProxies, parseProxy, saveProxy, testProxy, type ProxyInfo } from "../api/client";

function Facts({ info }: { info: ProxyInfo }) {
  return (
    <Group gap="xs">
      <Badge variant="light" color="gray" tt="none">
        {info.scheme}://{info.host}:{info.port}
      </Badge>
      <Badge variant="light" color={info.region ? "indigo" : "gray"} tt="none">
        地区 {info.region || "未检测到"}
      </Badge>
      <Badge variant="light" color={info.sid ? "indigo" : "gray"} tt="none">
        SID {info.sid || "未检测到"}
      </Badge>
    </Group>
  );
}

export function ProxySettings() {
  const [raw, setRaw] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saved, setSaved] = useState<ProxyInfo | null>(null);
  const [preview, setPreview] = useState<ProxyInfo | null>(null);
  const [message, setMessage] = useState<{ color: "red" | "green"; text: string } | null>(null);
  const [busy, setBusy] = useState<"parse" | "save" | "test" | null>(null);

  useEffect(() => {
    getProxies()
      .then((all) => setSaved(all.checkout))
      .catch(() => setSaved(null));
  }, []);

  async function run(kind: "parse" | "save" | "test", action: () => Promise<void>) {
    setBusy(kind);
    setMessage(null);
    try {
      await action();
    } catch (err) {
      setMessage({ color: "red", text: err instanceof Error ? err.message : "操作失败" });
    } finally {
      setBusy(null);
    }
  }

  const configured = Boolean(saved?.host);

  return (
    <Stack gap="lg" maw={720}>
      <div>
        <Title order={4}>出口代理</Title>
        <Text size="sm" c="dimmed">
          填入供应商生成的完整代理。订单地区应与实际出口一致。123Proxy 使用末尾的 +国家码，并保留供应商生成的 SESSION；其他供应商按账号中的地区和 SID 参数分配订单出口。
        </Text>
      </div>

      <Paper withBorder p="md" radius="md">
        <Stack gap="xs">
          <Group justify="space-between">
            <Text fw={600} size="sm">
              当前配置
            </Text>
            <Badge color={saved?.enabled ? "green" : "gray"} variant="light">
              {configured ? (saved?.enabled ? "已启用" : "已停用") : "未配置"}
            </Badge>
          </Group>
          {configured && saved ? (
            <Facts info={saved} />
          ) : (
            <Text size="sm" c="dimmed">
              还没有保存代理。
            </Text>
          )}
        </Stack>
      </Paper>

      <Stack gap="sm">
        <PasswordInput
          label="代理地址"
          description="支持 主机:端口:账号:密码、http://账号:密码@主机:端口、socks5h://…。不写协议时默认 HTTP；SOCKS5 线路必须带 socks5h:// 前缀。123Proxy 支持账号+ph 格式。"
          placeholder="http://账号-region-US-sid-abc123:密码@gateway.example.com:3010"
          value={raw}
          onChange={(event) => {
            setRaw(event.currentTarget.value.trim());
            setPreview(null);
          }}
        />
        <Group>
          <Button
            variant="light"
            loading={busy === "parse"}
            disabled={!raw}
            onClick={() =>
              void run("parse", async () => {
                setPreview(await parseProxy(raw));
              })
            }
          >
            解析预览
          </Button>
        </Group>
      </Stack>

      {preview && (
        <Paper withBorder p="md" radius="md">
          <Stack gap="sm">
            <Text fw={600} size="sm">
              解析结果
            </Text>
            <Facts info={preview} />
            {preview.routing_format === "123proxy" ? (
              <Alert color={preview.has_sid_param ? "blue" : "yellow"} variant="light" icon={<IconAlertCircle size={18} />}>
                已识别 123Proxy 格式，国家码保持小写并位于账号末尾，不追加 <Code>-region-</Code> 或 <Code>-sid-</Code>。
                {preview.has_sid_param
                  ? " 将保留你从供应商控制台生成的 SESSION。"
                  : " 当前未包含粘性 SESSION，连续请求可能更换出口。多步骤流程建议在供应商控制台启用 SESSION 后重新复制完整代理。"}
                供应商说明：国家或 SESSION 变更可能需要 3–15 分钟同步。
              </Alert>
            ) : (!preview.has_region_param || !preview.has_sid_param) && (
              <Alert color="yellow" variant="light" icon={<IconAlertCircle size={18} />}>
                账号里没有检测到
                {!preview.has_region_param && " 地区参数"}
                {!preview.has_region_param && !preview.has_sid_param && "、"}
                {!preview.has_sid_param && " SID 参数"}。保存后本站会在账号末尾追加 <Code>-region-XX</Code> / <Code>-sid-随机值</Code>，请确认你的代理供应商支持这个写法，否则请先在账号里加上对应参数。
              </Alert>
            )}
            <Switch
              label="保存后启用"
              checked={enabled}
              onChange={(event) => setEnabled(event.currentTarget.checked)}
            />
            <Group>
              <Button
                loading={busy === "save"}
                onClick={() =>
                  void run("save", async () => {
                    const next = await saveProxy(raw, enabled);
                    setSaved(next);
                    setPreview(null);
                    setRaw("");
                    setMessage({ color: "green", text: "已保存，对之后新建的订单生效。" });
                  })
                }
              >
                保存
              </Button>
            </Group>
          </Stack>
        </Paper>
      )}

      <Group>
        <Button
          variant="default"
          disabled={!configured || !saved?.enabled}
          loading={busy === "test"}
          onClick={() =>
            void run("test", async () => {
              const result = await testProxy();
              setMessage({
                color: "green",
                text: `连接成功：出口 IP ${result.ip}，耗时 ${result.elapsed_ms} ms。请自行确认这个 IP 属于所选地区。`,
              });
            })
          }
        >
          测试连接
        </Button>
      </Group>

      {message && (
        <Alert
          color={message.color}
          variant="light"
          icon={message.color === "green" ? <IconCheck size={18} /> : <IconAlertCircle size={18} />}
        >
          {message.text}
        </Alert>
      )}
    </Stack>
  );
}
