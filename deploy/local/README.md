# Windows 本机部署

需要 PowerShell 7.4+、Python 3.11+、Node.js 20+ 和 npm。在仓库根目录操作。
运行时使用独立 `.venv`，配置、凭据、证书和日志写入被 Git 和 Docker 忽略的 `runtime/local/`。

## 原生运行

首次安装（将 Python 命令替换为本机 Python 3.11+ 的路径）：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r deploy\server\requirements.txt
npm.cmd ci --prefix apps\web --no-audit --no-fund
npm.cmd run build --prefix apps\web
npm.cmd install --prefix runtime\local\node --no-audit --no-fund undici@6
& .\start-local.ps1
```

访问 <https://localhost:18443>。初始化脚本自动生成后台密码，保存在 `runtime/local/ACCESS.txt`；重复运行不会覆盖已有密码和代理。
服务以 UTF-8 模式在后台运行，只监听本机回环地址。Windows 重启后需要重新运行启动脚本。

本地证书保存在 `runtime/local/certs/`，有效期一年，脚本不会修改系统证书信任存储。
浏览器首次访问可能提示证书不受信任，请确认地址为本机 `localhost`。如需更新证书，先停止服务，再移走旧证书和私钥并重新初始化。

自检和停止：

```powershell
& .\.venv\Scripts\python.exe -X utf8 .\deploy\local\verify.py
& .\stop-local.ps1
```

自检只访问本地页面和 API，不创建订单或付款。日志位于 `runtime/local/logs/`。
停止脚本核对 PID 的程序路径及命令行，处理 Windows venv 的解释器子进程，并保留运行数据。

## 可选 Docker 部署

先启动 Docker Desktop，并停止占用 18443 端口的原生服务：

```powershell
& .\stop-local.ps1
& .\deploy\local\setup.ps1
docker compose -f compose.local.yaml up -d --build
docker compose -f compose.local.yaml ps
```

独立 Compose 项目名为 `gss-local`，自动构建前端，通过 Caddy 提供本机 HTTPS。API 不发布到宿主机公网接口。
代理配置位于可写的 `/data/proxy-gateways.json`；后台密码单独只读挂载，避免默认只读配置目录导致代理保存失败。
Docker 模式的 Caddy 证书独立保存在 `runtime/local/caddy-data/`。

```powershell
docker compose -f compose.local.yaml logs --tail 100
docker compose -f compose.local.yaml down
```

## 手动账单与报价

Plus 创建结账前，在页面填写付款人的真实姓名、邮箱和地址。创建请求携带 `billing`，上游创建结账后按该地址核算税费。
支付页沿用这份地址；修改地址请重新创建结账。

税费更新只返回成功状态而没有金额时，程序会重新读取同一个结账的报价。报价暂缺不会丢弃已创建的结账：页面显示待报价，可点击“刷新报价”，付款按钮在取得有效金额前保持禁用。
刷新报价复用原结账和原地址，不提交卡片、不确认付款，也不使用固定价格替代上游金额。

## 代理协议与 123Proxy

未注明协议的四段式 `主机:端口:账号:密码` 默认使用 HTTP。SOCKS5 线路应使用 URL 格式：

```text
socks5h://账号+ph:代理密码@residential.123proxy.cn:供应商端口
```

主机、端口和协议以供应商控制台为准；密码中的 `@`、`:`、`#` 等 URL 特殊字符需要编码。
123Proxy 住宅网关识别末尾的小写 `+国家码`，不追加通用 `-region-` / `-sid-` 后缀。
包含 `-sess_12位字母数字_分钟数` 时保留供应商生成的 SESSION；未包含时不在每次建单时擅自添加 SESSION。
网络错误的原有 SID 轮换路径使用供应商的 12 位格式并保留时长和末尾国家码。
地区或 SESSION 变更可能需要同步时间，具体以[供应商说明](https://123proxy.cn/developers/products/residential-rotating-proxy/)为准。

## HTTP 依赖与诊断

`curl_cffi==0.16.1` 支持项目使用的 `chrome150` 等配置。旧版 `0.13.0` 会在发请求前报 `ImpersonateError`；此错误单独报告为本地客户端配置错误，不误报为代理断连。
Sentinel 失败日志区分 Node 缺失、执行超时、脚本输出异常等阶段，并脱敏代理凭据及 Cookie；令牌生成脚本和流程保持原样。
