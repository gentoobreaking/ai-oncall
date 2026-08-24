# oncall-ui — 唯讀 Web（uv + alpine；模板隨套件原始碼進映像）
FROM python:3.12-alpine AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/
WORKDIR /app
COPY ui/pyproject.toml ui/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-alpine
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -g 10002 oncall-ui && adduser -D -H -u 10002 -G oncall-ui oncall-ui
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY ui/src ./src
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    READAPI_URL=http://core:8090
USER oncall-ui
EXPOSE 8091
CMD ["python", "-m", "oncall_ui", "--host", "0.0.0.0", "--port", "8091"]
