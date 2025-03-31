from pydantic import BaseModel
from pathlib import Path
from jmcomic import create_option_by_file, JmApiClient, JmModuleConfig
import logging
import multiprocessing
import asyncio
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- Pydantic Settings Model ---
class Settings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8699
    option_file: str = "./option.yml"  # Relative to project root
    pdf_dir: str = "./pdf"  # Relative to project root for *original* full PDFs (if kept)
    pdf_shard_cache_dir: str = "./pdf_cache" # Relative to project root for cached shards
    default_pdf_shard_size: int = 50 # Default number of pages per shard

    # Concurrency settings
    max_workers: int = max(
        4, multiprocessing.cpu_count()
    )  # Default to CPU count for process pool
    thread_workers: int = max(
        8, multiprocessing.cpu_count() * 2
    )  # Default to 2x CPU count for thread pool
    max_concurrent_downloads: int = 5  # Maximum concurrent album downloads
    max_concurrent_pdf_generations: int = 3  # Maximum concurrent PDF generations

    # Request timeouts (in seconds)
    download_timeout: int = 300  # Timeout for album downloads
    pdf_generation_timeout: int = 600  # Timeout for PDF generation

    # Ensure paths are resolved correctly relative to the project root
    @property
    def resolved_option_file(self) -> Path:
        # Assuming the project root is the parent of the 'jmcomic_api_fast' directory
        project_root = Path(__file__).parent.parent.parent
        return project_root / self.option_file

    @property
    def resolved_pdf_dir(self) -> Path:
        project_root = Path(__file__).parent.parent.parent
        path = project_root / self.pdf_dir
        path.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
        return path

    @property
    def resolved_pdf_shard_cache_dir(self) -> Path:
        project_root = Path(__file__).parent.parent.parent
        path = project_root / self.pdf_shard_cache_dir
        path.mkdir(parents=True, exist_ok=True) # Ensure cache directory exists
        return path

# --- Global Settings Instance ---
# Load settings (currently uses defaults defined above)
# In a real app, you might load from .env or a config file here
settings = Settings()

# --- Worker Pools ---
# These will be initialized when the application starts
_thread_pool = None
_process_pool = None
_download_semaphore = None
_pdf_semaphore = None


def get_thread_pool() -> ThreadPoolExecutor:
    """Get the thread pool executor for I/O bound tasks."""
    global _thread_pool
    if _thread_pool is None:
        _thread_pool = ThreadPoolExecutor(max_workers=settings.thread_workers)
    return _thread_pool


def get_process_pool() -> ProcessPoolExecutor:
    """Get the process pool executor for CPU bound tasks."""
    global _process_pool
    if _process_pool is None:
        _process_pool = ProcessPoolExecutor(max_workers=settings.max_workers)
    return _process_pool


def get_download_semaphore() -> asyncio.Semaphore:
    """Get the semaphore for limiting concurrent downloads."""
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
    return _download_semaphore


def get_pdf_semaphore() -> asyncio.Semaphore:
    """Get the semaphore for limiting concurrent PDF generations."""
    global _pdf_semaphore
    if _pdf_semaphore is None:
        _pdf_semaphore = asyncio.Semaphore(settings.max_concurrent_pdf_generations)
    return _pdf_semaphore


# --- JmComic Client Initialization ---
_jm_option = None
_jm_client = None


def get_jm_option():
    """Gets the JmOption instance, loading from file if necessary."""
    global _jm_option
    if _jm_option is None:
        option_path = settings.resolved_option_file
        if not option_path.exists():
            logger.error(f"JmComic option file not found at: {option_path}")
            # Handle error appropriately - maybe raise an exception or return None
            # For now, let create_option_by_file handle the potential error
            _jm_option = create_option_by_file(
                str(option_path)
            )  # Let it raise if file truly missing
        else:
            logger.info(f"Loading JmComic options from: {option_path}")
            _jm_option = create_option_by_file(str(option_path))
            # Apply custom naming rule from original main.py
            JmModuleConfig.AFIELD_ADVICE["jmbook"] = (
                lambda album: f"[{album.id}]{album.title}"
            )
    return _jm_option


def get_jm_client() -> JmApiClient:
    """Gets the JmApiClient instance, creating it if necessary."""
    global _jm_client
    if _jm_client is None:
        option = get_jm_option()
        if option:
            logger.info("Creating new JmApiClient instance.")
            _jm_client = option.new_jm_client()
        else:
            # This case might occur if get_jm_option failed to load the file
            logger.error("Cannot create JmApiClient: JmOption failed to load.")
            # Decide how to handle this - raise error? Return None?
            # Raising an error might be better to prevent app startup with invalid state.
            raise RuntimeError("Failed to initialize JmOption from file.")
    return _jm_client


def reload_jm_client():
    """Forces reloading of the JmOption and JmApiClient."""
    global _jm_option, _jm_client
    logger.info("Reloading JmComic client and options.")
    _jm_option = None
    _jm_client = None
    get_jm_client()  # Re-initialize


# Shutdown function to clean up resources
def shutdown_resources():
    """Shutdown and clean up all resources when application is stopping."""
    global _thread_pool, _process_pool
    if _thread_pool:
        logger.info("Shutting down thread pool...")
        _thread_pool.shutdown(wait=True)
        _thread_pool = None

    if _process_pool:
        logger.info("Shutting down process pool...")
        _process_pool.shutdown(wait=True)
        _process_pool = None

    logger.info("All resource pools have been shut down.")


# Example usage (optional, for testing within this module)
if __name__ == "__main__":
    print(f"Host: {settings.host}")
    print(f"Port: {settings.port}")
    print(f"Option File Path: {settings.resolved_option_file}")
    print(f"PDF Directory: {settings.resolved_pdf_dir}")
    print(f"Process Workers: {settings.max_workers}")
    print(f"Thread Workers: {settings.thread_workers}")
    try:
        client = get_jm_client()
        print(f"JmApiClient created: {client is not None}")
    except Exception as e:
        print(f"Error getting JmApiClient: {e}")
