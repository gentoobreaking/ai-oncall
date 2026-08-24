# oncall-core — Python 智能層 daemon（uv + alpine）
FROM python:3.12-alpine AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/
WORKDIR /app
COPY core/pyproject.toml core/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-alpine
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -g 10001 oncall-core && adduser -D -H -u 10001 -G oncall-core oncall-core
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY core/src ./src
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONUNBUFFERED=1
# SQLite 資料落 /data volume——先以 root 建立目錄並賦權，避免唯讀
RUN mkdir -p /data && chown -R oncall-core:oncall-core /data
VOLUME ["/data"]
USER oncall-core
EXPOSE 50051 8090
CMD ["python", "-m", "oncall_core", \
     "--db", "/data/oncall.db", \
     "--addr", "0.0.0.0:50051", \
     "--readapi-addr", "0.0.0.0:8090"]
