"""``python -m jmcomic_api`` runs uvicorn against the in-process app."""

from __future__ import annotations

import uvicorn

from jmcomic_api.settings import Settings


def main() -> None:
    cfg = Settings()
    uvicorn.run(
        "jmcomic_api.app:app",
        host=cfg.host,
        port=cfg.port,
        log_config=None,  # we configure structlog ourselves in lifespan
    )


if __name__ == "__main__":
    main()
