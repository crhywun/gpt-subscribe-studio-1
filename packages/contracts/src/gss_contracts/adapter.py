"""adapter 协议：本平台**唯一**的上游接触边界（ADR 0001）。

约束（宪法第 II 条）：
- adapter 是唯一允许接触上游代码/接口的位置；
- 每个 adapter 必须声明 :class:`AdapterCapabilities`（来源 / 许可证 / 能力 / 失败模式）；
- 上游字段不得泄漏进 ``packages/contracts`` 的领域模型。

凭证约束（宪法第 I 条）：``session`` / ``payment_method_token`` / ``card_id``
只作为**入参**在内存与受限临时文件中流转，绝不写入业务库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .models import BillingMode, Plan
from .state import PaymentStatus


@dataclass(frozen=True)
class AdapterCapabilities:
    """adapter 自述。任何 adapter 都必须能回答「我来自哪、什么许可证、能做什么、会怎么失败」。"""

    source: str
    license: str
    supports: Dict[str, List[str]] = field(default_factory=dict)
    failure_modes: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# checkout
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CheckoutRequest:
    """生成结账链接的请求。``session`` 仅内存传递。"""

    session: str
    plan: Plan
    billing_country: str
    billing_mode: BillingMode
    proxy: Optional[str] = None
    promo_code: str = ""
    promo_campaign_id: str = ""
    workspace_name: str = ""
    seat_quantity: int = 5
    price_interval: str = "month"
    billing: Optional[Dict[str, str]] = field(default=None, repr=False)


@dataclass(frozen=True)
class CheckoutResult:
    """托管结账链接结果。``publishable_key`` 是 Stripe 公钥，可回给浏览器。"""

    checkout_url: str
    plan: Plan
    currency: str
    amount: Optional[Decimal] = None
    upstream_request_id: Optional[str] = None
    checkout_session_id: Optional[str] = None
    publishable_key: Optional[str] = None
    customer_session_client_secret: Optional[str] = None
    billing: Optional[Dict[str, str]] = None
    checkout_kind: str = "custom"
    confirm_return_url: str = ""
    effective_proxy: Optional[str] = field(default=None, repr=False)
    quote_error: Optional[str] = None


@runtime_checkable
class CheckoutAdapter(Protocol):
    """结账链接生成能力。默认生成**正价**档位链接。"""

    def capabilities(self) -> AdapterCapabilities:
        ...

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        ...


# --------------------------------------------------------------------------- #
# payment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PaymentRequest:
    """支付请求。

    二者互斥：
    - ``payment_method_token``：客户卡，由浏览器端组件产出的**不透明标识**（非卡号）；
    - ``card_id``：平台卡池条目标识（非卡号）。
    """

    checkout_url: str
    country: str
    payment_method_token: Optional[str] = None
    card_id: Optional[str] = None
    checkout_proxy: Optional[str] = None
    approve_proxy: Optional[str] = None
    session: Optional[str] = None
    checkout_session_id: Optional[str] = None
    publishable_key: Optional[str] = None
    billing: Optional[Dict[str, str]] = None
    plan: Optional[str] = None
    team_context: Optional[Dict[str, Any]] = None
    passive_captcha_token: Optional[str] = None
    rv_timestamp: Optional[str] = None


@dataclass(frozen=True)
class PaymentResult:
    """支付结果。``next_action`` 为不透明 3DS 数据，不含敏感字段。"""

    status: PaymentStatus
    next_action: Optional[Dict[str, Any]] = None
    provider_ref: Optional[str] = None
    message: Optional[str] = None


@runtime_checkable
class PaymentAdapter(Protocol):
    """卡绑定与支付执行能力（对接 zkky 直卡绑定）。"""

    def capabilities(self) -> AdapterCapabilities:
        ...

    async def pay(self, req: PaymentRequest) -> PaymentResult:
        ...


# --------------------------------------------------------------------------- #
# account
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubscriptionInfo:
    """订阅校验结果。交付前必须调用（宪法第 VI 条 / FR-006）。"""

    active: bool
    expires_at: Optional[datetime] = None
    channel: Optional[str] = None
    raw_kind: Optional[str] = None


@dataclass(frozen=True)
class PromoCampaignInfo:
    """上游返回的可展示促销摘要；不包含 session/token。"""

    key: str
    campaign_id: str
    discount_percentage: Optional[float] = None
    duration_num_periods: Optional[int] = None
    duration_period: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    promotion_type: Optional[str] = None


@dataclass(frozen=True)
class PromoEligibility:
    """账号检查后的套餐与促销资格。"""

    account_id: Optional[str]
    current_plan_type: Optional[str]
    subscription_plan: Optional[str]
    is_free: bool
    plus_eligible: bool
    campaigns: List[PromoCampaignInfo] = field(default_factory=list)


@runtime_checkable
class AccountAdapter(Protocol):
    """账号能力：注册 / 库存 / 订阅校验。"""

    def capabilities(self) -> AdapterCapabilities:
        ...

    async def subscription_status(self, session: str) -> SubscriptionInfo:
        ...

    async def promo_eligibility(self, session: str) -> PromoEligibility:
        ...

    async def register_account(self) -> Dict[str, Any]:
        """注册平台账号（``account_source=platform_account`` 时使用）。"""


__all__ = [
    "AdapterCapabilities",
    "CheckoutRequest",
    "CheckoutResult",
    "CheckoutAdapter",
    "PaymentRequest",
    "PaymentResult",
    "PaymentAdapter",
    "SubscriptionInfo",
    "PromoCampaignInfo",
    "PromoEligibility",
    "AccountAdapter",
]
