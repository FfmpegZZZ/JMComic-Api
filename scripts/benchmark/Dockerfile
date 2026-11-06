# Compatible with both the classic Docker builder and BuildKit.
# Context is either the working tree or an unmodified git archive of baseline.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev
RUN groupadd --gid 10001 jmapi && useradd --uid 10001 --gid jmapi --no-create-home jmapi \
    && mkdir -p /data && chown -R jmapi:jmapi /app /data
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/harness PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["uvicorn", "fixture_app:app", "--host", "0.0.0.0", "--port", "8699", "--no-access-log", "--timeout-graceful-shutdown", "30"]
