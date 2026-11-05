"""订单编排流水线（骨架）。

把三个 adapter 能力编排成一条可离线验证的链路：

    create_checkout（提链） → pay（卡支付） → verify（订阅校验） → 交付门槛

对应 tasks.md T020 / T027 / T032。真实上游调用一律发生在 adapter 层（ADR 0001）。
本模块**无内部状态**，便于测试与断点续跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from gss_contracts import (
    AccountAdapter,
    CardSource,
    CheckoutAdapter,
    CheckoutRequest,
    CheckoutResult,
    Mode,
    OrderStatus,
    PaymentAdapter,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
    Plan,
    SubscriptionInfo,
    assert_transition,
    is_deliverable,
)
from gss_contracts.errors import DomainError, ErrorCode

#: 支付结果 → 订单状态映射（ADR 0002：不允许把「请求成功」当成「订阅生效」）。
PAYMENT_STATUS_TO_ORDER: Dict[PaymentStatus, OrderStatus] = {
    PaymentStatus.INITIATED: OrderStatus.PAYING,
    PaymentStatus.PROCESSING: OrderStatus.PAYING,
    PaymentStatus.SUCCEEDED: OrderStatus.VERIFYING,
    PaymentStatus.REQUIRES_ACTION: OrderStatus.REQUIRES_ACTION,
    PaymentStatus.DECLINED: OrderStatus.FAILED,
    PaymentStatus.ERROR: OrderStatus.UNKNOWN,
}


@dataclass
class PipelineDeps:
    """三个 adapter —— 唯一的出站边界。"""

    checkout: CheckoutAdapter
    payment: PaymentAdapter
    account: AccountAdapter


class OrderPipeline:
    """把 adapter 能力编排成链路。"""

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    async def create_checkout(
        self,
        *,
        session: str,
        plan: Plan,
        billing_country: str,
        mode: Mode,
        proxy: Optional[str] = None,
        promo_code: str = "",
        promo_campaign_id: str = "",
        workspace_name: str = "",
        seat_quantity: int = 5,
        price_interval: str = "month",
        billing: Optional[Dict[str, str]] = None,
    ) -> CheckoutResult:
        """阶段一：生成托管结账链接。``mode`` 非法时拒绝（含 promo 未确认）。"""

        mode.validate()
        assert_transition(OrderStatus.CREATED, OrderStatus.PENDING_CHECKOUT)
        req = CheckoutRequest(
            session=session,
            plan=plan,
            billing_country=billing_country,
            billing_mode=mode.billing_mode,
            proxy=proxy,
            promo_code=promo_code,
            promo_campaign_id=promo_campaign_id,
            workspace_name=workspace_name,
            seat_quantity=seat_quantity,
            price_interval=price_interval,
            billing=dict(billing) if billing is not None else None,
        )
        return await self.deps.checkout.create_checkout(req)

    async def pay(
        self,
        *,
        checkout_url: str,
        country: str,
        mode: Mode,
        payment_method_token: Optional[str] = None,
        card_id: Optional[str] = None,
        checkout_proxy: Optional[str] = None,
        approve_proxy: Optional[str] = None,
        session: Optional[str] = None,
        checkout_session_id: Optional[str] = None,
        publishable_key: Optional[str] = None,
        billing: Optional[Dict[str, str]] = None,
        plan: Optional[str] = None,
        team_context: Optional[Dict[str, Any]] = None,
        passive_captcha_token: Optional[str] = None,
        rv_timestamp: Optional[str] = None,
    ) -> PaymentResult:
        """阶段二：卡支付。按 ``card_source`` 校验必备入参（凭证只在此处入参传递）。"""

        if mode.card_source is CardSource.CUSTOMER and not payment_method_token:
            raise DomainError(
                ErrorCode.PAYMENT_DECLINED,
                "card_source=customer 需要浏览器端产出的 payment_method_token",
            )
        if mode.card_source is CardSource.PLATFORM and not card_id:
            raise DomainError(
                ErrorCode.PAYMENT_DECLINED, "card_source=platform 需要 card_id"
            )
        req = PaymentRequest(
            checkout_url=checkout_url,
            country=country,
            payment_method_token=payment_method_token,
            card_id=card_id,
            checkout_proxy=checkout_proxy,
            approve_proxy=approve_proxy,
            session=session,
            checkout_session_id=checkout_session_id,
            publishable_key=publishable_key,
            billing=billing, plan=plan, team_context=team_context,
            passive_captcha_token=passive_captcha_token, rv_timestamp=rv_timestamp,
        )
        return await self.deps.payment.pay(req)

    async def verify(self, *, session: str) -> SubscriptionInfo:
        """阶段三：订阅校验。交付前必须调用。"""

        return await self.deps.account.subscription_status(session)


def next_order_status(current: OrderStatus, payment: PaymentStatus) -> OrderStatus:
    """由支付结果推导订单的下一状态，并校验迁移合法性。"""

    target = PAYMENT_STATUS_TO_ORDER[payment]
    if target is current:
        # 支付仍在进行（PROCESSING）时订单停留在 paying，不算迁移。
        return current
    assert_transition(current, target)
    return target


def apply_payment_result(current: OrderStatus, payment: PaymentStatus) -> OrderStatus:
    """支付从 ``awaiting_payment`` 起步时，先进入 ``paying`` 再落到支付终态。"""

    if current is OrderStatus.AWAITING_PAYMENT:
        assert_transition(current, OrderStatus.PAYING)
        current = OrderStatus.PAYING
    return next_order_status(current, payment)


def assert_deliverable(status: OrderStatus, info: SubscriptionInfo) -> None:
    """交付门槛：订单必须 ``active`` **且** 订阅已确认生效（FR-006 / SC-003）。"""

    if not is_deliverable(status):
        raise DomainError(
            ErrorCode.SUBSCRIPTION_NOT_ACTIVE, f"订单状态 {status} 不可交付"
        )
    if not info.active:
        raise DomainError(
            ErrorCode.SUBSCRIPTION_NOT_ACTIVE, "订阅尚未确认生效，拒绝交付"
        )


__all__ = [
    "PipelineDeps",
    "OrderPipeline",
    "PAYMENT_STATUS_TO_ORDER",
    "next_order_status",
    "apply_payment_result",
    "assert_deliverable",
]
