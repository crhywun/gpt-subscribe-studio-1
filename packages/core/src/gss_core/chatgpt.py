"""本项目自己的 ChatGPT / Stripe 客户端。建单协议对齐公开的直卡提链实现。"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.billing import address_for
from gss_core.countries import currency_for, locale_for, timezone_for
from gss_core.money import quote_from_payload
from gss_core.redaction import redact_sensitive
from gss_core.sentinel import extract_attestation, extract_build, header_value, mint, so_header_value
from gss_core.session_token import SessionBundle, parse_session_bundle

APP_BASE = "https://chatgpt.com"
ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
STRIPE_BASE = "https://api.stripe.com"
_IMPERSONATE = "chrome150"
_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
_CLIENT_BUILD = "7904904"
# ── 真实 Chrome 146-150 profile（versionhistory.googleapis.com）──────────
_PROFILES = {
    "windows": {
        "builds": ["150.0.7871.189", "149.0.7827.201", "148.0.7778.218",
                   "147.0.7727.139", "146.0.7680.180",
                   "150.0.7871.187", "149.0.7827.199", "148.0.7778.216",
                   "147.0.7727.137", "146.0.7680.178"],
        "platform": "Win32",
        "label": "Windows",
        "screen": (1920, 1080, 1032),
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
        },
    },
    "macos": {
        "builds": ["150.0.7871.189", "149.0.7827.201", "148.0.7778.217",
                   "147.0.7727.139", "146.0.7680.180",
                   "150.0.7871.187", "149.0.7827.199", "148.0.7778.215",
                   "147.0.7727.137", "146.0.7680.177"],
        "platform": "MacIntel",
        "label": "macOS",
        "screen": (1512, 982, 938),
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {
            "sec-ch-ua-platform": '"macOS"',
            "sec-ch-ua-platform-version": '"14.6.1"',
            "sec-ch-ua-arch": '"arm"',
            "sec-ch-ua-bitness": '"64"',
        },
    },
    "linux": {
        "builds": ["150.0.7871.186", "149.0.7827.200", "148.0.7778.215",
                   "147.0.7727.137", "146.0.7680.177",
                   "150.0.7871.181", "149.0.7827.196", "148.0.7778.178",
                   "147.0.7727.116", "146.0.7680.164"],
        "platform": "Linux x86_64",
        "label": "Linux",
        "screen": (1920, 1080, 1053),
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {"sec-ch-ua-platform": '"Linux"'},
    },
}

# 加权：Windows 占真实用户多数
_PROFILE_ORDER = ["windows", "windows", "windows", "macos", "macos", "linux"]


def _hash8(seed: str, salt: str = "") -> int:
    import hashlib as _hl
    return int(_hl.sha256((str(seed) + salt).encode("utf-8")).hexdigest()[:8], 16)


def _pick_profile(device_id: str) -> dict:
    """按 device_id 稳定选定平台与版本，同一账号永远同一组合。"""
    seed = str(device_id or "anon")
    key = _PROFILE_ORDER[_hash8(seed, "os") % len(_PROFILE_ORDER)]
    prof = dict(_PROFILES[key])
    prof["key"] = key
    prof["build"] = prof["builds"][_hash8(seed, "b") % len(prof["builds"])]
    return prof


def _profile_from_ua(ua: str) -> dict:
    """兼容旧调用：从 UA 推导 platform / 屏幕 / 核心数。"""
    low = (ua or "").lower()
    if "windows" in low:
        return {"platform": "Win32", "platform_label": "Windows",
                "screen_w": 1920, "screen_h": 1080, "avail_h": 1032, "cores": 8}
    if "macintosh" in low or "mac os x" in low:
        return {"platform": "MacIntel", "platform_label": "macOS",
                "screen_w": 1512, "screen_h": 982, "avail_h": 938, "cores": 8}
    return {"platform": "Linux x86_64", "platform_label": "Linux",
            "screen_w": 1920, "screen_h": 1080, "avail_h": 1053, "cores": 8}


def _ua_mac(build: str) -> str:
    """兼容旧调用：默认 Linux UA。"""
    return _PROFILES["linux"]["ua"].format(build=build)


_CHROME = _PROFILES["linux"]["ua"].format(build=_PROFILES["linux"]["builds"][0])
_SEC_CH_UA = '"Google Chrome";v="150", "Chromium";v="150", "Not(A:Brand";v="24"'

STRIPE_API_VERSION = "2025-03-31.basil"
STRIPE_CHECKOUT_API_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
logger = logging.getLogger("gss.chatgpt")
_PLAN_NAME = {
    "plus": "chatgptplusplan",
    "go": "chatgptgoplan",
    "pro_lite": "chatgptprolite",
    "pro": "chatgptpro",
    "pro_max": "chatgptpromax",
    "team": "chatgptteamplan",
}
_PLAN_TYPE = {
    "plus": "plus",
    "go": "go",
    "pro_lite": "prolite",
    "pro": "pro",
    "pro_max": "promax",
    "team": "team",
    "chatgptplusplan": "plus",
    "chatgptgoplan": "go",
    "chatgptprolite": "prolite",
    "chatgptpro": "pro",
    "chatgptpromax": "promax",
    "chatgptteamplan": "team",
}

# Stripe Checkout 托管页片段（公开 UI 状态，不是密钥）。Team 长链与 153 同源协议。
_HOSTED_CHECKOUT_FRAGMENT = (
    "#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSdpamZkaWAnPyd%2FbScpJ3ZwZ3Zmd2x1cWxqa1Brb"
    "HRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxO"
    "YHwnPyd1blppbHNgWjA0TUp3VnJGM200a31Cakw2aVFEYldvXFN3fzFhUDZjU0pkZ3xGZk5XNnVnQ"
    "E9icEZTRGl0Rn1hfUZQc2pXbTRdUnJXZGZTbGpzUDZuSU5zdW5vbTJMdG5SNTVsXVR2b2o2aycpJ2"
    "N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyZjY2NjY2MnKSdpZHxqcHFRfHVgJ"
    "z8ndmxrYmlgWmxxYGgnKSdga2RnaWBVaWRmYG1qaWFgd3YnP3F3cGB4JSUl"
)
_CS_ID = re.compile(r"cs_(?:live|test)_[A-Za-z0-9]+")
_OAICS_ID = re.compile(r"oaics_[A-Za-z0-9]+")


def _stripe_device_id() -> str:
    """文档 8.2：guid/muid/sid = UUID + 6 位 hex，共 42 字符。"""
    return str(uuid.uuid4()) + uuid.uuid4().hex[:6]


def _elements_session_client_fields(
    *,
    stripe_js_id: str,
    elements_session_id: str = "",
    locale: str = "en",
) -> Dict[str, str]:
    fields = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale or "en",
        "elements_session_client[is_aggregation_expected]": "false",
    }
    if elements_session_id:
        fields["elements_session_client[session_id]"] = elements_session_id
    return fields


def clamp_team_seats(value: Any) -> int:
    try:
        seats = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Team 席位必须是整数") from exc
    if seats < 2:
        raise ValueError("Team 最少 2 个席位")
    if seats > 999:
        raise ValueError("Team 最多 999 个席位")
    return seats


def _first_id(pattern: re.Pattern[str], *blobs: Any) -> str:
    for blob in blobs:
        if isinstance(blob, dict):
            for value in _walk(blob):
                if isinstance(value, str):
                    match = pattern.search(value)
                    if match:
                        return match.group(0)
        elif isinstance(blob, str):
            match = pattern.search(blob)
            if match:
                return match.group(0)
    return ""


def stripe_checkout_id(*blobs: Any) -> str:
    return _first_id(_CS_ID, *blobs)


def openai_internal_checkout_id(*blobs: Any) -> str:
    """ChatGPT `/payments/checkout/confirm` 只接受内部 oaics_ 会话。"""
    return _first_id(_OAICS_ID, *blobs)


def public_checkout_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http"):
        text = text.rstrip("/").split("/")[-1]
    return text.split("?")[0].split("#")[0]


def stripe_verify_url(
    checkout_id: str,
    *,
    processor: str = "",
    plan: str = "",
    source: str = "chatgpt_web_merged",
) -> str:
    """HAR 里 PI confirm 的 return_url：/checkout/verify?stripe_session_id=oaics_…"""
    session_id = str(checkout_id or "").strip().rstrip("/").split("/")[-1]
    if session_id.startswith("http") or not session_id:
        return f"{APP_BASE}/"
    query = [f"stripe_session_id={session_id}"]
    entity = str(processor or "").strip()
    if entity and entity not in {"verify", "c", "pay"}:
        query.append(f"processor_entity={entity}")
    if source:
        query.append(f"source={source}")
    plan_type = _PLAN_TYPE.get(str(plan), "")
    if plan_type:
        query.append(f"plan_type={plan_type}")
    return f"{APP_BASE}/checkout/verify?{'&'.join(query)}"


def hosted_checkout_url(url: str = "", session_id: str = "") -> str:
    """Team 托管长链：pay.openai.com/c/pay/{cs_live} + Checkout 片段。"""
    value = str(url or "").strip()
    cs_id = session_id if str(session_id).startswith("cs_") else stripe_checkout_id(value, session_id)
    if value.startswith("https://checkout.stripe.com/c/pay/"):
        value = "https://pay.openai.com" + value[len("https://checkout.stripe.com") :]
    if value.startswith("https://pay.openai.com/c/pay/"):
        if "#" not in value:
            return value + _HOSTED_CHECKOUT_FRAGMENT
        return value
    if cs_id:
        return f"https://pay.openai.com/c/pay/{cs_id}{_HOSTED_CHECKOUT_FRAGMENT}"
    return value


# 浏览器抓包里 payments/checkout 与 checkout/confirm 请求都带这组前端标识。
_CODEX_WEB_HEADERS = {
    "originator": "Codex Browser",
    "x-openai-web-frontend": "codex_webview",
    "x-openai-codex-window-type": "browser",
    "x-oai-mcp-form-version": "2",
    "x-openai-checkout-source": "chatgpt_web_merged",
}


def _account_check_item(payload: Dict[str, Any], account_id: str = "") -> tuple[Dict[str, Any], str]:
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")
    requested = str(account_id or "").strip()
    if requested and isinstance(accounts.get(requested), dict):
        return accounts[requested], requested
    default = accounts.get("default")
    if isinstance(default, dict):
        account = default.get("account")
        return default, str((account or {}).get("account_id") or "default")
    for key, value in accounts.items():
        if key != "default" and isinstance(value, dict):
            return value, str(key)
    raise ValueError("未找到可解析的账号条目")


def parse_account_check(payload: Dict[str, Any], *, account_id: str = "") -> Dict[str, Any]:
    """解析 accounts/check/v4 响应，保留可用于 checkout 的活动 id。"""
    item, account_key = _account_check_item(payload, account_id)
    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    campaigns = item.get("eligible_promo_campaigns") or {}
    if not isinstance(campaigns, dict):
        campaigns = {}
    plan_type = str(account.get("plan_type") or "").strip().lower()
    subscription_plan = str(entitlement.get("subscription_plan") or "").strip().lower()
    is_free = plan_type == "free" or subscription_plan == "chatgptfreeplan"
    parsed_campaigns = []
    for key, raw in campaigns.items():
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata") or {}
        discount = metadata.get("discount") or {}
        duration = metadata.get("duration") or {}
        parsed_campaigns.append(
            {
                "key": str(key),
                "id": str(raw.get("id") or ""),
                "discount_percentage": discount.get("percentage"),
                "duration_num_periods": duration.get("num_periods"),
                "duration_period": duration.get("period"),
                "title": metadata.get("title"),
                "summary": metadata.get("summary"),
                "promotion_type": metadata.get("promotion_type_label"),
            }
        )
    if not is_free:
        parsed_campaigns = []
    plus_campaigns = [campaign for campaign in parsed_campaigns if campaign["key"] == "plus"]
    return {
        "account_id": str(account.get("account_id") or account_key or ""),
        "current_plan_type": plan_type or None,
        "subscription_plan": subscription_plan or None,
        "is_free": is_free,
        "plus_eligible": bool(is_free and plus_campaigns),
        "campaigns": parsed_campaigns,
    }


def _checkout_body(
    *,
    plan: str,
    country: str,
    currency: str,
    promo: bool = False,
    promo_code: str = "",
    promo_campaign_id: str = "",
    workspace_name: str = "",
    seat_quantity: int = 5,
    price_interval: str = "month",
) -> Dict[str, Any]:
    if str(plan) == "team":
        seats = clamp_team_seats(seat_quantity)
        interval = "year" if str(price_interval).lower() == "year" else "month"
        body: Dict[str, Any] = {
            "entry_point": "team_workspace_purchase_modal",
            "plan_name": "chatgptteamplan",
            "billing_details": {"country": country, "currency": currency},
            "cancel_url": f"{APP_BASE}/",
            "checkout_ui_mode": "redirect",
            "check_card_proxy": True,
            "team_plan_data": {
                "workspace_name": (workspace_name or "Team Workspace")[:80],
                "price_interval": interval,
                "seat_quantity": seats,
            },
        }
        code = str(promo_code or "").strip()
        if code:
            body["promo_code"] = code[:80]
        return body
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": _PLAN_NAME.get(str(plan), "chatgptplusplan"),
        "billing_details": {"country": country, "currency": currency},
        "checkout_source": "chatgpt_web_merged",
        "supports_saved_payment_link": True,
        "checkout_appearance": "light",
        "cancel_url": f"{APP_BASE}/?cta_tab=personal",
        "purchase_attempt_id": str(uuid.uuid4()),
        "checkout_ui_mode": "custom",
    }
    campaign_id = str(promo_campaign_id or "").strip()[:120]
    if promo or campaign_id:
        body["promo_campaign"] = {
            "promo_campaign_id": campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    # Personal Plus/Go campaigns use promo_campaign_id. Keep promo_code for the
    # hosted Team flow, whose checkout accepts an explicit code field.
    return body


class CheckoutAmountChanged(DomainError):
    """A quote changed before any checkout confirmation was submitted."""
    def __init__(self, amount_minor: int, currency: str):
        super().__init__(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账金额已变化，请重新确认")
        self.amount_minor = amount_minor
        self.currency = currency


@dataclass
class CheckoutSession:
    checkout_session_id: str
    publishable_key: str
    account_id: str
    checkout_url: str
    currency: str
    amount: Optional[Decimal] = None
    processor_entity: str = ""
    attestation: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    customer_session_client_secret: str = ""
    checkout_kind: str = "custom"
    confirm_return_url: str = ""
    billing: Optional[Dict[str, str]] = None
    quote_error: str = ""


def jwt_account_id(access_token: str) -> str:
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        value = auth.get("chatgpt_account_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _new_session(*, proxy: Optional[str], timeout: float, impersonate: str = _IMPERSONATE):
    try:
        from curl_cffi.requests import Session as CurlSession
    except ImportError as exc:  # pragma: no cover
        raise DomainError(
            ErrorCode.CHECKOUT_UPSTREAM_ERROR,
            "缺少 curl_cffi，无法用浏览器指纹请求 ChatGPT",
        ) from exc
    try:
        session = CurlSession(impersonate=impersonate, timeout=timeout)
    except Exception:
        session = CurlSession(impersonate="chrome150", timeout=timeout)
    if hasattr(session, "trust_env"):
        session.trust_env = False
    proxy = str(proxy or "").strip()
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}
    return session


@dataclass
class ChatGPTClient:
    access_token: str
    proxy: Optional[str] = None
    timeout: float = 90.0
    account_id: str = ""
    attestation: str = ""
    client_build_number: str = _CLIENT_BUILD
    client_version: str = _CLIENT_VERSION
    oai_session_id: str = ""
    session_token: str = ""
    device_id: str = ""
    extra_cookies: Dict[str, str] = field(default_factory=dict)
    language: str = "en-US"
    timezone: str = "America/New_York"
    sentinel_token: str = ""
    sentinel_so: str = ""
    _http: Any = field(default=None, repr=False)
    _stripe_http: Any = field(default=None, repr=False)
    _stripe_js_id: str = field(default="", repr=False)
    _elements_session_id: str = field(default="", repr=False)

    @classmethod
    def from_raw_session(cls, raw: str, proxy: Optional[str] = None) -> "ChatGPTClient":
        bundle: SessionBundle = parse_session_bundle(raw)
        token = bundle.access_token
        # 只粘贴 session token 时 accessToken 为空，改用 session token 派生设备标识，避免所有用户撞同一个 id。
        seed = token or bundle.session_token
        device = bundle.device_id or str(uuid.uuid5(uuid.NAMESPACE_URL, "dcp-did:" + seed))
        session_id = bundle.oai_session_id or str(uuid.uuid5(uuid.NAMESPACE_URL, "dcp-sid:" + seed))
        return cls(
            access_token=token,
            proxy=proxy,
            account_id=jwt_account_id(token),
            session_token=bundle.session_token,
            device_id=device,
            extra_cookies=dict(bundle.cookies),
            oai_session_id=session_id,
        )

    def _cookie_header(self) -> str:
        values: Dict[str, str] = dict(self.extra_cookies)
        if self.session_token:
            values.setdefault("__Secure-next-auth.session-token", self.session_token)
        if self.device_id:
            values["oai-did"] = self.device_id
        return "; ".join(f"{name}={value}" for name, value in values.items() if name and value is not None)

    @property
    def profile(self) -> dict:
        return _pick_profile(getattr(self, "device_id", "") or "")

    @property
    def chrome_build(self) -> str:
        return self.profile["build"]

    @property
    def chrome_major(self) -> str:
        return self.chrome_build.split(".", 1)[0]

    @property
    def sec_ch_ua(self) -> str:
        major = self.chrome_major
        return ('"Google Chrome";v="' + major + '", "Chromium";v="' + major
                + '", "Not(A:Brand";v="24"')

    @property
    def ua(self) -> str:
        return self.profile["ua"].format(build=self.chrome_build)

    @property
    def sentinel_profile(self) -> dict:
        p = self.profile
        return {"platform": p["platform"], "platform_label": p["label"],
                "screen_w": p["screen"][0], "screen_h": p["screen"][1],
                "avail_h": p["screen"][2], "cores": 8}

    def _identity_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Language": f"{self.language},{self.language.split('-')[0]};q=0.9,en;q=0.8",
            "Authorization": f"Bearer {self.access_token}",
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/",
            "Content-Type": "application/json",
            "oai-did": self.device_id,
            "oai-device-id": self.device_id,
            "oai-language": self.language,
            "oai-telemetry": "production",
            "oai-session-id": self.oai_session_id,
            "oai-client-version": self.client_version,
            "oai-client-build-number": self.client_build_number,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            **self.profile["hints"],
            "Cookie": self._cookie_header(),
        }
        if not self.access_token:
            # 仅凭 session cookie 换 token 时不发空的 Bearer。
            headers.pop("Authorization")
        if self.account_id:
            headers["chatgpt-account-id"] = self.account_id
        return headers

    def _route_headers(self, route: str, *, page_url: str = "") -> Dict[str, str]:
        headers = {
            "Referer": page_url or f"{APP_BASE}/",
            "x-openai-target-path": route,
            "x-openai-target-route": route,
        }
        if route.startswith("/backend-api/payments/"):
            headers.update(_CODEX_WEB_HEADERS)
        if route.startswith("/backend-api/payments/") and self.attestation:
            headers["oai-web-deployment-attestation"] = self.attestation
            headers["OAI-Web-Deployment-Attestation"] = self.attestation
        if self.sentinel_token:
            headers["openai-sentinel-token"] = self.sentinel_token
            headers["OpenAI-Sentinel-Token"] = self.sentinel_token
        if self.sentinel_so:
            headers["openai-sentinel-so-token"] = self.sentinel_so
            headers["OpenAI-Sentinel-SO-Token"] = self.sentinel_so
        return headers

    def _merge_cookies(self) -> None:
        jar = getattr(self.http, "cookies", None)
        if jar is None:
            return
        gathered: Dict[str, str] = {}
        try:
            getter = getattr(jar, "get_dict", None)
            if callable(getter):
                gathered.update(getter() or {})
                for domain in ("chatgpt.com", ".chatgpt.com"):
                    try:
                        gathered.update(getter(domain=domain) or {})
                    except TypeError:
                        break
                    except Exception:
                        continue
            elif hasattr(jar, "items"):
                gathered.update({str(k): str(v) for k, v in jar.items()})
        except Exception:
            gathered = {}
        for name, value in gathered.items():
            if name and value is not None:
                self.extra_cookies[str(name)] = str(value)
        self.http.headers.update(self._identity_headers())

    def refresh_session(self) -> bool:
        """用当前出口 IP 请求 /api/auth/session，换取与本批次出口一致的 accessToken 与 cookie。

        结账地区由出口代理决定（align_proxy_region），session 本身不绑定国家；
        刷新的目的是让本次请求携带的 accessToken / oai-sc 等 cookie 与出口 IP
        同批次，降低 Cloudflare 403 与 unusual activity 概率。

        软失败：session-token 缺失/失效、上游报错都返回 False，调用方沿用原
        session 继续，不阻断结账。刷新结果只在内存，不落库。
        """
        if not self.session_token:
            return False
        try:
            response = self.http.get(
                f"{APP_BASE}/api/auth/session",
                headers=self._route_headers("/api/auth/session"),
                timeout=self.timeout,
            )
        except Exception as exc:
            logger.warning("session 刷新失败（继续用原 session）: %s", redact_sensitive(str(exc))[:200])
            return False
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            logger.warning("session 刷新失败（HTTP %s），继续用原 session", status)
            return False
        payload = _json(response)
        token = str(payload.get("accessToken") or "").strip()
        if not token:
            # session-token 已失效时上游返回 200 + 空 accessToken。
            logger.warning("session 刷新未返回 accessToken（session-token 可能已失效），继续用原 session")
            return False
        self.access_token = token
        self._merge_cookies()
        logger.info("session 刷新成功，已与本批次出口对齐")
        return True

    def warmup(self, *, page_url: str = "") -> None:
        url = page_url or f"{APP_BASE}/"
        headers = self._identity_headers()
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers["Referer"] = url
        headers["sec-fetch-dest"] = "document"
        headers["sec-fetch-mode"] = "navigate"
        headers["upgrade-insecure-requests"] = "1"
        headers.pop("Content-Type", None)
        try:
            home = self.http.get(url, headers=headers, timeout=self.timeout)
        except Exception:
            return
        html = str(getattr(home, "text", "") or "")
        version, build, session_id = extract_build(html)
        if version:
            self.client_version = version
        if build:
            self.client_build_number = build
        if session_id:
            self.oai_session_id = session_id
        attestation = extract_attestation(html)
        if attestation:
            self.attestation = attestation
        self._merge_cookies()

    def mint_sentinel(self, *, flow: str, page_url: str = "") -> None:
        token = mint(
            flow=flow,
            device_id=self.device_id,
            user_agent=self.ua,
            proxy=self.proxy or "",
            page_url=page_url or f"{APP_BASE}/",
            language=self.language,
            timezone=self.timezone,
            cookie_header=self._cookie_header(),
            timeout_s=max(30.0, min(self.timeout, 90.0)),
            **self.sentinel_profile,
        )
        value = header_value(token)
        if not value:
            from .sentinel import last_failure
            reason = last_failure().get("reason", "unknown")
            labels = {"node_missing": "未找到 Node", "node_timeout": "Node 执行超时",
                      "assets_missing": "缺少本地脚本", "bridge_error": "脚本或上游响应异常",
                      "empty_output": "Node 未返回结果", "invalid_bridge_json": "脚本输出格式异常"}
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                f"无法取得 ChatGPT 风控令牌（Sentinel）：{labels.get(reason, '返回内容无效')}。请查看本机 Sentinel 诊断日志。",
                detail={"stage": "sentinel", "reason": reason},
            )
        self.sentinel_token = value
        self.sentinel_so = so_header_value(token)

    def prepare_risk(self, *, flow: str, page_url: str = "", country: str = "") -> None:
        if country:
            self.language = locale_for(country)
            self.timezone = timezone_for(country)
        url = page_url or f"{APP_BASE}/"
        self.warmup(page_url=url)
        self.mint_sentinel(flow=flow, page_url=url)

    @property
    def http(self):
        if self._http is None:
            self._http = _new_session(proxy=self.proxy, timeout=self.timeout)
            jar = getattr(self._http, "cookies", None)
            setter = getattr(jar, "set", None) if jar is not None else None
            if callable(setter):
                cookies = dict(self.extra_cookies)
                if self.session_token:
                    cookies.setdefault("__Secure-next-auth.session-token", self.session_token)
                if self.device_id:
                    cookies["oai-did"] = self.device_id
                for name, value in cookies.items():
                    if not name or value is None:
                        continue
                    try:
                        setter(str(name), str(value), domain="chatgpt.com")
                    except TypeError:
                        try:
                            setter(str(name), str(value))
                        except Exception:
                            continue
                    except Exception:
                        continue
            self._http.headers.update(self._identity_headers())
        return self._http

    @property
    def stripe_http(self):
        if self._stripe_http is None:
            self._stripe_http = _new_session(proxy=self.proxy, timeout=self.timeout)
            self._stripe_http.headers.update(
                {
                    "User-Agent": self.ua,
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            )
        return self._stripe_http

    def close(self) -> None:
        for handle in (self._http, self._stripe_http):
            if handle is not None:
                closer = getattr(handle, "close", None)
                if callable(closer):
                    closer()
        self._http = None
        self._stripe_http = None

    def _raise(self, code: ErrorCode, response: Any, prefix: str) -> None:
        body = redact_sensitive(str(getattr(response, "text", ""))[:500])
        status = int(getattr(response, "status_code", 0) or 0)
        lowered = body.lower()
        if status == 401:
            raise DomainError(ErrorCode.INVALID_SESSION, f"{prefix}: session 无效或已过期")
        if "unusual activity" in lowered:
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "ChatGPT 判定异常流量。请换一个同国家 SID 节点，"
                "或用与账单国家相同网络导出的新 session 再试。",
            )
        if "billing country must match" in lowered:
            raise DomainError(
                ErrorCode.BILLING_COUNTRY_MISMATCH,
                "账单国家必须与出口代理国家一致，请用所选地区的 SID 重试。",
            )
        if "already paid" in lowered:
            raise DomainError(code, f"{prefix}: 该账号已是付费订阅")
        if "only for internal checkout sessions" in lowered:
            raise DomainError(
                code,
                "ChatGPT 内部确认只接受 oaics_ 会话。当前是 Stripe cs_ 自定义结账，不能调用 /payments/checkout/confirm。",
            )
        if status == 403:
            raise DomainError(code, f"{prefix}: 被拦截（HTTP 403），请检查出口代理是否可用")
        if status >= 500:
            raise DomainError(code, f"{prefix}失败：ChatGPT 暂时返回 {status}，请换档位或稍后重试")
        raise DomainError(code, f"{prefix}: HTTP {status} {body}")

    def hydrate(self) -> None:
        if not self.account_id:
            self.account_id = jwt_account_id(self.access_token)
        if self.account_id:
            return
        me = self.http.get(
            f"{APP_BASE}/backend-api/me",
            headers=self._route_headers("/backend-api/me"),
            timeout=self.timeout,
        )
        if int(getattr(me, "status_code", 0) or 0) == 401:
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if int(getattr(me, "status_code", 0) or 0) >= 400:
            check = self.http.get(
                f"{APP_BASE}{ACCOUNTS_CHECK_PATH}",
                params={"timezone_offset_min": "0"},
                headers=self._route_headers(ACCOUNTS_CHECK_PATH),
                timeout=self.timeout,
            )
            if int(getattr(check, "status_code", 0) or 0) == 401:
                raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
            if int(getattr(check, "status_code", 0) or 0) >= 400:
                self._raise(ErrorCode.INVALID_SESSION, me, "读取账号")
            payload = _json(check)
        else:
            payload = _json(me)
        self.account_id = _account_id(payload)
        if not self.account_id:
            raise DomainError(ErrorCode.INVALID_SESSION, "无法从 session 解析账号 id")
        self.http.headers.update(self._identity_headers())

    def promo_eligibility(self, *, timezone_offset_min: int = 0) -> Dict[str, Any]:
        """读取 v4 账号检查并返回可安全展示/复用的活动摘要。"""
        if not self.account_id:
            self.hydrate()
        response = self.http.get(
            f"{APP_BASE}{ACCOUNTS_CHECK_PATH}",
            params={"timezone_offset_min": str(int(timezone_offset_min))},
            headers=self._route_headers(ACCOUNTS_CHECK_PATH),
            timeout=self.timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if status >= 400:
            self._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "查询优惠资格")
        return parse_account_check(_json(response), account_id=self.account_id)

    def create_checkout(
        self,
        *,
        plan: str,
        country: str,
        promo: bool = False,
        promo_code: str = "",
        promo_campaign_id: str = "",
        workspace_name: str = "",
        seat_quantity: int = 5,
        price_interval: str = "month",
        billing: Optional[Dict[str, str]] = None,
    ) -> CheckoutSession:
        country = country.upper()
        currency = currency_for(country)
        hosted = str(plan) == "team"
        campaign_id = str(promo_campaign_id or "").strip()[:120]
        page_url = (
            f"{APP_BASE}/?promo_campaign={quote(campaign_id or 'plus-1-month-free', safe='')}"
            if promo or campaign_id
            else f"{APP_BASE}/"
        )
        self.prepare_risk(flow="chatgpt_checkout", page_url=page_url, country=country)
        if not self.account_id:
            self.hydrate()
        if self._http is not None:
            self._http.headers.update(self._identity_headers())
        body = _checkout_body(
            plan=plan,
            country=country,
            currency=currency,
            promo=promo,
            promo_code=promo_code,
            promo_campaign_id=campaign_id,
            workspace_name=workspace_name,
            seat_quantity=seat_quantity,
            price_interval=price_interval,
        )
        response = self.http.post(
            f"{APP_BASE}/backend-api/payments/checkout",
            json=body,
            headers=self._route_headers("/backend-api/payments/checkout", page_url=page_url),
            timeout=self.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) == 401:
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "创建结账")
        payload = _json(response)
        raw_url = str(payload.get("url") or payload.get("checkout_url") or "").strip()
        checkout_id = public_checkout_id(
            payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id") or ""
        )
        stripe_id = stripe_checkout_id(payload, raw_url, checkout_id)
        internal_id = openai_internal_checkout_id(payload, raw_url, checkout_id)
        if hosted:
            url = hosted_checkout_url(raw_url, stripe_id or checkout_id)
            if not url:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未拿到 Team 托管长链")
            session_id = stripe_id or checkout_id
        else:
            # 官方晚间 SPA 常把 cs_live 放在顶层 id；内部 confirm 仍要 oaics_（若响应里有）。
            session_id = internal_id or checkout_id or stripe_id
            if not session_id:
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    f"结账响应缺少 session id: {redact_sensitive(str(payload)[:240])}",
                )
            processor = str(payload.get("processor_entity") or _processor(country))
            path_id = stripe_id if (session_id.startswith("oaics_") and stripe_id) else session_id
            url = raw_url if raw_url.startswith("http") else f"{APP_BASE}/checkout/{processor}/{path_id}"
        processor = str(payload.get("processor_entity") or _processor(country))
        logger.info(
            "checkout_session_kind=%s processor=%s hosted=%s",
            session_id.split("_", 1)[0] + "_" if "_" in session_id else "?",
            processor,
            hosted,
        )
        confirm_return = str(payload.get("confirm_return_url") or "").strip()
        if not hosted and not confirm_return:
            confirm_return = stripe_verify_url(session_id, processor=processor, plan=plan)
        pk = str(payload.get("publishable_key") or payload.get("publishableKey") or "").strip()
        if not pk.startswith(("pk_live_", "pk_test_")):
            try:
                pk = self._publishable_key()
            except DomainError:
                # 托管长链拿不到公钥时降级为纯链接模式（用户手动去 Stripe 页支付）；
                # custom 会话没有公钥就无法支付，必须抛错。
                if not hosted:
                    raise
                pk = ""
        page = url if str(url).startswith("http") else page_url
        amount, quote_currency = quote_from_payload(payload, currency)
        quote_billing = dict(billing) if billing is not None else None
        quote_error = ""
        if not hosted:
            refreshed, refreshed_currency = self._quote_from_context(
                processor=processor, checkout_id=session_id, page_url=page, currency=quote_currency or currency
            )
            if refreshed is not None:
                amount, quote_currency = refreshed, refreshed_currency
            quote_billing = quote_billing if quote_billing is not None else address_for(country)
            try:
                taxed, taxed_currency = self._quote_from_taxes(
                    checkout_id=session_id,
                    page_url=page,
                    currency=quote_currency or currency,
                    billing=quote_billing,
                    processor=processor,
                    strict=billing is not None,
                )
                if taxed is not None:
                    amount, quote_currency = taxed, taxed_currency
            except DomainError as exc:
                # The upstream checkout already exists. Keep it available instead
                # of discarding it just because its updated quote is unavailable.
                if billing is None:
                    raise
                amount = None
                quote_error = exc.message
        customer_session = str(
            payload.get("customer_session_client_secret")
            or payload.get("customerSessionClientSecret")
            or ""
        ).strip()
        return CheckoutSession(
            checkout_session_id=session_id,
            publishable_key=pk,
            account_id=self.account_id,
            checkout_url=url,
            currency=quote_currency or currency,
            amount=amount,
            processor_entity=processor,
            attestation=self.attestation,
            client_build_number=self.client_build_number,
            client_version=self.client_version,
            oai_session_id=self.oai_session_id,
            customer_session_client_secret=customer_session,
            checkout_kind="hosted" if hosted else "custom",
            confirm_return_url="" if hosted else confirm_return,
            billing=quote_billing,
            quote_error=quote_error,
        )

    def _publishable_key(self) -> str:
        response = self.http.get(
            f"{APP_BASE}/backend-api/payments/stripe_client_bootstrap",
            params={"account_id": self.account_id},
            headers=self._route_headers("/backend-api/payments/stripe_client_bootstrap"),
            timeout=self.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "读取 Stripe 公钥")
        payload = _json(response)
        key = str(payload.get("publishable_key") or payload.get("publishableKey") or "")
        if not key.startswith(("pk_live_", "pk_test_")):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未拿到 Stripe 公钥")
        return key

    def _quote_from_context(
        self, *, processor: str, checkout_id: str, page_url: str, currency: str
    ) -> tuple[Optional[Decimal], str]:
        entities: list[str] = []
        for entity in (processor, "openai_llc", "openai_ie"):
            if entity and entity not in entities:
                entities.append(entity)
        routes = [f"/backend-api/payments/checkout/{entity}/{checkout_id}" for entity in entities]
        routes.append(f"/backend-api/payments/checkout/{checkout_id}")
        for route in routes:
            try:
                response = self.http.get(
                    f"{APP_BASE}{route}",
                    headers=self._route_headers(route, page_url=page_url),
                    timeout=self.timeout,
                )
            except Exception:
                continue
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                continue
            amount, found = quote_from_payload(_json(response), currency)
            if amount is not None:
                return amount, found or currency
        return None, currency

    def _quote_from_taxes(
        self,
        *,
        checkout_id: str,
        page_url: str,
        currency: str,
        billing: Optional[Dict[str, str]] = None,
        processor: str = "",
        strict: bool = False,
    ) -> tuple[Optional[Decimal], str]:
        try:
            response = self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/taxes",
                json=_taxes_body(
                    checkout_id=checkout_id,
                    billing=billing,
                    processor=processor,
                    currency=currency,
                ),
                headers=self._route_headers("/backend-api/payments/checkout/taxes", page_url=page_url),
                timeout=self.timeout,
            )
        except Exception as exc:
            if strict:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "按账单地址请求报价失败，请重试创建结账") from exc
            return None, currency
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            if strict:
                self._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "按账单地址核算税费")
            return None, currency
        amount, found_currency = quote_from_payload(_json(response), currency)
        if strict and amount is None:
            # Some successful tax updates are acknowledgments without a total.
            # Retrieve the same checkout after the update to obtain its current quote.
            amount, found_currency = self._quote_from_context(
                processor=processor, checkout_id=checkout_id, page_url=page_url, currency=currency,
            )
        if strict and amount is None:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账已创建，上游报价暂未取得，请刷新报价后确认金额")
        return amount, found_currency

    def prepare_setup_intent(self) -> tuple[str, str]:
        if not self.account_id:
            self.hydrate()
        response = self.http.post(
            f"{APP_BASE}/backend-api/payments/payment_method",
            json={"account_id": self.account_id},
            headers=self._route_headers("/backend-api/payments/payment_method"),
            timeout=self.timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if status >= 400:
            self._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "准备支付方式")
        return setup_intent_from_payload(_json(response))

    def confirm_setup_intent(
        self, *, setup_id: str, client_secret: str, payment_method: str, publishable_key: str
    ) -> Dict[str, Any]:
        body = {
            "key": publishable_key,
            "client_secret": client_secret,
            "payment_method": payment_method,
        }
        response = self.stripe_http.post(
            f"{STRIPE_BASE}/v1/setup_intents/{setup_id}/confirm",
            data=urlencode(body),
            timeout=self.timeout,
        )
        payload = _json(response)
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            message = (
                (payload.get("error") or {}).get("message")
                if isinstance(payload.get("error"), dict)
                else payload.get("message")
            )
            text = str(message or f"Stripe confirm HTTP {response.status_code}")
            if "billing country must match" in text.lower():
                raise DomainError(
                    ErrorCode.BILLING_COUNTRY_MISMATCH,
                    "账单国家必须与出口代理国家一致，请用所选地区的 SID 重试。",
                )
            raise DomainError(ErrorCode.PAYMENT_DECLINED, redact_sensitive(text))
        return payload

    def create_confirmation_token(
        self,
        *,
        publishable_key: str,
        payment_method: str = "",
        setup_future_usage: Optional[str] = "off_session",
        card: Optional[Dict[str, Any]] = None,
        billing: Optional[Dict[str, Any]] = None,
        radar_token: str = "",
        time_on_page_ms: int = 0,
        client_session_id: str = "",
        customer_id: str = "",
        currency: str = "",
    ) -> str:
        """创建 confirmation token。

        ``setup_future_usage`` 必须与确认时的 ``save_payment_method_for_reuse``
        配对，否则上游返回 ``Card save choice does not match confirmation token``：

            off_session + true     表示保存卡片
            None        + 不带该字段  表示不保存
        """
        body: Dict[str, Any] = {
            "key": publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
        }
        if setup_future_usage:
            body["setup_future_usage"] = setup_future_usage

        if card and card.get("number"):
            # 对齐真实浏览器（HAR）：卡号直接交给 confirmation_tokens，
            # 而不是先造 pm_ 再传引用。
            b = dict(billing or {})
            import uuid as _uuid
            _csid = client_session_id or str(_uuid.uuid4())
            body["payment_method_data[type]"] = "card"
            body["payment_method_data[card][number]"] = str(card.get("number") or "")
            body["payment_method_data[card][cvc]"] = str(card.get("cvc") or "")
            body["payment_method_data[card][exp_month]"] = str(card.get("exp_month") or "")
            body["payment_method_data[card][exp_year]"] = str(card.get("exp_year") or "")
            body["payment_method_data[allow_redisplay]"] = "always"
            body["payment_method_data[pasted_fields]"] = "number,exp"
            body["payment_method_data[billing_details][name]"] = str(b.get("name") or "")
            body["payment_method_data[billing_details][email]"] = str(b.get("email") or "")
            body["payment_method_data[billing_details][address][country]"] = str(b.get("country") or "US").upper()
            body["payment_method_data[billing_details][address][line1]"] = str(b.get("line1") or "")
            body["payment_method_data[billing_details][address][city]"] = str(b.get("city") or "")
            body["payment_method_data[billing_details][address][postal_code]"] = str(
                b.get("postal_code") or b.get("zip") or "")
            body["payment_method_data[billing_details][address][state]"] = str(b.get("state") or "")
            body["payment_method_data[payment_user_agent]"] = (
                "stripe.js/7e9062bdeb; stripe-js-v3/7e9062bdeb; payment-element; deferred")
            body["payment_method_data[referrer]"] = APP_BASE
            body["payment_method_data[time_on_page]"] = str(int(time_on_page_ms or 45000))
            body["payment_method_data[client_attribution_metadata][client_session_id]"] = _csid
            body["payment_method_data[client_attribution_metadata][merchant_integration_source]"] = "elements"
            body["payment_method_data[client_attribution_metadata][merchant_integration_subtype]"] = "payment-element"
            body["payment_method_data[client_attribution_metadata][merchant_integration_version]"] = "2021"
            body["payment_method_data[client_attribution_metadata][payment_intent_creation_flow]"] = "deferred"
            body["payment_method_data[client_attribution_metadata][payment_method_selection_flow]"] = "merchant_specified"
            body["payment_method_data[guid]"] = uuid.uuid4().hex
            body["payment_method_data[muid]"] = uuid.uuid4().hex
            body["payment_method_data[sid]"] = uuid.uuid4().hex
            if radar_token:
                body["payment_method_data[radar_options][hcaptcha_token]"] = radar_token
            body["client_attribution_metadata[client_session_id]"] = _csid
            body["client_attribution_metadata[merchant_integration_source]"] = "elements"
            body["client_attribution_metadata[merchant_integration_subtype]"] = "payment-element"
            body["client_attribution_metadata[merchant_integration_version]"] = "2021"
            body["client_context[mode]"] = "subscription"
            body["client_context[payment_method_types][0]"] = "card"
            if currency:
                body["client_context[currency]"] = currency.lower()
            if customer_id:
                body["client_context[customer]"] = customer_id
        elif payment_method:
            body["payment_method"] = payment_method
        response = self.stripe_http.post(
            f"{STRIPE_BASE}/v1/confirmation_tokens",
            data=urlencode(body),
            timeout=self.timeout,
        )
        payload = _json(response)
        token = str(payload.get("id") or "")
        if int(getattr(response, "status_code", 0) or 0) >= 400 or not token:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            message = str(error.get("message") or payload.get("message") or "创建 confirmation token 失败")
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, redact_sensitive(message))
        return token

    def _sync_billing_tax(
        self,
        *,
        session_id: str,
        page_url: str,
        billing: Optional[Dict[str, str]],
        processor: str,
        currency: str,
        strict: bool,
    ) -> Optional[int]:
        self.mint_sentinel(flow="checkout_session_approval", page_url=page_url)
        taxed: Dict[str, Any] = {}
        try:
            response = self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/taxes",
                json=_taxes_body(
                    checkout_id=session_id,
                    billing=billing,
                    processor=processor,
                    currency=currency,
                ),
                headers=self._route_headers("/backend-api/payments/checkout/taxes", page_url=page_url),
                timeout=self.timeout,
            )
            taxed = _json(response)
            session = taxed.get("checkout_session") if isinstance(taxed.get("checkout_session"), dict) else {}
            logger.info(
                "checkout_taxes status=%s amount_total=%s billing_country=%s",
                getattr(response, "status_code", 0),
                session.get("amount_total") or (taxed.get("snapshot") or {}).get("amount_total"),
                (billing or {}).get("country", ""),
            )
        except Exception as exc:
            if strict:
                raise
            logger.warning("checkout_taxes 失败（继续确认）: %s", redact_sensitive(str(exc))[:200])
        try:
            self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/update_tax_id",
                json={"checkout_session_id": session_id, "tax_id_info": None},
                headers=self._route_headers(
                    "/backend-api/payments/checkout/update_tax_id", page_url=page_url
                ),
                timeout=self.timeout,
            )
        except Exception:
            pass
        return expected_amount_minor(taxed)

    def confirm_checkout(
        self,
        *,
        checkout_session_id: str,
        confirm_token: str,
        checkout_url: str = "",
        billing: Optional[Dict[str, str]] = None,
        processor_entity: str = "",
        currency: str = "",
        publishable_key: str = "",
        save_choice: Optional[bool] = None,
        expected_amount_minor: Optional[int] = None,
    ) -> Dict[str, Any]:
        session_id = public_checkout_id(checkout_session_id)
        page_url = checkout_url or f"{APP_BASE}/checkout/{session_id}"
        processor = processor_entity or _processor_from_checkout_url(checkout_url)
        # 文档 2.3：taxes snapshot.amount_total 才是 Confirm 的 expected_amount。
        tax_minor = self._sync_billing_tax(
            session_id=session_id,
            page_url=page_url,
            billing=billing,
            processor=processor,
            currency=currency,
            strict=not session_id.startswith("cs_"),
        )
        if expected_amount_minor is not None:
            if tax_minor is None:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未读到最终税费报价，尚未提交确认")
            if tax_minor != expected_amount_minor:
                raise CheckoutAmountChanged(tax_minor, currency)
        if session_id.startswith("cs_"):
            return self.confirm_stripe_custom_checkout(
                checkout_session_id=session_id,
                publishable_key=publishable_key,
                confirmation_token=confirm_token,
                return_url=stripe_verify_url(session_id, processor=processor),
                expected_amount=tax_minor,
                billing=billing,
                currency=currency,
                save_choice=save_choice,
                strict_expected_amount=expected_amount_minor is not None,
            )
        confirm_body: Dict[str, Any] = {
            "checkout_session_id": session_id,
            "confirmation_token": confirm_token,
            "confirm_token": confirm_token,
            "selected_payment_method_type": "card",
        }
        # 会话若声明了 saved_payment_link_initial_save_for_reuse，
        # 保存选择必须与 ctoken 的 setup_future_usage 一致，且上游按字符串解析。
        if save_choice is not None:
            confirm_body["save_payment_method_for_reuse"] = "true" if save_choice else "false"

        def _post_confirm() -> Any:
            return self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/confirm",
                json=confirm_body,
                headers=self._route_headers("/backend-api/payments/checkout/confirm", page_url=page_url),
                timeout=self.timeout,
            )

        response = _post_confirm()
        # 会话可能要求新卡显式给出保存选择（官方前端抓包带 save_payment_method_for_reuse）。
        # 上游有两种措辞：
        #   new_card_save_choice_required  —— 缺字段
        #   Card save choice does not match —— 值不对
        # 普通会话带这个字段反而会被拒，所以只在上游明确要求时补发。
        # 400 校验失败发生在扣款之前，重发是安全的。
        def _needs_save_choice(resp: Any) -> bool:
            if int(getattr(resp, "status_code", 0) or 0) != 400:
                return False
            text = str(getattr(resp, "text", "") or "").lower()
            return "save_choice" in text or "save choice" in text

        if _needs_save_choice(response) and save_choice is None:
            for choice in (True, False):
                logger.info(
                    "checkout_confirm 要求保存选择，save_payment_method_for_reuse=%s 重试", choice
                )
                confirm_body["save_payment_method_for_reuse"] = "true" if choice else "false"
                response = _post_confirm()
                if not _needs_save_choice(response):
                    break
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "确认结账")
        payload = _json(response)
        logger.info(
            "checkout_confirm status=%s type=%s has_secret=%s",
            payload.get("status") or payload.get("result") or "",
            payload.get("type") or "",
            bool(payload.get("client_secret")),
        )
        return payload

    def confirm_team_hosted_checkout(
        self, *, checkout_session_id: str, publishable_key: str,
        payment_method: str, checkout_url: str = "",
        billing: Optional[Dict[str, str]] = None, currency: str = "",
    ) -> Dict[str, Any]:
        """Compatibility entry; Team API prepares context before creating PaymentMethod."""
        from .hosted import TeamHostedClient
        hosted = TeamHostedClient(self)
        context = hosted.prepare(session_id=public_checkout_id(checkout_session_id), key=publishable_key,
                                 billing=dict(billing or {}), currency=currency,
                                 browser_locale=self.language, browser_timezone=self.timezone)
        return hosted.confirm(context, payment_method)

    def confirm_stripe_custom_checkout(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        confirmation_token: str,
        return_url: str = "",
        expected_amount: Optional[int] = None,
        billing: Optional[Dict[str, str]] = None,
        currency: str = "",
        save_choice: Optional[bool] = None,
        strict_expected_amount: bool = False,
    ) -> Dict[str, Any]:
        """cs_live：文档 4.2 Hosted/Custom Checkout（init → tax_region → elements → confirm）。

        ``save_choice``：会话要求显式保存选择时传 True/False；
        None 表示不带该字段（默认行为）。
        """
        session_id = public_checkout_id(checkout_session_id)
        pk = str(publishable_key or "").strip()
        token = str(confirmation_token or "").strip()
        if not session_id.startswith("cs_"):
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "Stripe Custom Checkout 需要 cs_ 会话")
        if not pk.startswith(("pk_live_", "pk_test_")):
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少 Stripe 公钥")
        if not token.startswith("ctoken_"):
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "缺少 confirmation token")
        locale = str((self.language or "en-US").split("-")[0] or "en")
        stripe_js_id = str(uuid.uuid4())
        started = time.monotonic()
        init_body: Dict[str, str] = {
            "browser_locale": self.language or "en-US",
            "browser_timezone": self.timezone or "America/New_York",
            "key": pk,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            **_elements_session_client_fields(stripe_js_id=stripe_js_id, locale=locale),
        }
        init_payload = self._post_stripe_form(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/init",
            init_body,
        )
        tax_payload = self._update_stripe_tax_region(
            session_id=session_id,
            publishable_key=pk,
            billing=billing,
            stripe_js_id=stripe_js_id,
            locale=locale,
        )
        if strict_expected_amount:
            actual_minor = expected_amount_minor(tax_payload)
            _, actual_currency = quote_from_payload(tax_payload, currency)
            if actual_minor is None or (actual_currency and actual_currency.upper() != currency.upper()):
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "最终 Stripe 报价未核实，尚未提交确认")
            if actual_minor != expected_amount:
                raise CheckoutAmountChanged(actual_minor, currency)
        minor = expected_amount
        if minor is None:
            minor = expected_amount_minor(tax_payload) or expected_amount_minor(init_payload)
        if minor is None:
            raise DomainError(
                ErrorCode.PAYMENT_UNKNOWN_STATE,
                "Stripe 确认缺少应付金额（expected_amount）。请重新创建结账后再确认。",
            )
        elements = self._fetch_elements_session(
            session_id=session_id,
            publishable_key=pk,
            amount=minor,
            currency=currency or str(init_payload.get("currency") or ""),
            stripe_js_id=stripe_js_id,
        )
        elements_session_id = str(
            elements.get("session_id") or init_payload.get("elements_session_id") or ""
        )
        elements_config_id = str(elements.get("config_id") or init_payload.get("config_id") or "")
        self._stripe_js_id = stripe_js_id
        self._elements_session_id = elements_session_id
        confirm_body: Dict[str, str] = {
            "key": pk,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            "confirmation_token": token,
            "expected_payment_method_type": "card",
            "expected_amount": str(minor),
            "return_url": return_url or stripe_verify_url(session_id),
            "consent[terms_of_service]": "accepted",
            "guid": _stripe_device_id(),
            "muid": _stripe_device_id(),
            "sid": _stripe_device_id(),
            "time_on_page": str(max(8000, int((time.monotonic() - started) * 1000))),
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            **_elements_session_client_fields(
                stripe_js_id=stripe_js_id,
                elements_session_id=elements_session_id,
                locale=locale,
            ),
            **(
                {"save_payment_method_for_reuse": "true" if save_choice else "false"}
                if save_choice is not None
                else {}
            ),
        }
        if elements_config_id:
            confirm_body["client_attribution_metadata[elements_session_config_id]"] = elements_config_id
            confirm_body["client_attribution_metadata[checkout_config_id]"] = elements_config_id
        if elements_session_id:
            confirm_body["client_attribution_metadata[elements_session_id]"] = elements_session_id
        for key in ("init_checksum", "js_checksum", "rv_timestamp"):
            value = str(init_payload.get(key) or tax_payload.get(key) or "")
            if value:
                confirm_body[key] = value
        payload = self._post_stripe_form(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/confirm",
            confirm_body,
        )
        logger.info(
            "stripe_payment_page_confirm status=%s submission=%s has_pi=%s expected_amount=%s",
            payload.get("status") or "",
            submission_state(payload),
            bool(_intent_object(payload)),
            minor,
        )
        return payload

    def finalize_stripe_custom_checkout(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        checkout_url: str = "",
        processor_entity: str = "",
        confirmed: Optional[Dict[str, Any]] = None,
        attempts: int = 12,
        interval: float = 0.8,
    ) -> Dict[str, Any]:
        """cs_live confirm 之后：先 fall-through 轮询，仍 requires_approval 再 Sentinel approve。"""
        page = dict(confirmed or {})
        if not payment_page_settled(page):
            page = self.poll_payment_page(
                checkout_session_id=checkout_session_id,
                publishable_key=publishable_key,
                attempts=min(3, attempts),
                interval=min(0.6, interval) if interval else 0,
            ) or page
        if checkout_requires_approval(page):
            approved = self.approve_checkout(
                checkout_session_id=checkout_session_id,
                processor_entity=processor_entity,
                checkout_url=checkout_url,
            )
            logger.info(
                "checkout_approve result=%s status=%s",
                approved.get("result") or "",
                approved.get("status") or "",
            )
            if checkout_confirm_blocked(approved):
                page["status"] = str(approved.get("status") or "blocked")
                page["result"] = str(approved.get("result") or "blocked")
                extra = checkout_confirm_message(approved)
                if extra:
                    page["message"] = extra
                return page
        if payment_page_settled(page) and not checkout_requires_approval(page):
            return page
        polled = self.poll_payment_page(
            checkout_session_id=checkout_session_id,
            publishable_key=publishable_key,
            attempts=attempts,
            interval=interval,
        )
        return polled or page

    def poll_payment_page(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        attempts: int = 12,
        interval: float = 0.8,
    ) -> Dict[str, Any]:
        session_id = public_checkout_id(checkout_session_id)
        pk = str(publishable_key or "").strip()
        if not session_id.startswith("cs_") or not pk:
            return {}
        last: Dict[str, Any] = {}
        for _ in range(max(1, attempts)):
            payload = self._poll_payment_page_once(session_id=session_id, publishable_key=pk)
            if payload:
                last = payload
                logger.info(
                    "stripe_payment_page_poll status=%s object=%s submission=%s has_pi=%s",
                    payload.get("status") or "",
                    payload.get("payment_object_status") or "",
                    submission_state(payload),
                    bool(_intent_object(payload)),
                )
                if submission_state(payload) in {"failed", "declined", "canceled", "cancelled"}:
                    attempt = payload.get("submission_attempt")
                    updates = attempt.get("manual_approval_updates") if isinstance(attempt, dict) else None
                    logger.warning(
                        "stripe_submission_failed detail=%s attempt_keys=%s pi_status=%s approval_updates=%s",
                        submission_failure_detail(payload) or "-",
                        sorted(attempt) if isinstance(attempt, dict) else [],
                        nested_intent_status(payload) or "-",
                        redact_sensitive(json.dumps(updates, ensure_ascii=False, default=str))[:400] if updates else "-",
                    )
                if payment_page_settled(payload):
                    return payload
            if interval > 0:
                time.sleep(interval)
        return last

    def _poll_payment_page_once(self, *, session_id: str, publishable_key: str) -> Dict[str, Any]:
        locale = str((self.language or "en-US").split("-")[0] or "en")
        body = {
            "key": publishable_key,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            **_elements_session_client_fields(
                stripe_js_id=self._stripe_js_id or str(uuid.uuid4()),
                elements_session_id=self._elements_session_id,
                locale=locale,
            ),
        }
        posted = self._post_stripe_form_soft(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/poll",
            body,
        )
        if posted:
            return posted
        getter = getattr(self.stripe_http, "get", None)
        if not callable(getter):
            return {}
        params = dict(body)
        for url in (
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/poll",
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}",
        ):
            try:
                response = getter(url, params=params, timeout=self.timeout)
            except Exception:
                continue
            if int(getattr(response, "status_code", 0) or 0) < 400:
                payload = _json(response)
                if payload:
                    return payload
        return {}

    def _update_stripe_tax_region(
        self,
        *,
        session_id: str,
        publishable_key: str,
        billing: Optional[Dict[str, str]],
        stripe_js_id: str,
        locale: str,
    ) -> Dict[str, Any]:
        if not billing:
            return {}
        body: Dict[str, str] = {
            "key": publishable_key,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            **_elements_session_client_fields(stripe_js_id=stripe_js_id, locale=locale),
        }
        for field, key in (
            ("country", "country"),
            ("line1", "line1"),
            ("city", "city"),
            ("postal_code", "postal_code"),
            ("state", "state"),
        ):
            value = str((billing or {}).get(key) or "").strip()
            if value:
                body[f"tax_region[{field}]"] = value
        return self._post_stripe_form_soft(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}",
            body,
        )

    def _fetch_elements_session(
        self,
        *,
        session_id: str,
        publishable_key: str,
        amount: int,
        currency: str,
        stripe_js_id: str,
    ) -> Dict[str, Any]:
        getter = getattr(self.stripe_http, "get", None)
        if not callable(getter):
            return {}
        params = {
            "type": "deferred_intent",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(amount),
            "deferred_intent[currency]": str(currency or "usd").lower(),
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
            "checkout_session_id": session_id,
            "stripe_js_id": stripe_js_id,
            "key": publishable_key,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "locale": str((self.language or "en-US").split("-")[0] or "en"),
        }
        try:
            response = getter(
                f"{STRIPE_BASE}/v1/elements/sessions",
                params=params,
                timeout=self.timeout,
            )
        except Exception:
            return {}
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            return {}
        return _json(response)

    def approve_checkout(
        self,
        *,
        checkout_session_id: str,
        processor_entity: str = "",
        checkout_url: str = "",
    ) -> Dict[str, Any]:
        session_id = public_checkout_id(checkout_session_id)
        processor = processor_entity or _processor_from_checkout_url(checkout_url) or _processor("")
        page_url = checkout_url or f"{APP_BASE}/checkout/{processor}/{session_id}"
        self.mint_sentinel(flow="checkout_session_approval", page_url=page_url)
        headers = self._route_headers("/backend-api/payments/checkout/approve", page_url=page_url)
        headers["x-oai-is-client-observation"] = "true"
        response = self.http.post(
            f"{APP_BASE}/backend-api/payments/checkout/approve",
            json={"checkout_session_id": session_id, "processor_entity": processor},
            headers=headers,
            timeout=self.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "批准结账")
        return _json(response)

    def _post_stripe_form(self, url: str, body: Dict[str, str]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        status = 0
        data = dict(body)
        for _ in range(8):
            response = self.stripe_http.post(
                url,
                data=urlencode(data),
                timeout=self.timeout,
            )
            payload = _json(response)
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 400:
                break
            unknown = _stripe_unknown_param(payload)
            if not unknown:
                break
            removed = False
            for key in list(data):
                if key == unknown or key.startswith(unknown + "["):
                    data.pop(key, None)
                    removed = True
            if not removed:
                break
        if status >= 400:
            _raise_stripe(payload, status)
        return payload

    def _post_stripe_form_soft(self, url: str, body: Dict[str, str]) -> Dict[str, Any]:
        try:
            return self._post_stripe_form(url, body)
        except DomainError:
            return {}

    def retrieve_stripe_intent(
        self,
        *,
        intent_id: str,
        client_secret: str = "",
        publishable_key: str,
    ) -> Dict[str, Any]:
        getter = getattr(self.stripe_http, "get", None)
        if not callable(getter) or not intent_id:
            return {}
        kind = "payment_intents" if intent_id.startswith("pi_") else "setup_intents"
        params = {
            "key": publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
        }
        if client_secret:
            params["client_secret"] = client_secret
        try:
            response = getter(
                f"{STRIPE_BASE}/v1/{kind}/{intent_id}",
                params=params,
                timeout=self.timeout,
            )
        except Exception:
            return {}
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            return {}
        return _json(response)

    def confirm_stripe_intent(
        self,
        *,
        intent_id: str,
        client_secret: str,
        publishable_key: str,
        payment_method: str = "",
        confirmation_token: str = "",
        return_url: str = "",
        request_three_d_secure: str = "",
    ) -> Dict[str, Any]:
        intent_id = intent_id or intent_id_from_secret(client_secret)
        kind = "payment_intents" if intent_id.startswith("pi_") else "setup_intents"
        body: Dict[str, str] = {
            "key": publishable_key,
            "client_secret": client_secret,
            "_stripe_version": STRIPE_API_VERSION,
            "client_attribution_metadata[merchant_integration_source]": "l1",
        }
        if confirmation_token:
            body["confirmation_token"] = confirmation_token
        elif payment_method:
            body["payment_method"] = payment_method
        if return_url:
            body["return_url"] = return_url
        if request_three_d_secure:
            # automatic（默认）多数卡不会弹挑战；any 表示「只要卡支持就做 3DS」
            body["payment_method_options[card][request_three_d_secure]"] = request_three_d_secure
        response = self.stripe_http.post(
            f"{STRIPE_BASE}/v1/{kind}/{intent_id}/confirm",
            data=urlencode(body),
            timeout=self.timeout,
        )
        payload = _json(response)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            _raise_stripe(payload, status)
        return payload

    def subscription_payload(self) -> Dict[str, Any]:
        if not self.account_id:
            self.hydrate()
        response = self.http.get(
            f"{APP_BASE}/backend-api/subscriptions",
            # 上游现已要求显式 account_id，否则 400 "must specify either organization_id or account_id"。
            params={"account_id": self.account_id} if self.account_id else None,
            headers=self._route_headers("/backend-api/subscriptions"),
            timeout=self.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) == 401:
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.SUBSCRIPTION_NOT_ACTIVE, response, "查询订阅")
        return _json(response)


def _json(response: Any) -> Dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _account_id(payload: Dict[str, Any]) -> str:
    for key in ("account_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    account = payload.get("account")
    if isinstance(account, dict):
        for key in ("id", "account_id"):
            value = account.get(key)
            if isinstance(value, str) and value:
                return value
    orgs = payload.get("orgs") or payload.get("accounts")
    if isinstance(orgs, dict):
        data = orgs.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            value = data[0].get("id")
            if isinstance(value, str):
                return value
    return ""


def _processor(_country: str) -> str:
    # HAR：日本结账的 processor_entity 也是 openai_llc，不是 openai_ie。
    return "openai_llc"


def _processor_from_checkout_url(url: str) -> str:
    parts = [part for part in str(url or "").split("/") if part]
    if "checkout" not in parts:
        return ""
    index = parts.index("checkout")
    if index + 1 >= len(parts):
        return ""
    candidate = parts[index + 1]
    if candidate in {"verify", ""} or candidate.startswith("oaics_"):
        return ""
    return candidate


def _taxes_body(
    *,
    checkout_id: str,
    billing: Optional[Dict[str, str]] = None,
    processor: str = "",
    currency: str = "",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"checkout_session_id": checkout_id, "tax_id": None}
    if processor:
        body["processor_entity"] = processor
    if currency:
        body["currency"] = currency
    if billing:
        body["billing_country"] = billing.get("country") or ""
        body["billing_name"] = billing.get("name") or ""
        body["billing_address"] = {
            "line1": billing.get("line1") or "",
            "city": billing.get("city") or "",
            "state": billing.get("state") or "",
            "postal_code": billing.get("postal_code") or "",
            "country": billing.get("country") or "",
        }
    return body


def intent_id_from_secret(secret: str) -> str:
    text = str(secret or "").strip()
    marker = "_secret_"
    if marker not in text:
        return ""
    prefix = text.split(marker, 1)[0]
    if prefix.startswith(("pi_", "seti_")):
        return prefix
    return ""


_CONFIRM_BLOCKED = frozenset({"blocked", "failed", "rejected", "not_approved", "declined"})


def checkout_confirm_blocked(payload: Dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("result") or "").strip().lower()
    return status in _CONFIRM_BLOCKED


def checkout_confirm_message(payload: Dict[str, Any]) -> str:
    for key in ("message", "detail", "reason", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in _CONFIRM_BLOCKED:
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("message") or value.get("code") or value.get("reason")
            if nested:
                return str(nested)
    return ""


def checkout_confirm_blocked_advice(
    country: str = "", *, upstream_reason: str = "", upstream_status: str = "blocked",
) -> str:
    """Report an upstream refusal without inferring a card, account or region cause.

    ``country`` is retained for call compatibility; country alone is not evidence.
    """
    status = str(upstream_status or "blocked").strip().lower()
    if status not in _CONFIRM_BLOCKED:
        status = "blocked"
    reason = redact_sensitive(str(upstream_reason or "").strip())
    if reason:
        return f"ChatGPT 未批准本次结账（{status}）。上游信息：{reason}。尚未确认扣款，请先核对账单。"
    return f"ChatGPT 未批准本次结账（{status}），上游未提供明确原因。尚未确认扣款，请先核对账单。"


def payment_intent_from_confirm(payload: Dict[str, Any]) -> tuple[str, str, str]:
    """ChatGPT checkout/confirm 成功时返回 status=success + pi client_secret，没有顶层 id。

    Stripe payment_pages/confirm 则把 PaymentIntent 嵌在 ``payment_intent`` 里，
    批准前也可能只给 ``pi_`` 字符串。
    """
    nested = _intent_object(payload)
    secret = str(payload.get("client_secret") or nested.get("client_secret") or "")
    intent_id = str(nested.get("id") or "")
    raw = payload.get("payment_intent")
    if not intent_id and isinstance(raw, str) and raw.startswith("pi_"):
        intent_id = raw
    if intent_id.startswith("cs_"):
        intent_id = ""
    intent_id = intent_id or intent_id_from_secret(secret)
    return_url = str(payload.get("confirm_return_url") or payload.get("return_url") or "")
    return intent_id, secret, return_url


def submission_state(payload: Dict[str, Any]) -> str:
    attempt = payload.get("submission_attempt") if isinstance(payload.get("submission_attempt"), dict) else {}
    return str(attempt.get("state") or attempt.get("status") or "").strip().lower()


_FAILURE_KEYS = ("decline_code", "code", "message", "reason", "failure_reason", "failure_code", "type")


def _failure_parts(source: Any) -> list[str]:
    if isinstance(source, str):
        return [source.strip()] if source.strip() else []
    if not isinstance(source, dict):
        return []
    parts: list[str] = []
    for key in _FAILURE_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    return parts


def submission_failure_detail(payload: Dict[str, Any]) -> str:
    """从失败的 Stripe 响应里尽量找出拒绝原因（decline_code / last_payment_error 等）。"""
    attempt = payload.get("submission_attempt") if isinstance(payload.get("submission_attempt"), dict) else {}
    nested = _intent_object(payload)
    sources = [
        attempt.get("error"),
        attempt.get("last_error"),
        attempt.get("failure"),
        attempt,
        payload.get("last_payment_error"),
        nested.get("last_payment_error"),
        payload.get("error"),
    ]
    seen: list[str] = []
    for source in sources:
        for part in _failure_parts(source):
            if part not in seen:
                seen.append(part)
    return "; ".join(seen[:6])


def checkout_requires_approval(payload: Dict[str, Any]) -> bool:
    return submission_state(payload) in {"requires_approval", "pending_approval"}


def nested_intent_status(payload: Dict[str, Any]) -> str:
    nested = _intent_object(payload)
    return str(nested.get("status") or "").strip().lower()


def payment_page_settled(payload: Dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("payment_object_status") or "").strip().lower()
    state = submission_state(payload)
    pi_status = nested_intent_status(payload)
    if status in {"complete", "completed", "expired", "succeeded", "requires_action"}:
        return True
    if state in {"succeeded", "complete", "completed", "failed", "declined", "canceled", "cancelled"}:
        return True
    if pi_status in {
        "succeeded",
        "processing",
        "requires_action",
        "requires_payment_method",
        "canceled",
        "cancelled",
    }:
        return True
    return False


def _intent_object(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("payment_intent", "setup_intent"):
        value = payload.get(key)
        if isinstance(value, dict) and (
            value.get("id") or value.get("client_secret") or value.get("status")
        ):
            return value
    attempt = payload.get("submission_attempt")
    if isinstance(attempt, dict):
        nested = _intent_object(attempt)
        if nested:
            return nested
    elements = payload.get("elements_session")
    if isinstance(elements, dict):
        nested = _intent_object(elements)
        if nested:
            return nested
    return {}


def expected_amount_minor(payload: Any) -> Optional[int]:
    """Stripe payment_pages/confirm 的 expected_amount：应付整数（最小货币单位）。

    init 响应里通常是 ``total_summary.due`` / ``invoice.amount_due``，不是顶层 amount_total。
    """
    if not isinstance(payload, dict):
        return None
    # Current custom checkout puts the payable total in checkout_state.total.total.
    for wrapper in (payload, payload.get("checkout_session"), payload.get("session")):
        if not isinstance(wrapper, dict):
            continue
        state = wrapper.get("checkout_state")
        if isinstance(state, dict) and isinstance(state.get("total"), dict):
            total = state["total"]
            bucket = total.get("total") if isinstance(total.get("total"), dict) else total
            if bucket.get("minorUnitsAmount") is not None:
                found = _as_minor_int(bucket["minorUnitsAmount"])
                if found is not None:
                    return found
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else None
    if snapshot:
        found = _as_minor_int(snapshot.get("amount_total") or snapshot.get("due"))
        if found is not None:
            return found
    wrappers = (
        payload,
        payload.get("session") if isinstance(payload.get("session"), dict) else None,
        payload.get("checkout_session") if isinstance(payload.get("checkout_session"), dict) else None,
        payload.get("payment_page") if isinstance(payload.get("payment_page"), dict) else None,
    )
    for wrapper in wrappers:
        if not isinstance(wrapper, dict):
            continue
        found = _expected_amount_from_object(wrapper)
        if found is not None:
            return found
    return None


def _as_minor_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    text = str(value).strip()
    if not text or "." in text or "," in text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _expected_amount_from_object(payload: Dict[str, Any]) -> Optional[int]:
    summary = payload.get("total_summary")
    if isinstance(summary, dict):
        for key in ("due", "amount_due", "total", "amount"):
            found = _as_minor_int(summary.get(key))
            if found is not None:
                return found
    invoice = payload.get("invoice")
    if isinstance(invoice, dict):
        for key in ("amount_due", "total", "amount_paid"):
            found = _as_minor_int(invoice.get(key))
            if found is not None:
                return found
    for key in ("amount_total", "amount_due", "checkout_amount_minor"):
        found = _as_minor_int(payload.get(key))
        if found is not None:
            return found
    total = payload.get("total")
    if isinstance(total, dict):
        nested = total.get("total") if isinstance(total.get("total"), dict) else None
        if nested:
            found = _as_minor_int(nested.get("minorUnitsAmount") or nested.get("due"))
            if found is not None:
                return found
        for key in ("due", "minorUnitsAmount", "amount", "total"):
            found = _as_minor_int(total.get(key))
            if found is not None:
                return found
    items = payload.get("line_items") or payload.get("lineItems")
    if isinstance(items, list):
        running = 0
        saw = False
        for item in items:
            if not isinstance(item, dict):
                continue
            found = _as_minor_int(item.get("amount") or item.get("amount_total"))
            if found is None:
                continue
            running += found
            saw = True
        if saw:
            return running
    return None


def _stripe_unknown_param(payload: Dict[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    param = str(error.get("param") or "").strip()
    if param:
        return param
    message = str(error.get("message") or payload.get("message") or "")
    match = re.search(r"unknown parameter[:\s]+['\"]?([A-Za-z0-9_\[\]]+)", message, re.I)
    return match.group(1) if match else ""


_DECLINE_ADVICE = {
    "do_not_honor": "发卡行未说明原因。请联系银行确认该卡已开通境外/网上/订阅扣款、额度够用，并核对账单地址与卡登记地址一致；先别连续重试，可换一张卡。",
    "generic_decline": "发卡行未说明原因。请联系银行确认该卡可用于境外网上扣款，再重试或换卡。",
    "insufficient_funds": "余额或额度不足，请充值后重试或换卡。",
    "card_velocity_exceeded": "短时间内尝试次数或金额超限，请稍后再试，或联系银行放宽限额。",
    "lost_card": "发卡行标记该卡已挂失，请换卡。",
    "stolen_card": "发卡行标记该卡不可用，请换卡。",
    "expired_card": "卡已过期，请换卡。",
    "incorrect_cvc": "安全码（CVC）不正确，请重新输入。",
    "incorrect_number": "卡号不正确，请重新输入。",
    "invalid_cvc": "安全码（CVC）不正确，请重新输入。",
    "invalid_expiry_month": "有效期月份不正确，请重新输入。",
    "invalid_expiry_year": "有效期年份不正确，请重新输入。",
    "authentication_required": "发卡行要求 3DS 验证，请在银行 App 或短信里完成验证后重试。",
    "card_not_supported": "这张卡不支持该类扣款，请换卡。",
    "currency_not_supported": "这张卡不支持当前币种，请联系银行开通或换卡。",
    "transaction_not_allowed": "发卡行不允许这类交易，请联系银行或换卡。",
    "pickup_card": "发卡行要求不再使用该卡，请换卡。",
    "fraudulent": "发卡行判定为风险交易，请联系银行确认后再试。",
}


def decline_message(detail: str) -> str:
    """发卡行拒绝的统一文案：保留原始拒绝码，已知码附上处理建议。"""
    code = str(detail or "").strip()
    advice = _DECLINE_ADVICE.get(code.lower())
    return f"发卡行拒绝: {code}。{advice}" if advice else f"发卡行拒绝: {code}"


def _raise_stripe(payload: Dict[str, Any], status: int) -> None:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(error.get("code") or "")
    decline = str(error.get("decline_code") or "")
    message = str(error.get("message") or payload.get("message") or f"Stripe HTTP {status}")
    if "billing country must match" in message.lower():
        raise DomainError(
            ErrorCode.BILLING_COUNTRY_MISMATCH,
            "账单国家必须与出口代理国家一致，请用所选地区的 SID 重试。",
        )
    if "automatic payment methods" in message.lower() and "payment_method_types" in message.lower():
        raise DomainError(
            ErrorCode.PAYMENT_UNKNOWN_STATE,
            "支付方式采集模式和确认接口不一致。请刷新页面后重新创建结账，不要用上一单的输入框。",
        )
    if "expected amount" in message.lower() and "has to be provided" in message.lower():
        raise DomainError(
            ErrorCode.PAYMENT_UNKNOWN_STATE,
            "Stripe 确认缺少应付金额。请重新创建结账后再确认。",
        )
    if status == 402 or code == "card_declined" or decline:
        detail = decline or code or message
        raise DomainError(ErrorCode.PAYMENT_DECLINED, decline_message(redact_sensitive(detail)))
    raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, redact_sensitive(message))


_SETI_SECRET = re.compile(r"seti_[A-Za-z0-9]+_secret_[A-Za-z0-9]+")
_SETI_ID = re.compile(r"seti_[A-Za-z0-9]+")


def _walk(payload: Any):
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _walk(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk(item)
    else:
        yield payload


def setup_intent_from_payload(payload: Any) -> tuple[str, str]:
    """从 ChatGPT /payments/payment_method 响应里抽出 seti + client_secret。"""
    secret = ""
    if isinstance(payload, dict):
        nested = payload.get("setup_intent")
        if isinstance(nested, dict):
            nested_id, nested_secret = setup_intent_from_payload(nested)
            if nested_id and nested_secret:
                return nested_id, nested_secret
        for key in ("client_secret", "clientSecret", "setup_intent_client_secret"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("seti_"):
                secret = value
                break
    if not secret:
        for value in _walk(payload):
            if isinstance(value, str) and _SETI_SECRET.search(value):
                secret = _SETI_SECRET.search(value).group(0)
                break
    if not secret:
        return "", ""
    match = _SETI_ID.search(secret)
    setup_id = match.group(0) if match else ""
    if isinstance(payload, dict):
        for key in ("id", "setup_intent_id", "setupIntentId"):
            value = str(payload.get(key) or "")
            if value.startswith("seti_") and "_secret_" not in value:
                setup_id = value
                break
    return setup_id, secret


__all__ = [
    "APP_BASE",
    "ChatGPTClient",
    "CheckoutSession",
    "hosted_checkout_url",
    "stripe_checkout_id",
    "openai_internal_checkout_id",
    "public_checkout_id",
    "clamp_team_seats",
    "checkout_confirm_blocked",
    "checkout_confirm_blocked_advice",
    "checkout_confirm_message",
    "checkout_requires_approval",
    "payment_page_settled",
    "submission_state",
    "nested_intent_status",
    "expected_amount_minor",
    "intent_id_from_secret",
    "jwt_account_id",
    "payment_intent_from_confirm",
    "setup_intent_from_payload",
    "stripe_verify_url",
]
