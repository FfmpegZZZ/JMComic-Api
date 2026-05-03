# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Reference docs (consult before guessing)

This project depends heavily on the third-party `jmcomic` library. **Anything you don't know about jmcomic — API signatures, plugin kwargs, exception types, `option.yml` fields, `dir_rule` DSL — you MUST look up in the docs/source before writing code. Do not rely on memory or training data; the library evolves quickly.**

- **PyPI / version**: https://pypi.org/project/jmcomic/ — pin in `pyproject.toml` is `==2.6.18` (2026-04-07)
- **中文文档首页**: https://jmcomic.readthedocs.io/zh-cn/latest/
- **GitHub source** (authoritative when docs are incomplete): https://github.com/hect0x7/JMComic-Crawler-Python
  - `src/jmcomic/jm_plugin.py` — built-in plugins including `Img2pdfPlugin` (kwargs: `pdf_dir`, `filename_rule`, `dir_rule`, `delete_original_file`, `encrypt`)
  - `src/jmcomic/jm_option.py` — option/dir_rule parsing, `AFIELD_ADVICE` hook (still present in 2.6.18, installed once at app startup in `services/jm_client.py::install_album_dirname_advice`)
  - `src/jmcomic/jm_client_impl.py` — `JmApiClient.search_site` / `categories_filter` signatures, `JmSearchPage.page_count` / `total`
  - `src/jmcomic/jm_exception.py` — `JmcomicException` hierarchy (`MissingAlbumPhotoException`, `RequestRetryAllFailException`, `PartialDownloadFailedException`, `ResponseUnexpectedException`, `JsonResolveFailException`, `RegularNotMatchException`)

When upgrading jmcomic, diff `jm_exception.py` against the previous version — exception class additions/renames must be reflected in `src/jmcomic_api/errors.py`. Diff `JmAlbumDetail.__init__` and `JmSearchPage`/`JmCategoryPage` properties when route schemas (`schemas/catalog.py`) need updates.

## Overview

FastAPI service that wraps the `jmcomic` Python client. Downloads albums as `.webp` images, merges them into PDFs (optionally encrypted with the album id as password), and exposes endpoints for download/search/categories/details. Served by `uvicorn`, default `0.0.0.0:8699`. Auto-generated Swagger UI at `/docs`.

## Run / develop

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run uvicorn jmcomic_api.app:app --reload
# or: python -m jmcomic_api
```

Tests: `uv run pytest tests/` (use `-m "not e2e"` to skip filesystem-touching e2e tests). Lint: `ruff check . && ruff format --check .`. Type-check: `mypy src`.

Server-side config is read from environment variables (or `.env`) prefixed `JMAPI_` — see `.env.example`. Library-side config is `option.yml`. **`option.yml` is hot-reloadable via `SIGHUP`** (e.g. `docker kill -s HUP <container>`); the previous watchdog observer was replaced because editor atomic-writes double-fired the event.

## Architecture

```
src/jmcomic_api/
├── app.py              # FastAPI factory + lifespan (initialises jmcomic, wires SIGHUP)
├── settings.py         # pydantic-settings: host/port/pdf_dir/log_*
├── logging_config.py   # structlog setup (JSON in prod, console in dev)
├── deps.py             # FastAPI Depends providers
├── errors.py           # JmcomicException → HTTP responses
├── routers/            # pdf, catalog, health
├── schemas/            # pydantic models + StrEnum (Category, OrderBy, TimeRange)
├── services/
│   ├── jm_client.py    # ALL jmcomic calls go through here, async-wrapped via asyncio.to_thread
│   ├── album.py        # AlbumService.get_or_build (cache validate + serial-per-id download)
│   ├── pdf_paths.py    # JM-prefix normalization + filename derivation
│   └── pdf_builder.py  # streaming webp → pdf via Pillow + pypdf
└── concurrency/
    └── keyed_lock.py   # asyncio.Lock dict, ref-counted GC
```

Key invariants worth preserving:

- **Album id normalisation**: `services/pdf_paths.py::clean_album_id` strips a leading `JM` prefix. Both `12345` and `JM12345` must resolve to the same cache slot — folder name `[12345]title`, filename `[12345] title.pdf`. The pre-rewrite code had this bug (`is_jm_book_exist` stripped the prefix but the folder-build path didn't), silently breaking cache reuse.
- **Per-album serial generation**: `KeyedAsyncLock` keyed on `f"album:{clean_id}"`. Concurrent requests for the *same* album serialise (avoiding duplicate downloads / corrupt half-written PDFs); different albums run in parallel.
- **Album-folder naming hook**: `JmModuleConfig.AFIELD_ADVICE['jmbook']` is set once at startup in the lifespan (`install_album_dirname_advice`). Don't move it back to module-import time — that produces side-effects in `pytest` collection.
- **Sync jmcomic in async routes**: every jmcomic call in `services/jm_client.py` runs through `asyncio.to_thread`. **Never `await client.foo()` directly** — jmcomic is synchronous and would block the event loop.

## Caching and password semantics

PDF cache lives at `<JMAPI_PDF_DIR>/<filename>.pdf`. On hit, `services/pdf_builder.py::cache_decryptable` opens with `pypdf.PdfReader` and checks whether the encrypted state matches the request's `enable_pwd` / `passwd` query param:

- Encryption password is the **clean** album id (no `JM` prefix).
- If the cache's encrypted state doesn't match the request, the cached PDF is deleted and rebuilt — flipping `passwd=true` ↔ `passwd=false` on the same album forces a rebuild every time.
- Cache files written by the old PyPDF2 code may not decrypt under pypdf if the encryption algorithm differs; users should `rm -rf pdf/` once after upgrading.

## Endpoints (legacy paths preserved)

| Path | Notes |
|---|---|
| `GET /health`, `/health/live`, `/health/ready` | the latter checks runtime + pdf_dir |
| `GET /get_pdf/{album_id}` | default returns base64 JSON; `?pdf=true` streams `application/pdf`; `?passwd=false` disables encryption; `?Titletype=0\|1\|2` |
| `GET /get_pdf_path/{album_id}` | returns absolute server path |
| `GET /search?query=&page=` | `has_next_page` derived from `JmSearchPage.page_count` (no second upstream request) |
| `GET /album/{album_id}` | id, title, tags |
| `GET /categories?time=&category=&order_by=&page=` | enums validated by Pydantic |

## Config files

- `option.yml` — jmcomic library config (download dir, log, plugins, login). The `dir_rule.base_dir` here (`./webp`) is what `services/album.py` reads via `runtime.opt.dir_rule.base_dir` to find downloaded folders.
- `.env` (copy from `.env.example`) — server-side config. All vars prefixed `JMAPI_`.
- `pyproject.toml` — single source of truth for deps and tool config.

## Docker

`Dockerfile` is at the repo root and uses `COPY . /app` (multi-stage with `uv`). The previous `Docker/Dockerfile` cloned an unrelated upstream repo (`FfmpegZZZ/JMComic-Api`) and its image content never matched this codebase — that footgun is removed. `compose.yaml` mounts `./pdf`, `./webp`, and `./option.yml`.
