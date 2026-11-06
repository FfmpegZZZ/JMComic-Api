# Compatible with both the classic Docker builder and BuildKit.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY README.md ./
RUN uv sync --locked --no-dev

FROM builder AS test
RUN uv sync --locked --extra dev
COPY tests/ tests/
COPY scripts/ scripts/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["sh", "scripts/container-checks.sh"]

FROM python:${PYTHON_VERSION}-slim AS runtime
WORKDIR /app
COPY --from=builder /app /app
COPY option.yml /app/option.yml
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    JMAPI_PDF_DIR=/app/pdf JMAPI_PDF_SHARD_CACHE_DIR=/app/pdf_cache \
    JMAPI_OPTION_FILE=/app/option.yml JMAPI_LOG_FORMAT=json
RUN groupadd --gid 10001 jmapi && useradd --uid 10001 --gid jmapi --no-create-home jmapi \
    && mkdir -p /app/pdf /app/webp /app/pdf_cache && chown -R jmapi:jmapi /app
USER 10001:10001
VOLUME ["/app/pdf", "/app/webp", "/app/pdf_cache"]
EXPOSE 8699
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8699/health/live', timeout=2)"
CMD ["uvicorn", "jmcomic_api.app:app", "--host", "0.0.0.0", "--port", "8699", "--timeout-graceful-shutdown", "30"]
