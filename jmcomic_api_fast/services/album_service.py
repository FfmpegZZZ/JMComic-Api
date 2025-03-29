import gc
import logging
import os
import time
from pathlib import Path
from typing import Tuple, List

# 导入 jmcomic 相关
from jmcomic import download_album, JmApiClient, JmOption, JmAlbumDetail

# 导入项目内部工具函数
from ..utils.pdf import merge_webp_to_pdf
from ..utils.file import IsJmBookExist

logger = logging.getLogger(__name__)

def list_directory_contents(dir_path: Path) -> List[str]:
    """Helper function to list directory contents for debugging."""
    try:
        if dir_path.exists() and dir_path.is_dir():
            return [item.name for item in dir_path.iterdir()]
        elif not dir_path.exists():
            return ["Directory does not exist"]
        else:
            return ["Path exists but is not a directory"]
    except Exception as e:
        return [f"Error listing directory: {e}"]

def find_folder_by_prefix(base_path: Path, prefix: str) -> Path | None:
    """Manually find the first directory starting with the given prefix."""
    if not base_path.is_dir():
        return None
    for item in base_path.iterdir():
        if item.is_dir() and item.name.startswith(prefix):
            return item
    return None


def get_album_pdf_path(
    jm_album_id: str,
    pdf_dir: Path, # Expecting Path object now
    opt: JmOption,
    client: JmApiClient, # Added client parameter for consistency
    enable_pwd: bool = True,
    Titletype: int = 2
) -> Tuple[Path, str]:
    """
    Gets the path to the PDF file for a given album ID.
    Downloads the album and generates the PDF if necessary.
    """
    logger.info(f"Requesting PDF path for album {jm_album_id}, enable_pwd={enable_pwd}, Titletype={Titletype}")

    webp_folder_path: Path = None
    title: str = None
    base_path = Path(opt.dir_rule.base_dir) # Define base_path early
    folder_prefix = f"[{jm_album_id}]" # Define the prefix to search for

    # 1. Check if album is downloaded, download if not
    title = IsJmBookExist(base_path, jm_album_id) # IsJmBookExist might need similar update if it uses glob
    if title is None:
        logger.info(f"Album {jm_album_id} not found locally, starting download.")
        try:
            album: JmAlbumDetail
            album, _ = download_album(jm_album_id, option=opt)
            title = album.title
            logger.info(f"Album {jm_album_id} downloaded successfully. Title: {title}")

            # --- Add delay and debug logging before finding path ---
            logger.info("Waiting for 1.5 second for filesystem sync after download...")
            time.sleep(1.5) # Increased sleep time for better sync
            logger.info(f"Contents of {base_path} before manual search: {list_directory_contents(base_path)}")

            # --- Find path manually ---
            webp_folder_path = find_folder_by_prefix(base_path, folder_prefix)

            if webp_folder_path is None:
                 logger.error(f"Manual search failed to find folder starting with '{folder_prefix}' in {base_path} after download.")
                 raise FileNotFoundError(f"Downloaded album folder for ID {jm_album_id} not found in {base_path} after download")
            logger.info(f"Found downloaded folder via manual search: {webp_folder_path}")

        except Exception as e:
            logger.exception(f"Failed during download or finding path for album {jm_album_id}: {e}", exc_info=True)
            raise
    else:
        logger.info(f"Album {jm_album_id} found locally: {title}, using existing files.")
        # --- Need to find the path even if using cache ---
        logger.info(f"Contents of {base_path} before manual search (cached): {list_directory_contents(base_path)}")

        webp_folder_path = find_folder_by_prefix(base_path, folder_prefix)

        if webp_folder_path is None:
            logger.error(f"Could not find cached album folder starting with '{folder_prefix}' in {base_path}")
            raise FileNotFoundError(f"Cached album folder for ID {jm_album_id} not found in {base_path}")
        logger.info(f"Found cached WebP folder: {webp_folder_path}")

    # Ensure title is not None if we found a cached version
    if title is None:
        try:
            first_bracket_index = webp_folder_path.name.find(']')
            if first_bracket_index != -1:
                title = webp_folder_path.name[first_bracket_index + 1:].strip()
                logger.info(f"Extracted title from cached folder name: {title}")
            else:
                title = f"Unknown Title {jm_album_id}"
        except Exception:
             title = f"Unknown Title {jm_album_id}"

    # 2. Determine PDF filename based on Titletype
    if Titletype == 0:
        pdf_filename = f"{jm_album_id}.pdf"
    elif Titletype == 1:
        sanitized_title = "".join(c for c in title if c.isalnum() or c in (' ', '_', '-')).rstrip()
        pdf_filename = f"{sanitized_title}.pdf"
    else: # Default to TitleType 2 or any other value
        pdf_filename = f"[{jm_album_id}] {title}.pdf"
        pdf_filename = "".join(c for c in pdf_filename if c.isalnum() or c in (' ', '_', '-', '[', ']')).rstrip()

    pdf_path_obj = pdf_dir / pdf_filename
    logger.debug(f"Target PDF path: {pdf_path_obj}")

    # 3. Check cache and decide if regeneration is needed
    regenerate = False
    if pdf_path_obj.exists():
        logger.info(f"Found existing PDF cache file: {pdf_path_obj}")
        use_cache = True
    else:
        logger.info(f"No cache found for PDF: {pdf_path_obj}. Will generate.")
        use_cache = False

    if not use_cache:
        regenerate = True

    # 4. Regenerate PDF if needed
    if regenerate:
        if pdf_path_obj.exists():
            try:
                os.remove(pdf_path_obj)
                logger.info(f"Removed existing cache file before regeneration: {pdf_path_obj}")
            except OSError as e:
                logger.warning(f"Could not remove existing cache file {pdf_path_obj}: {e}")

        logger.info(f"Starting PDF generation (enable_pwd={enable_pwd}): {pdf_path_obj}")

        # --- Use the determined webp_folder_path ---
        if not webp_folder_path or not webp_folder_path.is_dir():
             logger.error(f"WebP folder path is invalid or not a directory before merging: {webp_folder_path}")
             raise FileNotFoundError(f"Invalid WebP source folder path: {webp_folder_path}")

        logger.info(f"Using WebP folder for merge: {webp_folder_path}")

        try:
            merge_webp_to_pdf(
                folder_path=str(webp_folder_path), # Changed argument name
                pdf_path=str(pdf_path_obj),
                password=jm_album_id if enable_pwd else None
            )
            logger.info(f"Successfully generated PDF: {pdf_path_obj}")
        except Exception as e:
            logger.exception(f"Failed to generate PDF for album {jm_album_id} from folder {webp_folder_path}: {e}", exc_info=True)
            if pdf_path_obj.exists():
                try:
                    os.remove(pdf_path_obj)
                    logger.info(f"Removed potentially incomplete PDF file after generation error: {pdf_path_obj}")
                except OSError as rm_e:
                    logger.warning(f"Could not remove incomplete PDF file {pdf_path_obj}: {rm_e}")
            raise
        finally:
            gc.collect()
            logger.debug("Garbage collection triggered after PDF generation attempt.")

    # 5. Return the path object and filename
    return pdf_path_obj, pdf_filename
