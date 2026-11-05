from __future__ import annotations

import pytest
from urllib.parse import unquote, urlsplit

from gss_core.proxies import (
    GatewayStore, align_proxy_region, describe_gateway, gateway_from_input,
    parse_proxy_input, proxy_exit_region, rewrite_proxy_exit, rotate_proxy_sid, session_proxy,
)


def test_parse_url_with_region_and_sid() -> None:
    gw = gateway_from_input("http://acct-region-US-sid-AbC123:p%40ss@gw.example.com:3010")
    assert (gw.scheme, gw.host, gw.port, gw.password) == ("http", "gw.example.com", 3010, "p@ss")
    assert (gw.region, gw.sid) == ("US", "AbC123")
    info = describe_gateway(gw)
    assert info["has_region_param"] and info["has_sid_param"]
    assert "password" not in info and "username" not in info


@pytest.mark.parametrize(
    "raw",
    [
        "acct-region-JP-sid-x1:secret@gw.example.com:3010",
        "gw.example.com:3010:acct-region-JP-sid-x1:secret",
        "socks5://acct-region-JP-sid-x1:secret@gw.example.com:3010",
    ],
)
def test_parse_accepts_common_formats(raw: str) -> None:
    gw = gateway_from_input(raw)
    assert gw.host == "gw.example.com" and gw.port == 3010
    assert gw.region == "JP" and gw.sid == "x1"
    assert gw.password == "secret"


def test_parse_keeps_provider_key_names_and_case() -> None:
    raw = "http://u_country_us_session_abc123:pw@gw.example.com:8000"
    gw = gateway_from_input(raw)
    assert (gw.region, gw.sid) == ("US", "abc123")
    out = rewrite_proxy_exit(gw.proxy_url(), region="JP", sid="NEW999")
    assert "u_country_jp_session_NEW999" in out  # 键名和小写风格沿用供应商写法


def test_parse_without_tokens_is_flagged() -> None:
    info = describe_gateway(gateway_from_input("gw.example.com:3010"))
    assert not info["has_region_param"] and not info["has_sid_param"]


@pytest.mark.parametrize("raw", ["", "not a proxy", "gw.example.com", "http://gw.example.com", "a b:1"])
def test_parse_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_proxy_input(raw)


def test_123proxy_plus_country_is_detected_without_changing_credentials() -> None:
    gw = gateway_from_input("socks5h://account%2Bph:p%40ss@residential.123proxy.cn:32000")
    assert (gw.scheme, gw.region, gw.sid) == ("socks5h", "PH", "")
    assert (gw.username, gw.password) == ("account+ph", "p@ss")
    info = describe_gateway(gw)
    assert info["routing_format"] == "123proxy"
    assert info["has_region_param"] and not info["has_sid_param"]
    assert "username" not in info and "password" not in info


def test_123proxy_host_port_input_keeps_supplier_country_suffix() -> None:
    gw = gateway_from_input("residential.123proxy.cn:32000:account+ph:secret")
    assert gw.region == "PH" and gw.username == "account+ph"
    assert gw.scheme == "http"  # The caller must explicitly choose SOCKS5 for a SOCKS5 port.


def test_123proxy_sticky_session_survives_save_and_order_routing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GSS_PROXY_SID", raising=False)
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(tmp_path / "proxy.json"))
    store = GatewayStore()
    store.save("checkout", {"host": "residential.123proxy.cn", "port": 32000, "scheme": "socks5h",
                            "username": "account-sess_a8F3kP9xQ2mL_60+ph", "password": "p@ss", "enabled": True})
    gw = store.get("checkout")
    assert (gw.region, gw.sid) == ("PH", "a8F3kP9xQ2mL")
    routed = session_proxy("checkout", "PH")
    assert unquote(urlsplit(routed).username) == gw.username
    assert session_proxy("checkout", "PH") == routed
    aligned = align_proxy_region(routed, "JP")
    assert unquote(urlsplit(aligned).username) == "account-sess_a8F3kP9xQ2mL_60+jp"
    assert proxy_exit_region(aligned) == "JP"
    assert unquote(urlsplit(aligned).password) == "p@ss"
    assert store.get("checkout").region == "PH"


def test_123proxy_no_session_is_not_given_generic_suffixes(monkeypatch) -> None:
    original = gateway_from_input("residential.123proxy.cn:32000:account+ph:pw").proxy_url()
    monkeypatch.setattr("gss_core.proxies.configured_proxy", lambda role: original)
    routed = session_proxy("checkout", "PH")
    assert unquote(urlsplit(routed).username) == "account+ph"
    assert "-region-" not in routed and "-sid-" not in routed


def test_123proxy_rotation_uses_twelve_character_session_and_preserves_duration(monkeypatch) -> None:
    monkeypatch.delenv("GSS_PROXY_SID", raising=False)
    gw = gateway_from_input("residential.123proxy.cn:32000:account-sess_a8F3kP9xQ2mL_60+ph:pw")
    rotated = gateway_from_input(rotate_proxy_sid(gw.proxy_url()))
    assert len(rotated.sid) == 12 and rotated.sid.isalnum()
    assert rotated.sid != gw.sid
    assert rotated.username == f"account-sess_{rotated.sid}_60+ph"


def test_123proxy_explicit_session_insertion_keeps_country_last() -> None:
    original = gateway_from_input("residential.123proxy.cn:32000:account+ph:pw").proxy_url()
    result = rewrite_proxy_exit(original, region="US", sid="a8F3kP9xQ2mL")
    assert unquote(urlsplit(result).username) == "account-sess_a8F3kP9xQ2mL_15+us"


@pytest.mark.parametrize("suffix", ["-sess_short_15+ph", "-sess_a8F3kP9xQ2mL_0+ph", "-sess_a8F3kP9xQ2mL_121+ph"])
def test_123proxy_invalid_session_is_rejected(suffix) -> None:
    with pytest.raises(ValueError, match="123Proxy SESSION"):
        gateway_from_input(f"residential.123proxy.cn:32000:account{suffix}:pw")


def test_123proxy_invalid_pinned_session_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("GSS_PROXY_SID", "short")
    original = gateway_from_input("residential.123proxy.cn:32000:account+ph:pw").proxy_url()
    with pytest.raises(ValueError, match="GSS_PROXY_SID"):
        rotate_proxy_sid(original)


def test_other_provider_plus_suffix_is_not_assumed_to_be_123proxy() -> None:
    info = describe_gateway(gateway_from_input("other.example.com:32000:account+ph:pw"))
    assert info["routing_format"] == "standard" and not info["has_region_param"]
