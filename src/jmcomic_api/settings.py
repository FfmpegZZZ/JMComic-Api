"""Server-side configuration. Reads from env or .env, validated by pydantic.

The jmcomic library's own configuration still lives in ``option.yml`` (path
configurable via ``JMAPI_OPTION_FILE``).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JMAPI_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8699

    option_file: Path = Path("./option.yml")
    pdf_dir: Path = Path("./pdf")
    pdf_shard_cache_dir: Path = Path("./pdf_cache")
    pdf_shard_size: int = Field(
        default=100, ge=1, description="Pages per PDF shard. Cache key includes this value."
    )

    log_format: str = Field(
        default="console", description="'json' for production, 'console' for dev"
    )
    log_level: str = "INFO"

    cors_origins: list[str] = Field(default_factory=list)
    slow_request_threshold_seconds: float = 5.0

    # ---- reliability knobs ------------------------------------------------
    # Total operation budgets (seconds). Reverse proxies should be configured
    # with `proxy_read_timeout` >= download_timeout_seconds.
    download_timeout_seconds: float = Field(default=600, ge=1)
    pdf_build_timeout_seconds: float = Field(default=300, ge=1)
    metadata_timeout_seconds: float = Field(default=30, ge=1)

    # Async retry budget for transient download failures.
    download_retry_attempts: int = Field(default=3, ge=1)
    download_retry_initial_wait: float = Field(default=2, ge=0)
    download_retry_max_wait: float = Field(default=30, ge=0)

    # Global concurrency cap on simultaneous album downloads (jmcomic mobile
    # API is sensitive to bursts — 3 is a sane default).
    max_concurrent_downloads: int = Field(default=3, ge=1)
    max_concurrent_metadata: int = Field(default=8, ge=1)
    max_concurrent_pdf_builds: int = Field(default=1, ge=1)
    max_concurrent_base64: int = Field(default=1, ge=1)
    max_concurrent_base64_streams: int = Field(default=4, ge=1)
    response_send_timeout_seconds: float = Field(default=30, gt=0)
    chapter_threads: int = Field(default=3, ge=1)
    image_threads: int = Field(default=12, ge=1)
    queue_capacity: int = Field(default=32, ge=0)
    queue_timeout_seconds: float = Field(default=30, gt=0)
    page_cache_mb: int = Field(default=512, ge=0)
    jpeg_optimize: bool = True
    query_cache_ttl_seconds: float = Field(default=0, ge=0)
    detail_cache_ttl_seconds: float = Field(default=0, ge=0)
    auth_enabled: bool = False
    auth_state_dir: Path = Path("./state/sessions")
    auth_key_file: Path | None = None
    auth_session_ttl_seconds: int = Field(default=604800, ge=60)
    auth_session_idle_seconds: int = Field(default=86400, ge=60)
    auth_max_sessions: int = Field(default=256, ge=1)
    auth_max_runtimes: int = Field(default=32, ge=1)
    auth_login_timeout_seconds: float = Field(default=30, ge=1)
    pdf_cache_ttl_seconds: float = Field(default=0, ge=0)
    pdf_cache_max_mb: int = Field(default=0, ge=0)
    cleanup_interval_seconds: float = Field(default=3600, gt=0)

    # Max time to wait for a per-album lock before giving up with 503.
    lock_acquire_timeout_seconds: float = Field(default=600, ge=1)

    # /health/ready disk-space gate. Below this, the readiness probe goes 503
    # so a load balancer sheds traffic before downloads start failing.
    min_free_disk_mb: int = Field(default=500, ge=0)

    # Graceful shutdown: lifespan cancels in-flight tasks then waits up to N
    # seconds before yielding control to uvicorn for the final teardown.
    shutdown_grace_seconds: float = Field(default=30, ge=0)

    @property
    def option_path(self) -> Path:
        return self.option_file.resolve()

    @property
    def pdf_path(self) -> Path:
        return self.pdf_dir.resolve()

    @property
    def shard_cache_path(self) -> Path:
        return self.pdf_shard_cache_dir.resolve()


def get_settings() -> Settings:
    """FastAPI Depends entry. Cached at process scope by lru_cache in deps.py."""
    return Settings()
