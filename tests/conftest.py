from __future__ import annotations

# Defensive: macOS uv venvs sometimes re-set the UF_HIDDEN flag on site-packages,
# causing site.py to skip the .pth file that wires src/ onto sys.path.
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# --- jmcomic page-like fakes -------------------------------------------------


class FakePage:
    """Mimics JmSearchPage / JmCategoryPage: iterable of (id, title)."""

    def __init__(self, items: list[tuple[str, str]], total: int | None = None) -> None:
        self._items = list(items)
        self.total = total if total is not None else len(items)
        self.page_size = 80

    def __iter__(self):
        return iter(self._items)

    @property
    def page_count(self) -> int:
        import math

        return max(1, math.ceil(self.total / self.page_size))


@pytest.fixture
def make_page():
    return FakePage


# --- FastAPI test fixtures --------------------------------------------------


@pytest.fixture
def mock_runtime():
    runtime = MagicMock()
    runtime.opt.dir_rule.base_dir = "./webp"
    return runtime


@pytest.fixture
def app_with_mocks(mock_runtime, tmp_path, monkeypatch) -> Iterator:
    """Build a FastAPI app whose lifespan stub-injects a mock runtime so we never
    need a real jmcomic client.
    """
    monkeypatch.setenv("JMAPI_PDF_DIR", str(tmp_path / "pdf"))
    monkeypatch.setenv("JMAPI_OPTION_FILE", str(tmp_path / "option.yml"))
    (tmp_path / "option.yml").write_text("dir_rule:\n  base_dir: ./webp\n")
    (tmp_path / "pdf").mkdir(exist_ok=True)

    from jmcomic_api.deps import settings as settings_factory

    settings_factory.cache_clear()  # respect the new env vars

    from jmcomic_api.app import create_app

    app = create_app()

    # Replace the heavy parts before lifespan runs.
    from jmcomic_api.services import jm_client as jm_mod
    from jmcomic_api.services.album import AlbumService

    monkeypatch.setattr(jm_mod, "load_runtime", lambda _path: mock_runtime)
    monkeypatch.setattr(jm_mod, "install_album_dirname_advice", lambda: None)

    yield app, mock_runtime, AlbumService


@pytest.fixture
def client(app_with_mocks):
    app, runtime, _svc = app_with_mocks
    with TestClient(app) as c:
        # Replace the album_service installed by lifespan with a MagicMock so
        # tests can stub get_or_build per-test.
        c.app.state.album_service = MagicMock()
        yield c, runtime
