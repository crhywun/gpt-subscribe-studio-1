"""请求 / 响应 schema。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ModeIn(BaseModel):
    account_source: str = Field(default="customer_session")
    card_source: str = Field(default="customer")
    billing_mode: str = Field(default="standard")
    risk_acknowledged: bool = False


class BillingIn(BaseModel):
    email: str = Field(default="", max_length=254)
    name: str = Field(min_length=1, max_length=100)
    line1: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=2, max_length=2)


class OrderCreateRequest(BaseModel):
    plan: str
    billing_country: str
    mode: ModeIn = Field(default_factory=ModeIn)
    session: Optional[str] = None
    payment_method_token: Optional[str] = None
    promo_code: str = ""
    promo_campaign_id: str = ""
    workspace_name: str = ""
    seat_quantity: Optional[int] = None
    price_interval: str = "month"
    billing: Optional[BillingIn] = None


class CardIn(BaseModel):
    """Server-side card submission. Never persisted or logged."""
    number: str = Field(min_length=12, max_length=23)
    cvc: str = Field(min_length=3, max_length=4)
    exp_month: str = Field(min_length=1, max_length=2)
    exp_year: str = Field(min_length=2, max_length=4)


class OrderPayRequest(BaseModel):
    payment_method_token: str = ""
    billing: Optional[BillingIn] = None
    team_context_id: Optional[str] = Field(default=None, max_length=80)
    passive_captcha_token: Optional[str] = Field(default=None, max_length=20000)
    rv_timestamp: Optional[str] = Field(default=None, max_length=5000)
    card: Optional[CardIn] = None


class TeamQuoteRequest(BaseModel):
    billing: Optional[BillingIn] = None
    browser_locale: str = Field(default="en-US", max_length=30)
    browser_timezone: str = Field(default="UTC", max_length=80)


class TeamPrepareRequest(BaseModel):
    billing: BillingIn
    browser_locale: str = Field(default="en-US", max_length=30)
    browser_timezone: str = Field(default="UTC", max_length=80)


class SubscriptionOut(BaseModel):
    active: bool
    expires_at: Optional[str] = None
    channel: Optional[str] = None


class OrderOut(BaseModel):
    id: str
    status: str
    plan: str
    billing_country: str
    mode: ModeIn
    checkout_url: Optional[str] = None
    publishable_key: Optional[str] = None
    customer_session_client_secret: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    billing: Optional[Dict[str, str]] = None
    last_error: Optional[str] = None
    quote_error: Optional[str] = None
    next_action: Optional[Dict[str, Any]] = None
    subscription: Optional[SubscriptionOut] = None
    checkout_kind: Optional[str] = None
    seat_quantity: Optional[int] = None
    workspace_name: Optional[str] = None
    promo_code: Optional[str] = None
    promo_campaign_id: Optional[str] = None
    price_interval: Optional[str] = None
    confirm_return_url: Optional[str] = None
    # 从 accessToken(JWT) 解析出的账户邮箱，用于前端预填账单邮箱
    session_email: Optional[str] = None


class ErrorOut(BaseModel):
    code: str
    message: str
    detail: Optional[Dict[str, Any]] = None
