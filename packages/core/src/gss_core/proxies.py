"""按阶段隔离的动态网关配置；口令只进入受限配置文件与 adapter 入参。"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import string
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ROLES = ("checkout", "approve")
# 供应商写法不一：-region-US / _country_us、-sid-abc / -session-abc。替换时保留原来的键名与大小写。
_REGION = re.compile(r"([-_](?:region|country|area|cc)[-_])([A-Za-z]{2})(?=[-_]|$)", re.I)
_SID = re.compile(r"([-_](?:sid|session|sessid|sessionid)[-_])([A-Za-z0-9]+)(?=[-_]|$)", re.I)
_123_REGION = re.compile(r"\+([A-Za-z]{2})$")
_123_SESSION = re.compile(r"-sess_([A-Za-z0-9]{12})_([0-9]{1,3})$")


def _is_123proxy(host: str) -> bool:
    return host.lower().rstrip(".") == "residential.123proxy.cn"


def _123proxy_parts(username: str):
    country = _123_REGION.search(username)
    body = username[:country.start()] if country else username
    session = _123_SESSION.search(body)
    if "-sess_" in body and (not session or not 1 <= int(session[2]) <= 120):
        raise ValueError("123Proxy SESSION 格式应为 -sess_12位字母数字_分钟数，分钟数为 1–120")
    return body, country, session


def _route_123proxy_username(username: str, region: str, sid: str) -> str:
    """123Proxy: account[-sess_12chars_minutes][+country]; country stays last."""
    body, country, session = _123proxy_parts(username)
    suffix = "+" + region.lower() if region else (country[0].lower() if country else "")
    if sid:
        if not re.fullmatch(r"[A-Za-z0-9]{12}", sid):
            raise ValueError("123Proxy SESSION ID 必须是 12 位字母或数字")
        minutes = session[2] if session else "15"
        base = body[:session.start()] if session else body
        body = f"{base}-sess_{sid}_{minutes}"
    return body + suffix


def _like(old: str, new: str) -> str:
    """新值沿用旧值的大小写风格（供应商常要求小写国家码）。"""
    return new.lower() if old.islower() else new


def route_username(username: str, region: str, sid: str) -> str:
    """只改供应商账号中的地区/SID，保留账号前缀和时间参数。"""
    if region:
        if not _REGION.search(username):
            raise ValueError("账号中没有 -region- 参数，请检查供应商格式")
        username = _REGION.sub(lambda m: m[1] + _like(m[2], region), username)
    if sid:
        if not _SID.search(username):
            raise ValueError("账号中没有 -sid- 参数，请检查供应商格式")
        username = _SID.sub(lambda m: m[1] + sid, username)
    return username


@dataclass
class Gateway:
    host: str = ""
    port: int = 3010
    scheme: str = "http"
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    region: str = ""
    sid: str = ""
    enabled: bool = False

    def validate(self):
        if self.scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError("代理协议必须是 HTTP、HTTPS 或 SOCKS5")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("端口必须是 1 到 65535 的整数")
        if type(self.enabled) is not bool:
            raise ValueError("启用状态格式不正确")
        for value in (self.host, self.username, self.password, self.region, self.sid):
            if not isinstance(value, str) or len(value) > 1024:
                raise ValueError("代理字段格式不正确或过长")
        self.host = self.host.strip().strip("[]")
        if self.enabled and not self.host:
            raise ValueError("请填写网关主机")
        if self.host:
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", self.host):
                    raise ValueError("主机只能填写域名或 IP，不包含协议、端口和账号")
        self.region = self.region.strip().upper()
        if self.region and not re.fullmatch(r"[A-Z]{2}", self.region):
            raise ValueError("地区填写两位国家码，例如 US")
        if self.sid and not re.fullmatch(r"[A-Za-z0-9]{1,64}", self.sid):
            raise ValueError("SID 只能包含字母和数字，最多 64 位")
        if _is_123proxy(self.host):
            self.username = _route_123proxy_username(self.username, self.region, self.sid)
            _, region_match, sid_match = _123proxy_parts(self.username)
            self.region = region_match[1].upper() if region_match else ""
            self.sid = sid_match[1] if sid_match else ""
            return self
        self.username = route_username(self.username, self.region, self.sid)
        region_match, sid_match = _REGION.search(self.username), _SID.search(self.username)
        if region_match:
            self.region = region_match[2].upper()
        if sid_match:
            self.sid = sid_match[2]
        return self

    def proxy_url(self) -> str | None:
        if not self.enabled:
            return None
        auth = ""
        if self.username or self.password:
            auth = quote(self.username, safe="") + ":" + quote(self.password, safe="") + "@"
        host = "[" + self.host + "]" if ":" in self.host else self.host
        return f"{self.scheme}://{auth}{host}:{self.port}"

    def public(self):
        return {
            "host": self.host,
            "port": self.port,
            "scheme": self.scheme,
            "region": self.region,
            "sid": self.sid,
            "enabled": self.enabled,
            "has_username": bool(self.username),
            "has_password": bool(self.password),
            "routing_format": "123proxy" if _is_123proxy(self.host) else "standard",
        }


def proxy_config_path() -> Path:
    return Path(
        os.environ.get(
            "GSS_PROXY_CONFIG",
            str(Path(os.environ.get("GSS_CONFIG_DIR", "config")) / "proxy-gateways.json"),
        )
    )


class GatewayStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else proxy_config_path()
        self.lock = RLock()

    def _read(self):
        if not self.path.exists():
            return {"version": 1}
        with self.path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("代理配置版本不正确")
        return data

    def get(self, role: str) -> Gateway:
        if role not in ROLES:
            raise ValueError("未知代理阶段")
        with self.lock:
            return Gateway(**self._read().get(role, {})).validate()

    def save(self, role: str, values: dict) -> Gateway:
        if role not in ROLES:
            raise ValueError("未知代理阶段")
        if not isinstance(values, dict) or set(values) - {
            "host",
            "port",
            "scheme",
            "username",
            "password",
            "region",
            "sid",
            "enabled",
            "clear_auth",
        }:
            raise ValueError("代理配置字段不正确")
        with self.lock:
            data = self._read()
            existing = Gateway(**data.get(role, {})).validate()
            merged = asdict(existing)
            clear_auth = values.get("clear_auth", False)
            if type(clear_auth) is not bool:
                raise ValueError("清除认证状态格式不正确")
            merged.update(
                {k: v for k, v in values.items() if k not in {"clear_auth", "username", "password"}}
            )
            for key in ("username", "password"):
                supplied = values.get(key, "")
                if not isinstance(supplied, str):
                    raise ValueError("代理认证字段必须是文本")
                if clear_auth:
                    merged[key] = ""
                elif supplied:
                    merged[key] = supplied
            if clear_auth:
                merged["region"] = merged["sid"] = ""
            gateway = Gateway(**merged).validate()
            data[role] = asdict(gateway)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle, temporary = tempfile.mkstemp(prefix=".proxy-", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return gateway


def configured_proxy(role: str) -> str | None:
    """每次读取持久化的该阶段配置，不覆盖另一阶段。"""
    try:
        return GatewayStore().get(role).proxy_url()
    except (OSError, ValueError, TypeError):
        return None


def new_proxy_sid(*, length: int = 8) -> str:
    # GSS_PROXY_SID pins the sticky session (debugging / solver-IP alignment).
    pinned = os.environ.get("GSS_PROXY_SID", "").strip()
    if pinned:
        if length == 12 and not re.fullmatch(r"[A-Za-z0-9]{12}", pinned):
            raise ValueError("123Proxy 的 GSS_PROXY_SID 必须是 12 位字母或数字")
        return pinned
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def rewrite_proxy_exit(proxy: str, *, region: str = "", sid: str = "") -> str:
    """改用户名里的地区 / SID，不写回已保存的配置。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return proxy
    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    hostname = parsed.hostname or ""
    if not username or not hostname:
        return proxy
    region = str(region or "").strip().upper()
    sid = str(sid or "").strip()
    if _is_123proxy(hostname):
        username = _route_123proxy_username(username, region, sid)
    elif region:
        if _REGION.search(username):
            username = _REGION.sub(lambda match: match[1] + _like(match[2], region), username)
        else:
            username = f"{username}-region-{region}"
    if sid and not _is_123proxy(hostname):
        if _SID.search(username):
            username = _SID.sub(lambda match: match[1] + sid, username)
        else:
            username = f"{username}-sid-{sid}"
    host = hostname
    if ":" in hostname and not hostname.startswith("["):
        host = f"[{hostname}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    netloc = f"{auth}{host}"
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))


def rotate_proxy_sid(proxy: str) -> str:
    """只换 SID，地区不变。"""
    length = 12 if _is_123proxy(urlsplit(proxy or "").hostname or "") else 8
    return rewrite_proxy_exit(proxy, sid=new_proxy_sid(length=length))


def session_proxy(role: str, region: str) -> str | None:
    """本次开通专用出口：选定地区 + 随机 SID。"""
    base = configured_proxy(role)
    if not base:
        return None
    # Preserve the supplier-generated SESSION: new 123Proxy routes can take
    # 3–15 minutes to synchronize, so do not invent a session on every order.
    if _is_123proxy(urlsplit(base).hostname or ""):
        return rewrite_proxy_exit(base, region=normalize_region(region))
    return rewrite_proxy_exit(base, region=normalize_region(region), sid=new_proxy_sid())


def proxy_exit_region(proxy: str) -> str:
    """从代理用户名里读当前出口国家码。"""
    parsed = urlsplit(str(proxy or ""))
    username = unquote(parsed.username or "")
    if _is_123proxy(parsed.hostname or ""):
        _, country, _ = _123proxy_parts(username)
        return country[1].upper() if country else ""
    match = _REGION.search(username)
    return match[2].upper() if match else ""


def align_proxy_region(proxy: str | None, country: str) -> str | None:
    """账单国必须等于出口国，否则 Stripe 会拒：Billing country must match request country。"""
    proxy = str(proxy or "").strip()
    country = normalize_region(country)
    if not proxy:
        return None
    if not country or proxy_exit_region(proxy) == country:
        return proxy
    return rewrite_proxy_exit(proxy, region=country)


def normalize_region(region: str) -> str:
    return str(region or "").strip().upper()


__all__ = [
    "ROLES",
    "Gateway",
    "GatewayStore",
    "configured_proxy",
    "route_username",
    "new_proxy_sid",
    "rewrite_proxy_exit",
    "rotate_proxy_sid",
    "session_proxy",
    "proxy_exit_region",
    "align_proxy_region",
]


def parse_proxy_input(raw: str) -> dict:
    """把用户粘贴的代理串拆成 Gateway 字段。

    支持：``scheme://user:pass@host:port``、``user:pass@host:port``、
    ``host:port:user:pass``、``host:port``。账号里的地区 / SID 由 Gateway.validate 读出。
    """
    text = str(raw or "").strip()
    if not text or len(text) > 2048 or any(ch in text for ch in "\r\n\t "):
        raise ValueError("请粘贴一行代理，例如 http://账号:密码@主机:端口")
    scheme = "http"
    if "://" in text:
        parsed = urlsplit(text)
        scheme = (parsed.scheme or "http").lower()
        host, port = parsed.hostname or "", parsed.port
        username, password = unquote(parsed.username or ""), unquote(parsed.password or "")
    elif "@" in text:
        creds, _, endpoint = text.rpartition("@")
        username, _, password = creds.partition(":")
        host, _, port_text = endpoint.rpartition(":")
        port = int(port_text) if port_text.isdigit() else None
    else:
        parts = text.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, username, password = parts[0], int(parts[1]), parts[2], ":".join(parts[3:])
        elif len(parts) == 2 and parts[1].isdigit():
            host, port, username, password = parts[0], int(parts[1]), "", ""
        else:
            raise ValueError("无法识别代理格式，请使用 http://账号:密码@主机:端口")
    if not host or port is None:
        raise ValueError("代理缺少主机或端口")
    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def gateway_from_input(raw: str, *, enabled: bool = True) -> Gateway:
    return Gateway(**parse_proxy_input(raw), enabled=enabled).validate()


def describe_gateway(gateway: Gateway) -> dict:
    """给界面预览用：不含账号密码，标出是否检测到地区 / SID 参数。"""
    data = gateway.public()
    data["has_region_param"] = bool(gateway.region)
    data["has_sid_param"] = bool(gateway.sid)
    return data


__all__ += ["parse_proxy_input", "gateway_from_input", "describe_gateway"]
