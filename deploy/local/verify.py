"""Local deployment smoke checks; never creates orders or contacts payment services."""
import json
from pathlib import Path
import re
import ssl

import httpx

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "runtime" / "local"
URL = "https://localhost:18443"
context = ssl.create_default_context(cafile=str(LOCAL / "certs" / "localhost.crt"))
password = json.loads((LOCAL / "config" / "container-auth.json").read_text())["password"]

def check(condition, message):
    if not condition:
        raise RuntimeError(message)
    print("PASS:", message)

with httpx.Client(base_url=URL, verify=context, trust_env=False, timeout=15) as client:
    check(client.get("/login").status_code == 200, "HTTPS login page")
    check(client.get("/api/health").status_code == 401, "API requires authentication")
    check(client.post("/login", data={"password": "incorrect-deployment-smoke-test"}).status_code == 401,
          "Incorrect password is rejected")
    check(client.post("/login", data={"password": password}, headers={"Origin": "https://invalid.example"}).status_code == 403,
          "Unexpected login origin is rejected")
    response = client.post("/login", data={"password": password}, headers={"Origin": URL})
    cookie = response.headers.get("set-cookie", "")
    check(response.status_code == 303 and "__Host-gss_session=" in cookie and "Secure" in cookie and "HttpOnly" in cookie,
          "Login creates a secure session")
    response = client.get("/api/health")
    health = response.json()
    check(response.status_code == 200 and health.get("status") == "ok" and health.get("fake_adapters") is False
          and health.get("promo_enabled") is False and health.get("missing_upstreams") == [],
          "Backend is healthy with real adapters and full-price mode")
    response = client.get("/")
    check(response.status_code == 200 and 'id="root"' in response.text, "Built application is served")
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
    check(bool(assets), "Built frontend has asset references")
    for asset in assets:
        response = client.get(asset)
        check(response.status_code == 200 and len(response.content) > 0, "Frontend asset: " + asset)
    response = client.get("/api/settings/proxies")
    check(response.status_code == 200 and all(role in response.json() for role in ("checkout", "approve")),
          "Proxy settings can be read")
    # Initial empty proxy config: an idempotent write verifies the native data directory is writable.
    if not response.json()["checkout"]["enabled"]:
        response = client.put("/api/settings/proxies/checkout", json={"enabled": False}, headers={"Origin": URL})
        check(response.status_code == 200 and response.json()["enabled"] is False,
              "Proxy settings can be persisted")
    response = client.post("/logout", headers={"Origin": URL})
    check(response.status_code == 303 and client.get("/api/health").status_code == 401,
          "Logout ends the session")

print("Deployment checks completed. No order or payment requests were sent.")
