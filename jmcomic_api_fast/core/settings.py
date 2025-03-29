from pydantic import BaseModel
from pathlib import Path
from jmcomic import create_option_by_file, JmApiClient, JmModuleConfig
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Pydantic Settings Model ---
class Settings(BaseModel):
    host: str = '0.0.0.0'
    port: int = 8699
    option_file: str = './option.yml' # Relative to project root
    pdf_dir: str = './pdf' # Relative to project root

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
        path.mkdir(parents=True, exist_ok=True) # Ensure directory exists
        return path

# --- Global Settings Instance ---
# Load settings (currently uses defaults defined above)
# In a real app, you might load from .env or a config file here
settings = Settings()

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
            _jm_option = create_option_by_file(str(option_path)) # Let it raise if file truly missing
        else:
            logger.info(f"Loading JmComic options from: {option_path}")
            _jm_option = create_option_by_file(str(option_path))
            # Apply custom naming rule from original main.py
            JmModuleConfig.AFIELD_ADVICE['jmbook'] = lambda album: f'[{album.id}]{album.title}'
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
    get_jm_client() # Re-initialize

# Example usage (optional, for testing within this module)
if __name__ == "__main__":
    print(f"Host: {settings.host}")
    print(f"Port: {settings.port}")
    print(f"Option File Path: {settings.resolved_option_file}")
    print(f"PDF Directory: {settings.resolved_pdf_dir}")
    try:
        client = get_jm_client()
        print(f"JmApiClient created: {client is not None}")
    except Exception as e:
        print(f"Error getting JmApiClient: {e}")
