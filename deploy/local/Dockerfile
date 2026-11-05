FROM node:20-bookworm-slim AS frontend
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

FROM node:20-bookworm-slim AS nodeenv
RUN npm install --prefix /opt/nodedeps --omit=dev --no-audit --no-fund undici@6

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=nodeenv /usr/local/bin/node /usr/local/bin/node
COPY --from=nodeenv /opt/nodedeps/node_modules /app/node_modules
ENV NODE_PATH=/app/node_modules
COPY deploy/server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY packages/ /app/packages/
COPY services/ /app/services/
COPY deploy/server/app.py /app/app.py
COPY deploy/server/login.html /app/login.html
COPY --from=frontend /web/dist/ /app/web/
ENV PYTHONPATH=/app/packages/contracts/src:/app/packages/core/src:/app/services/account/src:/app/services/checkout/src:/app/services/payment/src:/app/services/api/src:/app/services/orchestrator/src
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18120"]
