"""出口代理配置。提链与支付分阶段，互不覆盖。"""

from __future__ import annotations

import ipaddress
import time

import httpx
from fastapi import APIRouter, HTTPException, Request

from gss_core.proxies import ROLES, GatewayStore, describe_gateway, gateway_from_input

router = APIRouter(prefix="/api/settings/proxies", tags=["settings"])


def store(request: Request) -> GatewayStore:
    return request.app.state.gateway_store


@router.get("")
async def get_proxies(request: Request):
    try:
        return {role: store(request).get(role).public() for role in ROLES}
    except (OSError, ValueError, TypeError):
        raise HTTPException(500, "代理配置读取失败")


@router.post("/parse")
async def parse_proxy(request: Request):
    """预览：把粘贴的代理串拆开，不保存。"""
    try:
        values = await request.json()
        return describe_gateway(gateway_from_input(str(values.get("raw", ""))))
    except ValueError as exc:
        raise HTTPException(400, str(exc) if type(exc) is ValueError else "代理格式不正确")
    except (TypeError, AttributeError):
        raise HTTPException(400, "代理格式不正确")


@router.put("")
async def save_proxy_all(request: Request):
    """保存同一个代理到提链与支付两个阶段，保证同一订单出口一致。"""
    try:
        values = await request.json()
        enabled = values.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("启用状态格式不正确")
        gateway = gateway_from_input(str(values.get("raw", "")), enabled=enabled)
        fields = {
            "host": gateway.host,
            "port": gateway.port,
            "scheme": gateway.scheme,
            "username": gateway.username,
            "password": gateway.password,
            "region": gateway.region,
            "sid": gateway.sid,
            "enabled": gateway.enabled,
        }
        for role in ROLES:
            store(request).save(role, {**fields, "clear_auth": False})
        return describe_gateway(gateway)
    except ValueError as exc:
        raise HTTPException(400, str(exc) if type(exc) is ValueError else "代理格式不正确")
    except (TypeError, AttributeError, OSError):
        raise HTTPException(400, "代理配置保存失败，请检查参数与文件权限")


@router.put("/{role}")
async def save_proxy(role: str, request: Request):
    if role not in ROLES:
        raise HTTPException(404, "未知代理阶段")
    try:
        values = await request.json()
        gateway = store(request).save(role, values)
        return gateway.public()
    except ValueError as exc:
        message = str(exc) if type(exc) is ValueError else "代理配置格式不正确"
        raise HTTPException(400, message)
    except (TypeError, OSError):
        raise HTTPException(400, "代理配置保存失败，请检查参数与文件权限")


async def probe_gateway(proxy_url: str) -> dict:
    start = time.perf_counter()
    async with httpx.AsyncClient(
        proxy=proxy_url, timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False, trust_env=False
    ) as client:
        # Use the supplier's documented IPv4 check; api64 may route to IPv6
        # that is unavailable through an otherwise working residential exit.
        response = await client.get("https://api.ipify.org?format=json")
        response.raise_for_status()
        ip = str(ipaddress.ip_address(response.json()["ip"]))
    return {"ip": ip, "elapsed_ms": round((time.perf_counter() - start) * 1000)}


@router.post("/{role}/test")
async def test_proxy(role: str, request: Request):
    if role not in ROLES:
        raise HTTPException(404, "未知代理阶段")
    try:
        gateway = store(request).get(role)
        proxy_url = gateway.proxy_url()
        if proxy_url is None:
            raise HTTPException(400, "请先启用并保存该阶段代理")
        result = await probe_gateway(proxy_url)
        return {"role": role, "region": gateway.region, "sid": gateway.sid, **result}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(502, "代理连接超时。未写协议时默认 HTTP；SOCKS5 线路请使用 socks5h://账号:密码@主机:端口，并核对供应商端口。")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
        raise HTTPException(502, "代理连接失败，请检查网关、账号密码和供应商白名单后重试")
