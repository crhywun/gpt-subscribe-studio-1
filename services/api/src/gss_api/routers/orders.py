"""订单：粘贴 session → 生成结账 → 用浏览器 PaymentMethod 开通。"""

from __future__ import annotations

import asyncio
import httpx
import json
import logging
import re
import time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from gss_contracts import (
    AccountSource,
    BillingMode,
    CardSource,
    Job,
    Mode,
    ModeValidationError,
    Order,
    OrderStatus,
    Plan,
)
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.config import load_settings, use_fake_adapters
from gss_core.countries import is_supported, normalize_country
from gss_core.hosted import TeamHostedClient
from gss_core.plus_protocol import PlusProtocol
from gss_core.plus_har_protocol import PlusHarProtocol
from gss_core.stripe_protocol import StripeProtocol, PaymentOutcome
from gss_core.proxies import align_proxy_region, configured_proxy, session_proxy
from gss_core.redaction import redact_sensitive
from gss_core.chatgpt import ChatGPTClient, clamp_team_seats, _processor_from_checkout_url
from gss_core.session_token import (
    email_from_access_token,
    parse_session,
    parse_session_bundle,
)
from gss_core.vault import OrderSecrets, vault
from gss_core.saved_cards import verification_session
from gss_orchestrator.pipeline import apply_payment_result, assert_deliverable
from gss_payment.adapter import hosted_payment_result

from ..runtime import build_pipeline
from ..schemas import (
    BillingIn,
    CardIn,
    ErrorOut,
    ModeIn,
    OrderCreateRequest,
    OrderOut,
    OrderPayRequest,
    SubscriptionOut,
    TeamPrepareRequest,
    TeamQuoteRequest,
)
from ..store import store
from .events import notify_order_update

router = APIRouter(prefix="/api", tags=["orders"])
logger = logging.getLogger("gss.orders")
_team_locks: dict[str, asyncio.Lock] = {}


def _assert_saved_card_resolved(secrets: OrderSecrets) -> None:
    context = secrets.saved_card_context
    if context and context.get("protocol_flow"):
        raise HTTPException(409, "本订单已进入协议绑卡支付，请使用协议状态查询")
    if context and context.get("state") not in {"saved", "checkout_ready", "canceled"}:
        raise HTTPException(409, "请先完成保存卡片的验证与账号同步，再提交付款")


def _verify_saved_checkout(pipeline, secrets: OrderSecrets):
    client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
    client.timeout = 30.0
    try:
        session = verification_session(client, secrets.saved_card_context["account_id"])
    finally:
        client.close()
    # The upstream account adapter uses blocking HTTP; keep the entire lookup off the API loop.
    return session, asyncio.run(pipeline.verify(session=session))


def _to_mode(mode_in: ModeIn) -> Mode:
    return Mode(
        account_source=AccountSource(mode_in.account_source),
        card_source=CardSource(mode_in.card_source),
        billing_mode=BillingMode(mode_in.billing_mode),
        risk_acknowledged=mode_in.risk_acknowledged,
    )


def _to_out(order: Order) -> OrderOut:
    view = store.view(order.id)
    subscription = None
    if view.subscription is not None:
        subscription = SubscriptionOut(
            active=view.subscription.active,
            expires_at=view.subscription.expires_at.isoformat() if view.subscription.expires_at else None,
            channel=view.subscription.channel,
        )
    # 账户邮箱：从 vault 里的 accessToken 现解一次，失败留空由前端兜底
    session_email = ""
    try:
        _sec = vault.get(order.id)
        if _sec and _sec.session:
            session_email = email_from_access_token(parse_session(_sec.session))
    except Exception:
        session_email = ""

    return OrderOut(
        id=order.id,
        status=str(order.status),
        plan=str(order.plan),
        billing_country=order.billing_country,
        mode=ModeIn(
            account_source=str(order.mode.account_source),
            card_source=str(order.mode.card_source),
            billing_mode=str(order.mode.billing_mode),
            risk_acknowledged=order.mode.risk_acknowledged,
        ),
        checkout_url=view.checkout_url,
        publishable_key=view.publishable_key,
        customer_session_client_secret=view.customer_session_client_secret,
        amount=view.amount if view.amount is not None else (str(order.amount) if order.amount is not None else None),
        currency=view.currency,
        billing=view.billing,
        last_error=view.last_error,
        quote_error=view.quote_error,
        next_action=view.next_action,
        subscription=subscription,
        checkout_kind=view.checkout_kind,
        seat_quantity=view.seat_quantity,
        workspace_name=view.workspace_name,
        promo_code=view.promo_code,
        promo_campaign_id=view.promo_campaign_id,
        price_interval=view.price_interval,
        confirm_return_url=view.confirm_return_url,
        session_email=session_email or None,
    )


def _error(status_code: int, code: ErrorCode, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorOut(code=str(code), message=message).model_dump(),
    )


def _from_domain(exc: DomainError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code in {ErrorCode.INVALID_SESSION, ErrorCode.SESSION_EXPIRED}:
        status_code = status.HTTP_401_UNAUTHORIZED
    return _error(status_code, exc.code, exc.message)


_PROXY_ERR_HINTS = (
    "SSL", "EOF", "ConnectError", "Connection closed", "CurlError",
    "TLS", "timed out", "Timeout", "reset by peer",
)


def _is_proxy_error(exc: Exception) -> bool:
    return any(k in str(exc) for k in _PROXY_ERR_HINTS)


async def _create_checkout_with_retry(pipeline, *, proxy: str, **kwargs):
    """出口代理断连时换 SID 重试建单。

    返回 (checkout, 实际使用的 proxy)。调用方必须把返回的 proxy 存进 vault，
    否则支付会用回旧的（已失效的）出口，造成跨步骤指纹矛盾。
    """
    from gss_core.proxies import rotate_proxy_sid

    current = proxy
    last: Exception | None = None
    for attempt in range(3):
        try:
            checkout = await pipeline.create_checkout(proxy=current, **kwargs)
            return checkout, current
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_proxy_error(exc):
                raise
            last = exc
            if attempt == 2:
                break
            logger.info("checkout_proxy_retry attempt=%d", attempt + 1)
            current = rotate_proxy_sid(current) if current else current
            await asyncio.sleep(2)
    raise last if last else DomainError(
        ErrorCode.CHECKOUT_UPSTREAM_ERROR, "建单重试失败"
    )


@router.post(
    "/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderOut,
    responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}},
)
async def create_order(
    payload: OrderCreateRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> OrderOut:
    existing = store.find_by_idempotency(idempotency_key)
    if existing is not None:
        return _to_out(existing)

    try:
        plan = Plan(payload.plan)
    except ValueError:
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, f"未知套餐: {payload.plan}")

    try:
        mode = _to_mode(payload.mode)
        mode.validate()
    except ModeValidationError as exc:
        try:
            code = ErrorCode(exc.code)
        except ValueError:
            code = ErrorCode.COMPLIANCE_BLOCKED
        raise _error(status.HTTP_400_BAD_REQUEST, code, exc.message)
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, f"模式非法: {exc}")

    if mode.billing_mode is BillingMode.PROMO and not load_settings().enable_promo_mode:
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, "促销模式尚未启用")

    if mode.account_source is AccountSource.CUSTOMER_SESSION and not payload.session:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_SESSION,
            "请粘贴 ChatGPT session",
        )

    raw_session = (payload.session or "").strip()
    try:
        bundle = parse_session_bundle(raw_session)
    except ValueError as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.INVALID_SESSION, str(exc))
    
    country = normalize_country(payload.billing_country)
    if not is_supported(country):
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, f"不支持的地区: {payload.billing_country}")

    billing = None
    if payload.billing is not None:
        billing = {key: value.strip() for key, value in payload.billing.model_dump().items()}
        billing["country"] = normalize_country(billing["country"])
        if not is_supported(billing["country"]):
            raise _error(400, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "不支持的账单国家")
        if not all(billing[key] for key in ("name", "line1", "city", "postal_code")):
            raise _error(400, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "请填写完整的真实账单地址")
        if billing["country"] in {"US", "CA"} and not billing["state"]:
            raise _error(400, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "请填写账单地址的州或省")
    
    # 换 token 与后续提链、支付共用同一个出口（同一 sid），所以只算一次。
    exit_proxy = align_proxy_region(
        session_proxy("checkout", country) or configured_proxy("checkout"),
        country,
    )

    # 只粘贴了 session token：先用本单出口换 accessToken，再按完整 session 继续。
    if not bundle.access_token and bundle.session_token:
        client = ChatGPTClient.from_raw_session(raw_session, proxy=exit_proxy)
        try:
            refreshed = await asyncio.to_thread(client.refresh_session)
        except Exception as exc:
            logger.warning("session token 换取失败: %s", redact_sensitive(str(exc))[:200])
            refreshed = False
        finally:
            client.close()
        if not refreshed or not client.access_token:
            raise _error(
                status.HTTP_401_UNAUTHORIZED,
                ErrorCode.INVALID_SESSION,
                "session token 已失效或无法换取 accessToken，请重新导出 session",
            )
        bundle.access_token = client.access_token
        raw_session = json.dumps({
            "accessToken": client.access_token,
            "sessionToken": client.session_token,
            "deviceId": client.device_id,
            "sessionId": client.oai_session_id,
            "cookies": client.extra_cookies,
        })
        logger.info("已从 session token 换取 accessToken")

    if not bundle.access_token:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.INVALID_SESSION, "JSON 中没有 accessToken")

    seats = 5
    workspace = str(payload.workspace_name or "").strip()
    promo_code = str(payload.promo_code or "").strip()
    promo_campaign_id = str(payload.promo_campaign_id or "").strip()
    interval = "year" if str(payload.price_interval or "").lower() == "year" else "month"
    if plan is Plan.TEAM:
        if payload.seat_quantity is None:
            raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, "Team 需要填写席位，最少 2 人")
        try:
            seats = clamp_team_seats(payload.seat_quantity)
        except ValueError as exc:
            raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, str(exc))
        if not workspace:
            workspace = "Team Workspace"

    order = Order(
        idempotency_key=idempotency_key or "",
        plan=plan,
        billing_country=country,
        mode=mode,
        status=OrderStatus.CREATED,
    )
    store.create_order(order, idempotency_key)
    store.add_job(Job(order_id=order.id, stage="checkout"))
    # 指纹 profile：与建单 client 用同一个 device_id 推导，保证全程一致
    try:
        from gss_core.chatgpt import _pick_profile as _pick_prof
        _dev = getattr(bundle, "device_id", "") or ""
        _profile = _pick_prof(_dev)
    except Exception:
        _profile = None
    vault.put(order.id, OrderSecrets(session=raw_session, proxy=exit_proxy or "",
                                  profile=_profile))
    # 建单重试可能换了 SID —— 用最终生效的出口覆盖，保证支付同出口

    pipeline = build_pipeline()
    try:
        checkout, exit_proxy = await _create_checkout_with_retry(
            pipeline,
            proxy=exit_proxy or "",
            session=raw_session,
            plan=plan,
            billing_country=country,
            mode=mode,
            promo_code=promo_code,
            promo_campaign_id=promo_campaign_id,
            workspace_name=workspace,
            seat_quantity=seats,
            price_interval=interval,
            billing=billing,
        )
    except DomainError as exc:
        store.update_status(order.id, OrderStatus.REJECTED)
        store.view(order.id).last_error = exc.message
        logger.info(
            "checkout_failed id=%s plan=%s country=%s error=%s",
            order.id,
            plan,
            country,
            exc.message,
        )
        raise _from_domain(exc)

    # 建单最终生效的出口：重试可能换过 SID，支付必须复用
    _s = vault.get(order.id)
    if _s is not None:
        _s.proxy = exit_proxy or ""
        vault.put(order.id, _s)

    view = store.view(order.id)
    view.checkout_url = checkout.checkout_url
    view.checkout_session_id = checkout.checkout_session_id
    view.publishable_key = checkout.publishable_key
    view.customer_session_client_secret = checkout.customer_session_client_secret
    view.billing = dict(checkout.billing or {})
    view.amount = str(checkout.amount) if checkout.amount is not None else None
    view.currency = checkout.currency
    view.quote_error = getattr(checkout, "quote_error", None) or None
    view.checkout_kind = checkout.checkout_kind or ("hosted" if plan is Plan.TEAM else "custom")
    view.confirm_return_url = checkout.confirm_return_url or None
    view.promo_code = promo_code or None
    view.promo_campaign_id = promo_campaign_id or None
    if plan is Plan.TEAM:
        view.seat_quantity = seats
        view.workspace_name = workspace
        view.price_interval = interval
    order.amount = checkout.amount
    secrets = vault.get(order.id)
    if secrets is not None:
        secrets.checkout_session_id = checkout.checkout_session_id or ""
        secrets.publishable_key = checkout.publishable_key or ""
        secrets.billing = dict(checkout.billing or {})
        effective_proxy = getattr(checkout, "effective_proxy", None)
        if effective_proxy is not None:
            secrets.proxy = effective_proxy
        vault.put(order.id, secrets)
    store.update_status(order.id, OrderStatus.PENDING_CHECKOUT)
    store.update_status(order.id, OrderStatus.AWAITING_PAYMENT)
    logger.info(
        "checkout_created id=%s plan=%s country=%s amount=%s currency=%s kind=%s seats=%s",
        order.id,
        plan,
        country,
        view.amount,
        view.currency,
        view.checkout_kind,
        view.seat_quantity,
    )

    if payload.payment_method_token:
        return await pay_order(order.id, OrderPayRequest(payment_method_token=payload.payment_method_token))
    return _to_out(store.get_order(order.id))  # type: ignore[arg-type]


@router.post("/orders/{order_id}/quote", response_model=OrderOut)
async def refresh_checkout_quote(order_id: str) -> OrderOut:
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "订单已进入付款流程，请查询原订单状态")
        secrets = vault.get(order_id)
        if secrets is None:
            raise HTTPException(401, "订单凭证已过期，请重新创建结账")
        if secrets.saved_card_context:
            raise HTTPException(409, "订单已开始支付处理，请使用协议状态查询")
        view = store.view(order_id)
        if use_fake_adapters():
            return _to_out(order)
        client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
        client.timeout = 30.0
        try:
            amount, currency = await asyncio.to_thread(
                client._quote_from_taxes,
                checkout_id=secrets.checkout_session_id,
                page_url=view.checkout_url or "",
                currency=view.currency or "",
                billing=secrets.billing,
                processor=_processor_from_checkout_url(view.checkout_url or ""),
                strict=True,
            )
            if amount is None or not currency:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "报价暂未取得，请稍后刷新报价")
            view.amount, view.currency = str(amount), currency
            order.amount = amount
            view.quote_error = None
        except DomainError as exc:
            view.amount, order.amount = None, None
            view.quote_error = exc.message
        except Exception:
            view.amount, order.amount = None, None
            view.quote_error = "读取报价时连接中断，请重试刷新报价"
        finally:
            client.close()
        return _to_out(order)


async def _mark_delivered(order_id: str) -> None:
    """扣款成功后直接推进到 delivered，并清掉内存里的 session。"""
    current = store.get_order(order_id)
    if current is not None and current.status is OrderStatus.VERIFYING:
        store.update_status(order_id, OrderStatus.ACTIVE)
    current = store.get_order(order_id)
    if current is not None and current.status is OrderStatus.ACTIVE:
        store.update_status(order_id, OrderStatus.DELIVERED)
    store.view(order_id).last_error = None
    store.view(order_id).next_action = None
    vault.drop(order_id)
    await notify_order_update(order_id, OrderStatus.DELIVERED, {"payment_confirmed": True})


def _team_client(secrets: OrderSecrets, country: str = "") -> ChatGPTClient:
    """Build a client for this order, optionally exiting from the billing country.

    支付出口必须与创建结账时完全一致：Stripe 把 checkout session 绑定在
    创建时的出口上，按账单国家重新对齐会换掉 IP，导致验证码/风控判定异常。
    country 参数保留仅为兼容既有调用方，不再参与出口选择。
    """
    client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
    client.timeout = 30.0
    return client


def _team_quote(view, context: dict) -> None:
    code = context["currency"].upper()
    zero = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
    divisor = 1 if code in zero else 100
    view.amount = str(Decimal(context["amount_minor"]) / divisor)
    view.currency = code
    view.quote_error = None


@router.post("/orders/{order_id}/team/quote", response_model=OrderOut)
async def quote_team(order_id: str, payload: TeamQuoteRequest) -> OrderOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order = store.get_order(order_id)
        if order is None or order.plan is not Plan.TEAM:
            raise HTTPException(404, "Team 订单不存在")
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "支付已提交，请查询支付状态")
        secrets = vault.get(order_id)
        if secrets is None:
            raise _error(401, ErrorCode.SESSION_EXPIRED, "session 已过期")
        view = store.view(order_id)
        if use_fake_adapters():
            return _to_out(order)
        billing = {k: v.strip() for k, v in payload.billing.model_dump().items()} if payload.billing else None
        if billing:
            billing["country"] = billing["country"].upper()
            if not is_supported(billing["country"]):
                raise HTTPException(400, "不支持的支付账单国家")
        started = time.monotonic()
        client = _team_client(secrets)
        try:
            quote = await asyncio.to_thread(TeamHostedClient(client).quote,
                session_id=secrets.checkout_session_id, key=secrets.publishable_key,
                billing=billing, currency=view.currency or "", browser_locale=payload.browser_locale,
                browser_timezone=payload.browser_timezone)
            _team_quote(view, quote)
            logger.info("team_quote_ok id=%s amount=%s currency=%s elapsed=%.2f",
                        order_id, view.amount, view.currency, time.monotonic() - started)
        except DomainError as exc:
            view.quote_error = exc.message
            logger.warning("team_quote_failed id=%s code=%s elapsed=%.2f error=%s", order_id,
                           exc.code.value, time.monotonic() - started, redact_sensitive(exc.message)[:160])
        except Exception as exc:
            view.quote_error = "读取 Stripe 报价时连接中断，本步骤未提交付款确认"
            logger.warning("team_quote_error id=%s type=%s elapsed=%.2f", order_id, type(exc).__name__,
                           time.monotonic() - started)
        finally:
            client.close()
        return _to_out(order)


@router.post("/orders/{order_id}/team/prepare", tags=["orders"])
async def prepare_team(order_id: str, payload: TeamPrepareRequest) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order = store.get_order(order_id)
        if order is None or order.plan is not Plan.TEAM:
            raise HTTPException(404, "Team 订单不存在")
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "已提交的订单请查询支付状态，不要重新提交")
        secrets = vault.get(order_id)
        if secrets is None:
            raise _error(401, ErrorCode.SESSION_EXPIRED, "session 已过期")
        billing = {k: v.strip() for k, v in payload.billing.model_dump().items()}
        billing["country"] = billing["country"].upper()
        if not is_supported(billing["country"]):
            raise HTTPException(400, "不支持的支付账单国家")
        if use_fake_adapters():
            context = {"context_id": "fixture-context", "session_id": secrets.checkout_session_id,
                "publishable_key": secrets.publishable_key, "eid": "NA", "amount_minor": 115000,
                "currency": "egp", "billing": billing, "prepared_at": time.time(),
                "checkout_config_id": "fixture-config", "elements_session_id": "elements_fixture",
                "client_session_id": "fixture-client", "guid": "fixture-guid", "muid": "fixture-muid", "sid": "fixture-sid",
                "passive_captcha": {}}
        else:
            started = time.monotonic()
            client = _team_client(secrets)
            try:
                context = await asyncio.to_thread(TeamHostedClient(client).prepare,
                    session_id=secrets.checkout_session_id, key=secrets.publishable_key, billing=billing,
                    currency=store.view(order_id).currency or "", browser_locale=payload.browser_locale,
                    browser_timezone=payload.browser_timezone,
                    on_quote=lambda quote: _team_quote(store.view(order_id), quote))
                logger.info("team_prepare_ok id=%s amount=%s currency=%s elapsed=%.2f",
                            order_id, store.view(order_id).amount, store.view(order_id).currency,
                            time.monotonic() - started)
            except DomainError as exc:
                store.view(order_id).last_error = exc.message
                logger.warning("team_prepare_failed id=%s code=%s elapsed=%.2f confirm_submitted=False error=%s",
                               order_id, exc.code.value, time.monotonic() - started,
                               redact_sensitive(exc.message)[:160])
                raise _from_domain(exc)
            finally:
                client.close()
        secrets.team_context = context
        secrets.billing = billing
        vault.put(order_id, secrets)
        view = store.view(order_id)
        view.billing = billing
        view.last_error = None
        _team_quote(view, context)
        return {"order": _to_out(order).model_dump(), "context": {k: context[k] for k in (
            "context_id", "session_id", "publishable_key", "eid", "amount_minor", "currency",
            "checkout_config_id", "elements_session_id", "client_session_id", "guid", "muid", "sid",
            "passive_captcha", "customer_email") if k in context}}


async def _solve_challenge(order_id: str, secrets, view):
    """Solve the stored intent challenge and return the refreshed payment result."""
    from gss_core.captcha import solve_hcaptcha, verify_challenge
    from gss_core.config import load_settings
    from gss_payment.adapter import hosted_payment_result as _hosted_result
    settings = load_settings()
    action = view.next_action or {}
    logger.info("challenge_solve_start id=%s key=%s proxy=%s", order_id,
                bool(settings.captcha_api_key), bool(secrets.proxy))
    if not settings.captcha_api_key:
        logger.warning("challenge_solve_skipped id=%s reason=no_api_key", order_id)
        return None
    challenge = {k: str(action.get(k) or "") for k in ("site_key", "rqdata", "verification_url")}
    if not challenge.get("site_key"):
        return None
    intent_id = str(action.get("payment_intent_id") or "")
    client_secret = str(action.get("client_secret") or "")
    if not intent_id.startswith("pi_") or not client_secret.startswith("pi_"):
        return None
    billing_country = str((getattr(view, "billing", {}) or {}).get("country") or "").upper()
    client = _team_client(secrets, billing_country)
    started = time.monotonic()
    try:
        agent = ""
        try:
            agent = str(client.stripe_http.headers.get("User-Agent") or "")
        except Exception:
            agent = ""
        token = await asyncio.to_thread(solve_hcaptcha, httpx.Client(timeout=40), challenge,
                                        api_key=settings.captcha_api_key, api_url=settings.captcha_api_url,
                                        proxy=secrets.proxy or "", user_agent=agent)
        verified = await asyncio.to_thread(verify_challenge, client.stripe_http, intent_id=intent_id,
                                           client_secret=client_secret, token=token,
                                           publishable_key=secrets.publishable_key,
                                           api_version="2026-03-25.dahlia")
        logger.info("challenge_solved id=%s elapsed=%.1f status=%s", order_id,
                    time.monotonic() - started, verified.get("status"))
        result = _hosted_result({"payment_intent": verified}, secrets.checkout_session_id,
                                view.checkout_url or "")
        view.next_action = None if result.status.value in {"succeeded", "declined"} else result.next_action
        return result
    except Exception as exc:
        logger.warning("challenge_solve_failed id=%s type=%s", order_id, type(exc).__name__)
        return None
    finally:
        client.close()


@router.post("/orders/{order_id}/team/poll", response_model=OrderOut)
async def poll_team(order_id: str) -> OrderOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order = store.get_order(order_id)
        if order is None or order.plan is not Plan.TEAM:
            raise HTTPException(404, "Team 订单不存在")
        if order.status in {OrderStatus.DELIVERED, OrderStatus.FAILED, OrderStatus.REJECTED}:
            return _to_out(order)
        secrets = vault.get(order_id)
        if secrets is None:
            raise _error(401, ErrorCode.SESSION_EXPIRED, "session 已过期")
        client = _team_client(secrets)
        try:
            payload = await asyncio.to_thread(TeamHostedClient(client).poll,
                session_id=secrets.checkout_session_id, key=secrets.publishable_key)
        except DomainError as exc:
            raise _from_domain(exc)
        finally:
            client.close()
        result = hosted_payment_result(payload, secrets.checkout_session_id, store.view(order_id).checkout_url or "")
        view = store.view(order_id)
        # Complete Stripe's intent challenge here: solving is slow, so it runs during poll
        # instead of inside the pay request where the gateway would time out.
        logger.info("team_poll_result id=%s result=%s has_next_action=%s challenge=%s",
                    order_id, result.status.value, bool(view.next_action),
                    str((view.next_action or {}).get("challenge_type") or ""))
        # Stripe's poll payload reports "processing" while the challenge is still open,
        # so trigger on the stored challenge instead of the polled status.
        pending = view.next_action or {}
        if (pending.get("challenge_type") == "intent_confirmation_challenge"
                and result.status.value not in {"succeeded", "declined"}):
            solved = await _solve_challenge(order_id, secrets, view)
            if solved is not None:
                result = solved
        if result.status.value == "succeeded":
            if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.REQUIRES_ACTION, OrderStatus.UNKNOWN}:
                store.update_status(order_id, OrderStatus.PAYING)
            if order.status is OrderStatus.PAYING:
                store.update_status(order_id, OrderStatus.VERIFYING)
            view.next_action = None
            await _mark_delivered(order_id)
        elif result.status.value == "declined":
            store.update_status(order_id, OrderStatus.FAILED)
            view.last_error = result.message
            view.next_action = None
            await notify_order_update(order_id, OrderStatus.FAILED)
        elif result.status.value == "requires_action":
            if order.status is OrderStatus.AWAITING_PAYMENT:
                store.update_status(order_id, OrderStatus.PAYING)
            store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
            view.next_action = result.next_action
            view.last_error = result.message
        # Pending poll responses must not erase an earlier challenge/client_secret.
        return _to_out(order)


@router.post(
    "/orders/{order_id}/pay",
    response_model=OrderOut,
    responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
async def pay_order(order_id: str, payload: OrderPayRequest) -> OrderOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _pay_order(order_id, payload)


async def _pay_order(order_id: str, payload: OrderPayRequest) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status in {OrderStatus.DELIVERED, OrderStatus.ACTIVE}:
        return _to_out(order)
    if order.status is not OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(409, "支付已提交，请查询状态，不要重复确认")
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.SESSION_EXPIRED, "session 已过期，请重新提交")
    _assert_saved_card_resolved(secrets)

    if order.plan is Plan.TEAM and not use_fake_adapters():
        context = secrets.team_context
        address = {k: v.strip() for k, v in payload.billing.model_dump().items()} if payload.billing else dict(secrets.billing)
        address["country"] = address.get("country", "").upper()
        address.setdefault("email", "")
        if not context or context.get("context_id") != payload.team_context_id:
            raise HTTPException(409, "Team 会话上下文不匹配，请重新初始化")
        if time.time() - context["prepared_at"] > 600 or address != context["billing"]:
            raise HTTPException(409, "Team 账单或会话已变更，请重新初始化")
        # Server-side card submission acquires its own pm_ and may run without a browser captcha token.
        if (payload.card is None and context.get("passive_captcha", {}).get("site_key")
                and not payload.passive_captcha_token):
            raise HTTPException(400, "请先完成当前会话的 Stripe 验证")

        if payload.card is not None:
            if payload.payment_method_token:
                raise HTTPException(400, "卡号与 payment_method 只能二选一")
            started = time.monotonic()
            client = _team_client(secrets, address.get("country", ""))
            try:
                pm_id = await asyncio.to_thread(
                    TeamHostedClient(client).tokenize,
                    card=payload.card.model_dump(), billing=address,
                    publishable_key=secrets.publishable_key, session_id=secrets.checkout_session_id,
                    checkout_config_id=context.get("checkout_config_id", ""),
                    device={"guid": context.get("guid", ""), "muid": context.get("muid", ""),
                            "sid": context.get("sid", ""),
                            "client_session_id": context.get("client_session_id", "")},
                    captcha_token=payload.passive_captcha_token or "",
                )
                logger.info("team_tokenize_ok id=%s elapsed=%.2f", order_id, time.monotonic() - started)
            except DomainError as exc:
                logger.warning("team_tokenize_failed id=%s code=%s elapsed=%.2f error=%s", order_id,
                               exc.code.value, time.monotonic() - started,
                               redact_sensitive(exc.message)[:160])
                raise _from_domain(exc)
            finally:
                client.close()
            payload = payload.model_copy(update={"payment_method_token": pm_id})

    if payload.billing is not None:
        custom = {k: v.strip() for k, v in payload.billing.model_dump().items()}
        custom["country"] = custom["country"].upper()
        # order.billing_country 是创建结账的地区（决定币种）；
        # custom.country 是支付账单国家，独立用于 PaymentMethod 和 Stripe tax_region。
        if order.plan is Plan.TEAM:
            if not is_supported(custom["country"]):
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    ErrorCode.BILLING_COUNTRY_MISMATCH,
                    f"不支持的账单国家: {custom['country']}",
                )
        secrets.billing = custom
        vault.put(order_id, secrets)

    view = store.view(order_id)
    if payload.billing is not None:
        view.billing = dict(secrets.billing)
    pipeline = build_pipeline()
    store.update_status(order_id, OrderStatus.PAYING)
    try:
        payment = await pipeline.pay(
            checkout_url=view.checkout_url or "",
            country=order.billing_country,
            mode=order.mode,
            payment_method_token=payload.payment_method_token,
            session=secrets.session,
            checkout_session_id=view.checkout_session_id or secrets.checkout_session_id,
            publishable_key=view.publishable_key or secrets.publishable_key,
            checkout_proxy=secrets.proxy or None,
            approve_proxy=secrets.proxy or None,
            billing=secrets.billing or None,
            plan=str(order.plan), team_context=secrets.team_context,
            passive_captcha_token=payload.passive_captcha_token, rv_timestamp=payload.rv_timestamp,
        )
    except DomainError as exc:
        uncertain = order.plan is Plan.TEAM and exc.code is ErrorCode.PAYMENT_UNKNOWN_STATE
        failed_status = OrderStatus.UNKNOWN if uncertain else OrderStatus.FAILED
        store.update_status(order_id, failed_status)
        view.last_error = exc.message
        logger.info(
            "pay_failed id=%s country=%s error=%s",
            order_id,
            order.billing_country,
            exc.message,
        )
        # SSE 通知：支付失败
        await notify_order_update(order_id, failed_status, {"error": exc.message})
        raise _from_domain(exc)

    new_status = apply_payment_result(OrderStatus.PAYING, payment.status)
    store.update_status(order_id, new_status)
    view.next_action = payment.next_action
    view.last_error = None if payment.status.value == "succeeded" else payment.message
    logger.info(
        "pay_finished id=%s country=%s payment=%s order=%s error=%s",
        order_id,
        order.billing_country,
        payment.status,
        new_status,
        view.last_error or "",
    )
    
    # SSE 通知：支付状态更新
    await notify_order_update(order_id, new_status, {"payment_status": payment.status.value})

    if new_status is OrderStatus.VERIFYING and order.plan is Plan.TEAM:
        await _mark_delivered(order_id)
    elif new_status is OrderStatus.VERIFYING:
        # 业务规则：扣款成功即视为开通成功。订阅查询只作为附带信息，失败或尚未生效都不影响结果。
        try:
            info = await pipeline.verify(session=secrets.session)
            view.subscription = info
        except DomainError as exc:
            logger.warning("verify_after_pay_failed id=%s error=%s", order_id, redact_sensitive(exc.message)[:200])
        await _mark_delivered(order_id)
    return _to_out(store.get_order(order_id))  # type: ignore[arg-type]


@router.post(
    "/orders/{order_id}/verify",
    response_model=OrderOut,
    responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
async def verify_order(order_id: str, paid: bool = False) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED}:
        return _to_out(order)
    if order.plan is Plan.TEAM and not use_fake_adapters():
        return await poll_team(order_id)
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _verify_order(order_id, paid=paid)


async def _verify_order(order_id: str, paid: bool = False) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(404, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED}:
        return _to_out(order)
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.SESSION_EXPIRED, "session 已过期，请重新提交")
    view = store.view(order_id)
    pipeline = build_pipeline()
    if secrets.saved_card_context:
        if secrets.saved_card_context.get("protocol_flow"):
            raise HTTPException(409, "本订单已进入协议绑卡支付，请使用协议状态查询")
        if secrets.saved_card_context.get("state") != "checkout_ready":
            raise HTTPException(409, "请先完成保存卡片并刷新结账链接，再查询订阅")
        # A status query before payment must leave this checkout available.
        try:
            refreshed_session, info = await asyncio.to_thread(_verify_saved_checkout, pipeline, secrets)
            secrets.session = refreshed_session
        except DomainError as exc:
            if exc.code in {ErrorCode.INVALID_SESSION, ErrorCode.SESSION_EXPIRED, ErrorCode.CHECKOUT_UPSTREAM_ERROR}:
                raise _from_domain(exc)
            view.last_error = "订阅状态暂未读到，请稍后重新查询"
            return _to_out(order)
        view.subscription = info
        if info.active:
            if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.REQUIRES_ACTION, OrderStatus.UNKNOWN}:
                store.update_status(order_id, OrderStatus.PAYING)
            if order.status is OrderStatus.PAYING:
                store.update_status(order_id, OrderStatus.VERIFYING)
            store.update_status(order_id, OrderStatus.ACTIVE)
            assert_deliverable(OrderStatus.ACTIVE, info)
            store.update_status(order_id, OrderStatus.DELIVERED)
            view.last_error = None
            vault.drop(order_id)
            await notify_order_update(order_id, OrderStatus.DELIVERED, {"subscription_active": True})
        else:
            view.last_error = "订阅尚未生效，请在新结账页面完成付款后重新查询"
        return _to_out(order)
    current = store.get_order(order_id)
    if current is not None and current.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.REQUIRES_ACTION, OrderStatus.UNKNOWN}:
        # 3DS 验证完成（或不确定态复查）后从这里继续往 verifying 推进。
        store.update_status(order_id, OrderStatus.PAYING)
    current = store.get_order(order_id)
    if current is not None and current.status is OrderStatus.PAYING:
        store.update_status(order_id, OrderStatus.VERIFYING)
    try:
        info = await pipeline.verify(session=secrets.session)
    except DomainError as exc:
        if exc.code in {ErrorCode.INVALID_SESSION, ErrorCode.SESSION_EXPIRED}:
            raise _from_domain(exc)
        # 上游查询暂时出错：保持当前状态返回，由前端稍后重查，不把已支付订单打成失败。
        logger.warning("verify_failed id=%s error=%s", order_id, redact_sensitive(exc.message)[:200])
        if paid:
            await _mark_delivered(order_id)
            return _to_out(store.get_order(order_id))  # type: ignore[arg-type]
        view.last_error = f"订阅状态查询失败，稍后会自动重试：{exc.message[:120]}"
        return _to_out(store.get_order(order_id))  # type: ignore[arg-type]
    view.subscription = info
    if info.active:
        store.update_status(order_id, OrderStatus.ACTIVE)
        assert_deliverable(OrderStatus.ACTIVE, info)
        store.update_status(order_id, OrderStatus.DELIVERED)
        view.last_error = None
        vault.drop(order_id)
        # SSE 通知：订阅激活并交付
        await notify_order_update(order_id, OrderStatus.DELIVERED, {"subscription_active": True})
    elif paid:
        await _mark_delivered(order_id)
    else:
        view.last_error = "订阅尚未生效。若是 Team 长链,请先打开链接完成支付再查询。"
        # SSE 通知：验证失败
        current_status = store.get_order(order_id)
        if current_status:
            await notify_order_update(order_id, current_status.status, {"subscription_active": False})
    return _to_out(store.get_order(order_id))  # type: ignore[arg-type]


@router.get(
    "/orders/{order_id}",
    response_model=OrderOut,
    responses={404: {"model": ErrorOut}},
)
async def get_order(order_id: str) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    return _to_out(order)

# ── 纯协议支付（不依赖浏览器） ────────────────────────────────
class ProtocolStepsIn(BaseModel):
    """协议链路的可选环节。全部默认关闭 = 与 scripts/run_cards.py 一致的批量模式。"""

    fingerprint: bool = False   # m.stripe.com/6 × 4 指纹注册 + fetch_allowed_origins
    dwell: bool = False         # init 后停留 10–16s
    elements: bool = False      # elements/sessions
    tax_region: bool = False    # 提交 tax_region[country]


class ProtocolPayRequest(BaseModel):
    """协议支付的入参。

    两种取 pm_ 的方式，二选一：
      · card              服务端直接向 Stripe 提交卡号（全协议）
      · payment_method_id 浏览器 Stripe 输入框已换好的 pm_（混合模式）
        卡数据走 Stripe iframe，与真实用户一致，hCaptcha/Radar 判定更宽松。
    """
    card: Optional[CardIn] = None
    payment_method_id: Optional[str] = Field(default=None, max_length=80)
    billing: BillingIn
    proxy: Optional[str] = Field(default=None, max_length=300)
    publishable_key: Optional[str] = Field(default=None, max_length=120)
    steps: ProtocolStepsIn = ProtocolStepsIn()


def resolve_protocol_payment_method(payload: "ProtocolPayRequest") -> str:
    """校验 card / payment_method_id 只给一个，返回浏览器给的 pm_（没有就空串）。

    单独抽出来是为了能离线测：pay-protocol 本身会真的打 Stripe，集成测试不该碰。
    """
    pm_id = (payload.payment_method_id or "").strip()
    if not payload.card and not pm_id:
        raise HTTPException(400, "需要 card 或 payment_method_id 其一")
    if payload.card and pm_id:
        raise HTTPException(400, "card 与 payment_method_id 只能二选一")
    if pm_id and not pm_id.startswith("pm_"):
        raise HTTPException(400, "payment_method_id 格式不对（应以 pm_ 开头）")
    return pm_id


class ProtocolPayOut(BaseModel):
    """纯协议支付的结果。"""
    state: str
    code: Optional[str] = None
    decline_code: Optional[str] = None
    message: Optional[str] = None
    intent_status: Optional[str] = None
    charge_id: Optional[str] = None
    payment_method_id: Optional[str] = None


@router.post(
    "/orders/{order_id}/pay-protocol",
    response_model=ProtocolPayOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut},
               409: {"model": ErrorOut}, 502: {"model": ErrorOut}},
)
async def pay_order_protocol(order_id: str, payload: ProtocolPayRequest) -> ProtocolPayOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _pay_order_protocol(order_id, payload)


async def _pay_order_protocol(order_id: str, payload: ProtocolPayRequest) -> ProtocolPayOut:
    """纯 HTTP 协议完成 Stripe Checkout 支付，不需要浏览器。

    链路：指纹注册 → init → elements/sessions → 逐字段地址
          → hCaptcha(passive + challenge) → payment_methods → confirm → poll
    """
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status is not OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(409, "支付已提交，请查询订单状态，不要重复确认")

    view = store.view(order_id)
    checkout_url = (getattr(view, "checkout_url", "") or "") if view else ""
    if not checkout_url:
        raise HTTPException(409, "订单缺少 checkout_url，请先创建结账")

    settings = load_settings()
    captcha_key = getattr(settings, "captcha_api_key", "") or ""
    captcha_url = getattr(settings, "captcha_api_url", "https://api.yescaptcha.com")
    # 出口按「用户选择的账单地区」自动对齐，并且每次支付用新的 SID：
    # 复用配置里固定的 SID 会一直撞同一个出口 IP，抽到坏 IP 时整单必挂。
    proxy = (
        payload.proxy
        or session_proxy("checkout", payload.billing.country)
        or align_proxy_region(configured_proxy("checkout"), payload.billing.country)
    )

    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "订单凭证已过期，请重新创建结账")
    _assert_saved_card_resolved(secrets)
    session_raw = secrets.session or ""
    publishable_key = (
        (payload.publishable_key or "")
        or (getattr(view, "publishable_key", "") or "")
        or ((secrets.publishable_key if secrets else "") or "")
    )
    currency = (getattr(view, "currency", "") or "") or "USD"

    pm_id = resolve_protocol_payment_method(payload)
    card = payload.card.model_dump() if payload.card else {}
    billing = {k: (v or "").strip() for k, v in payload.billing.model_dump().items()}
    billing["country"] = (billing.get("country") or "US").upper()

    # custom checkout 的会话 id 有两种：oaics_（ChatGPT 内部）与 cs_live_（Stripe 自定义）。
    # 单看 URL 前缀会漏掉后者，所以以订单的 checkout_kind 为准。
    is_custom_checkout = (
        str(getattr(view, "checkout_kind", "") or "").lower() == "custom"
        or bool(re.search(r"oaics_[A-Za-z0-9]+", checkout_url))
    )

    def _run() -> PaymentOutcome:
        har_checkout_id = str(getattr(view, "checkout_session_id", "") or "")
        if is_custom_checkout and not pm_id and not har_checkout_id.startswith("cs_"):
            # 协议模式：2026-10-29 HAR 逐请求复刻（指纹 + 遥测，设备每次随机）
            hp = PlusHarProtocol(
                session=session_raw, checkout_url=checkout_url,
                publishable_key=publishable_key, card=card, billing=billing,
                proxy=proxy, currency=currency,
                profile=getattr(secrets, "profile", None),
                customer_session_client_secret=(
                    getattr(view, "customer_session_client_secret", "") or ""),
                email=billing.get("email") or "",
                country=billing.get("country") or "",
                account_id=secrets.account_id,
                checkout_id=getattr(view, "checkout_session_id", "") or "",
                confirm_return_url=getattr(view, "confirm_return_url", "") or "",
            )
            try:
                return hp.pay_from_url()
            finally:
                hp.close()
        if is_custom_checkout:
            # 输入框模式：浏览器已换好 pm_，服务端只做确认，不接触卡号
            pp = PlusProtocol(
                session=session_raw, checkout_url=checkout_url,
                publishable_key=publishable_key, card=card, billing=billing,
                proxy=proxy, currency=currency,
                profile=getattr(secrets, "profile", None),
            )
            pp.external_pm = pm_id
            try:
                return pp.pay_from_url()
            finally:
                pp.close()
        sp = StripeProtocol(
            proxy=proxy, captcha_key=captcha_key, captcha_url=captcha_url,
            card=card, billing=billing,
            profile=getattr(secrets, "profile", None),
            publishable_key=publishable_key,
        )
        # 默认全关（批量模式）；网页上勾选后逐项打开
        sp.step_fingerprint = payload.steps.fingerprint
        sp.step_dwell = payload.steps.dwell
        sp.step_elements = payload.steps.elements
        sp.step_tax_region = payload.steps.tax_region
        # 浏览器已经换好 pm_ 时复用它，服务端不再提交卡号
        sp.external_pm = pm_id
        try:
            return sp.pay_from_url(checkout_url)
        finally:
            sp.close()

    try:
        outcome = await asyncio.to_thread(_run)
    except DomainError as exc:
        raise _from_domain(exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("protocol_pay_failed id=%s error=%s", order_id, str(exc)[:200])
        raise _error(status.HTTP_502_BAD_GATEWAY, ErrorCode.CHECKOUT_UPSTREAM_ERROR, str(exc)[:200])

    logger.info(
        "protocol_pay id=%s state=%s decline=%s code=%s",
        order_id, outcome.state, outcome.decline_code, outcome.code,
    )
    return ProtocolPayOut(
        state=outcome.state, code=outcome.code,
        decline_code=outcome.decline_code, message=outcome.message,
        intent_status=outcome.intent_status, charge_id=outcome.charge_id,
        payment_method_id=outcome.payment_method_id,
    )

