"""Start this checkout using only its local runtime and virtual environment."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "runtime" / "local"
os.chdir(ROOT)

for line in (ROOT / ".env.local").read_text(encoding="utf-8-sig").splitlines():
    if line.strip() and not line.lstrip().startswith("#"):
        key, separator, value = line.partition("=")
        if separator:
            os.environ[key.strip()] = value.strip()

for package in ("packages/contracts", "packages/core", "services/account", "services/checkout",
                "services/payment", "services/api", "services/orchestrator", "deploy/server"):
    sys.path.insert(0, str(ROOT / package / ("src" if package != "deploy/server" else "")))

os.environ.update({
    "GSS_AUTH_FILE": str(LOCAL / "config" / "container-auth.json"),
    "GSS_DATA_DIR": str(LOCAL / "data"),
    "GSS_CONFIG_DIR": str(LOCAL / "config"),
    "GSS_PROXY_CONFIG": str(LOCAL / "data" / "proxy-gateways.json"),
    "GSS_WEB_DIR": str(ROOT / "apps" / "web" / "dist"),
    "GSS_LOGIN_FILE": str(ROOT / "deploy" / "server" / "login.html"),
    "NODE_PATH": str(LOCAL / "node" / "node_modules"),
    "GSS_BIND_HOST": "127.0.0.1",
    "GSS_BIND_PORT": "18443",
})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=18443,
                ssl_keyfile=str(LOCAL / "certs" / "localhost.key"),
                ssl_certfile=str(LOCAL / "certs" / "localhost.crt"))
