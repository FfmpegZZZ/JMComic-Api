from fastapi import FastAPI
import logging
import uvicorn
from contextlib import asynccontextmanager

# Configure logging (can be centralized later)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import settings and resource management functions
from .settings import (
    settings,
    get_jm_client,
    get_thread_pool,
    get_process_pool,
    get_download_semaphore,
    get_pdf_semaphore,
    shutdown_resources,
)


# Lifespan context manager to handle startup and shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize resources on startup
    logger.info("Initializing application resources...")

    # Pre-initialize all resource pools
    thread_pool = get_thread_pool()
    process_pool = get_process_pool()
    download_semaphore = get_download_semaphore()
    pdf_semaphore = get_pdf_semaphore()

    # Pre-initialize JmComic client
    client = get_jm_client()

    logger.info(
        f"Resource pools initialized: {settings.thread_workers} thread workers, {settings.max_workers} process workers"
    )
    logger.info(
        f"Concurrency limits: {settings.max_concurrent_downloads} downloads, {settings.max_concurrent_pdf_generations} PDF generations"
    )

    yield  # Application runs here

    # Clean up resources on shutdown
    logger.info("Shutting down application resources...")
    shutdown_resources()
    logger.info("Application shutdown complete")


# Create FastAPI app instance with lifespan manager
app = FastAPI(
    title="JMComic API (FastAPI)",
    description="An API for interacting with JMComic, migrated from Flask.",
    version="1.0.0",  # Example version
    lifespan=lifespan,
)


# --- Basic Root Endpoint ---
@app.get("/", tags=["General"])
async def read_root():
    """
    Root endpoint providing a welcome message.
    """
    return {"message": "Welcome to JMComic API (FastAPI version)"}


# --- Placeholder for Docs Redirect (if needed differently than default /docs) ---
# The original Flask app had a /docs redirect. FastAPI provides /docs and /redoc automatically.
# If the specific redirect to apifox is still desired at /docs, we might need to override.
# For now, let's rely on FastAPI's default /docs. If the apifox redirect is crucial,
# we can add it back later, potentially disabling FastAPI's default docs or using a different path.

# --- Include Routers ---
from ..routes import (
    pdf,
    search,
    album,
    category,
)  # Import routers from the routes package

app.include_router(pdf.router)
app.include_router(search.router)
app.include_router(album.router)
app.include_router(category.router)

logger.info("FastAPI application instance created and routers included.")

# Example of how to run this app directly (for development)
# In production, use uvicorn command: uvicorn jmcomic_api_fast.core.main:app --host 0.0.0.0 --port 8699
if __name__ == "__main__":
    from .settings import settings  # Import settings from the same directory

    uvicorn.run(app, host=settings.host, port=settings.port)
