# syntax=docker/dockerfile:1.7
# The previous Dockerfile `git clone`d an unrelated upstream
# (FfmpegZZZ/JMComic-Api), so the published image's code never matched this
# repository. Now: COPY local src + pyproject.

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install runtime deps first so they're cached separately from source changes.
COPY pyproject.toml ./
COPY src/ src/
COPY README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python --no-deps -e . && \
    uv pip install --python /app/.venv/bin/python \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" "jmcomic==2.6.18" \
        "pypdf>=5" "Pillow>=11.1.0" "pydantic>=2.7" "pydantic-settings>=2.4" \
        "PyYAML>=6.0" "structlog>=24.4"


FROM python:3.12-slim AS runtime

WORKDIR /app

# Bring over the venv + source from the builder.
COPY --from=builder /app /app
COPY option.yml /app/option.yml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JMAPI_PDF_DIR=/app/pdf \
    JMAPI_OPTION_FILE=/app/option.yml \
    JMAPI_LOG_FORMAT=json

RUN mkdir -p /app/pdf /app/webp
VOLUME ["/app/pdf", "/app/webp"]

EXPOSE 8699

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8699/health/live', timeout=2).status==200 else 1)" || exit 1

# uvicorn is the canonical entry; SIGHUP triggers option.yml hot-reload
# (`docker kill -s HUP <id>`).
CMD ["uvicorn", "jmcomic_api.app:app", "--host", "0.0.0.0", "--port", "8699"]
