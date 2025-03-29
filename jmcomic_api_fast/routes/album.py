import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Path as FastApiPath
from pydantic import BaseModel, Field

from jmcomic import JmAlbumDetail, JmSearchPage, JmcomicException
from jmcomic.jm_exception import MissingAlbumPhotoException
from ..core.settings import get_jm_client # Import client getter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/album",
    tags=["Album"],
)

# --- Pydantic Models for Response ---
class AlbumData(BaseModel):
    id: str
    title: str
    tags: List[str] = Field(default_factory=list) # Default to empty list if tags are missing

class AlbumSuccessResponse(BaseModel):
    success: bool = True
    message: str = "Album details retrieved"
    data: AlbumData

class ErrorResponse(BaseModel):
    success: bool = False
    message: str

# --- Route Implementation ---
@router.get("/{jm_album_id}",
            response_model=AlbumSuccessResponse,
            summary="Get details for a specific album",
            response_description="JSON object containing album details (ID, title, tags)",
            responses={
                404: {"model": ErrorResponse, "description": "Album not found"},
                500: {"model": ErrorResponse, "description": "Server error retrieving details"}
            })
async def get_album_details(
    jm_album_id: str = FastApiPath(..., title="JMComic Album ID", description="The unique ID of the JMComic album")
):
    """
    Retrieves details for a specific JMComic album using its ID.
    Includes a fallback mechanism to search for the album ID if direct retrieval fails initially.
    """
    client = get_jm_client()
    logger.info(f"Attempting to get details for album ID: {jm_album_id}")

    try:
        album: Optional[JmAlbumDetail] = None
        try:
            album = client.get_album_detail(jm_album_id)
            logger.info(f"Successfully retrieved details for album {jm_album_id} directly.")
        except JmcomicException as e:
             # Specifically check for "not found" type errors before attempting search fallback
             if isinstance(e, MissingAlbumPhotoException) or "not found" in str(e).lower() or "不存在" in str(e):
                 logger.warning(f"Direct get_album_detail failed for {jm_album_id} (likely not found), attempting search fallback. Error: {e}")
                 album = None # Ensure album is None to trigger search logic
             else:
                 # Re-raise other JmcomicExceptions
                 raise e

        # Fallback logic if direct get_album_detail failed or returned None/empty
        if not album:
            logger.info(f"Direct retrieval failed or returned no data for {jm_album_id}, trying search_site as fallback.")
            try:
                page: JmSearchPage = client.search_site(search_query=jm_album_id)
                # Convert iterator to list to check results
                results_list = list(page)
                if not results_list:
                    logger.warning(f"Search fallback found no results for album ID '{jm_album_id}'.")
                    raise HTTPException(status_code=404, detail=f"Album with ID '{jm_album_id}' not found via direct access or search.")

                # Check if the first search result matches the requested ID
                first_result_id, _ = results_list[0]
                if str(first_result_id) == str(jm_album_id):
                    logger.info(f"Search fallback confirmed album ID '{jm_album_id}' exists. Retrying get_album_detail.")
                    # Retry getting details now that we know it likely exists
                    album = client.get_album_detail(jm_album_id)
                    if not album:
                        # This case is unlikely if search found it, but handle defensively
                        logger.error(f"Could not retrieve details for album ID '{jm_album_id}' even after search confirmation.")
                        raise HTTPException(status_code=500, detail=f"Failed to retrieve details for album ID '{jm_album_id}' after search confirmation.")
                else:
                    logger.warning(f"Search fallback found results, but none matched the exact ID '{jm_album_id}'. First result was '{first_result_id}'.")
                    raise HTTPException(status_code=404, detail=f"Search found results, but none matched the exact ID '{jm_album_id}'.")
            except JmcomicException as search_e:
                 logger.error(f"JmcomicException during search fallback for {jm_album_id}: {search_e}", exc_info=True)
                 raise HTTPException(status_code=500, detail=f"Error during search fallback for album ID '{jm_album_id}': {search_e}")
            except Exception as search_e:
                 logger.exception(f"Unexpected error during search fallback for {jm_album_id}: {search_e}", exc_info=True)
                 raise HTTPException(status_code=500, detail=f"Unexpected server error during search fallback for album ID '{jm_album_id}'.")

        # If we reach here, album should be populated
        if not album:
             # Defensive check if somehow album is still None
             logger.error(f"Album object is unexpectedly None for ID '{jm_album_id}' after all checks.")
             raise HTTPException(status_code=500, detail=f"Failed to obtain album details for ID '{jm_album_id}'.")

        # Construct the successful response
        album_data = AlbumData(
            id=str(album.id), # Ensure ID is string
            title=album.title,
            tags=album.tags if album.tags else [] # Handle potential None tags
        )
        logger.info(f"Successfully prepared response for album {jm_album_id}")
        return AlbumSuccessResponse(data=album_data)

    except JmcomicException as e:
        # Handle specific "not found" exceptions as 404, others as 500
        if isinstance(e, MissingAlbumPhotoException) or "not found" in str(e).lower() or "不存在" in str(e):
            logger.warning(f"Final check: Album with ID '{jm_album_id}' not found (JmcomicException: {e}).")
            raise HTTPException(status_code=404, detail=f"Album with ID '{jm_album_id}' not found.")
        else:
            logger.error(f"JmcomicException retrieving details for {jm_album_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Jmcomic error retrieving details: {e}")
    except HTTPException as http_exc:
        # Re-raise HTTPExceptions that might have been raised internally (e.g., from fallback)
        raise http_exc
    except Exception as e:
        logger.exception(f"Unexpected error in get_album_details for {jm_album_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected server error occurred.")
