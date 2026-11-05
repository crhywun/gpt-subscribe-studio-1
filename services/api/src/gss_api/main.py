"""FastAPI 应用入口。默认只监听回环地址。"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gss_core import install_logging_filter
from gss_core.config import load_settings, missing_upstreams, use_fake_adapters
from gss_core.proxies import GatewayStore

from .routers import events, orders, promo, proxies, saved_cards, protocol_cards

__version__ = "0.1.0"


def _ensure_info_logging() -> None:
    gss = logging.getLogger("gss")
    gss.setLevel(logging.INFO)
    if not gss.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        gss.addHandler(handler)
    gss.propagate = False


def create_app() -> FastAPI:
    install_logging_filter()
    _ensure_info_logging()
    install_logging_filter(logging.getLogger("gss"))
    settings = load_settings()

    app = FastAPI(
        title="gpt-subscribe-studio API",
        version=__version__,
        description="独立的 ChatGPT 订阅开通工具：粘贴 session，浏览器填卡。",
    )
    app.include_router(orders.router)
    app.include_router(saved_cards.router)
    app.include_router(protocol_cards.router)
    app.include_router(promo.router)
    app.include_router(events.router)
    app.state.gateway_store = GatewayStore()
    app.include_router(proxies.router)

    @app.get("/api/health", tags=["health"])
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "promo_enabled": settings.enable_promo_mode,
            "fake_adapters": use_fake_adapters(),
            "missing_upstreams": missing_upstreams(settings),
        }

    # Distinguish local client configuration errors from upstream network errors.
    try:
        from curl_cffi.requests.exceptions import (
            ConnectionError as _CurlConnectionError,
            ImpersonateError as _ImpersonateError,
            ProxyError as _CurlProxyError,
            Timeout as _CurlTimeout,
        )
        _CurlNetworkErrors = (_CurlConnectionError, _CurlProxyError, _CurlTimeout)
    except Exception:  # noqa: BLE001
        _CurlNetworkErrors = ()
        _ImpersonateError = None  # type: ignore[assignment]
    try:
        import httpx as _httpx
        _HttpxErr = _httpx.TransportError
    except Exception:  # noqa: BLE001
        _HttpxErr = None  # type: ignore[assignment]

    _CONN_HINTS = (
        "SSL", "Connection closed", "ConnectError", "ConnectionError",
        "CurlError", "Temporary failure", "timed out", "Timeout",
        "RemoteDisconnected", "EOF", "reset by peer",
    )

    @app.exception_handler(Exception)
    async def _conn_error_handler(request, exc):  # noqa: ANN001
        if _ImpersonateError is not None and isinstance(exc, _ImpersonateError):
            return JSONResponse(
                status_code=500,
                content={"code": "http_client_configuration_error",
                         "detail": "本机 HTTP 依赖不支持所需浏览器版本，请更新项目依赖并重启服务。"},
            )
        is_conn = (
            isinstance(exc, _CurlNetworkErrors)
            or (_HttpxErr is not None and isinstance(exc, _HttpxErr))
            or any(k in str(exc) for k in _CONN_HINTS)
        )
        if is_conn:
            return JSONResponse(
                status_code=503,
                content={"detail": "连接上游服务失败或超时。请检查代理到目标服务的连通性，"
                                   "必要时重试或更换线路。"},
            )
        raise exc

    return app


app = create_app()
