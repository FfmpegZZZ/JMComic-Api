import logging
from typing import List, Optional
from itertools import tee

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from jmcomic import JmSearchPage, JmcomicException
from ..core.settings import get_jm_client # Import client getter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/search",
    tags=["Search"],
)

# --- Pydantic Models for Response ---
class SearchResultItem(BaseModel):
    id: str
    title: str

class SearchResponseData(BaseModel):
    results: List[SearchResultItem]
    current_page: int
    has_next_page: bool

class SearchSuccessResponse(BaseModel):
    success: bool = True
    message: str = "Search successful"
    data: SearchResponseData

class ErrorResponse(BaseModel):
    success: bool = False
    message: str

# --- Route Implementation ---
@router.get("",
            response_model=SearchSuccessResponse,
            summary="Search for comics by query",
            response_description="JSON object containing search results, pagination info",
            responses={
                400: {"model": ErrorResponse, "description": "Missing query parameter"},
                500: {"model": ErrorResponse, "description": "Server error during search"}
            })
async def search_comics(
    query: Optional[str] = Query(None, title="Search Query", description="The search term for comics"),
    page: int = Query(1, alias="page", ge=1, title="Page Number", description="The page number of the search results")
):
    """
    Searches the JMComic site for comics matching the provided query.
    Returns paginated results.
    """
    if not query:
        logger.warning("Search request received without query parameter.")
        # Return JSONResponse directly for specific error codes/models if needed,
        # or raise HTTPException which FastAPI handles.
        raise HTTPException(status_code=400, detail="Missing 'query' parameter")

    client = get_jm_client()
    logger.info(f"Performing search for query='{query}', page={page}")

    try:
        # Perform the search for the current page
        page_result: JmSearchPage = client.search_site(search_query=query, page=page)

        # Check for the next page to determine has_next_page
        # This logic remains similar to the original Flask app
        has_next_page = False
        try:
            # Use tee to avoid consuming the main iterator
            page_iter_copy, page_iter_check = tee(page_result)
            # Try to get the first item from the check iterator to confirm current page has results
            first_item = next(page_iter_check)
            # Now, try to fetch the next page
            next_page_check = client.search_site(search_query=query, page=page + 1)
            # Try to get the first item from the next page iterator
            next(iter(next_page_check))
            has_next_page = True
            # Restore the original iterator if tee was used successfully
            page_result = page_iter_copy
            logger.debug(f"Next page check successful for query='{query}', page={page}")
        except StopIteration:
            # This means either the current page or the next page is empty
            has_next_page = False
            logger.debug(f"Next page check indicates no more pages for query='{query}', page={page}")
        except JmcomicException as e:
            # Jmcomic might raise an exception if the next page doesn't exist or other issues
            logger.warning(f"JmcomicException during next page check for query='{query}', page={page + 1}: {e}")
            has_next_page = False
        except Exception as e:
            # Catch any other unexpected errors during the check
            logger.error(f"Unexpected exception during next page check for query='{query}', page={page + 1}: {e}", exc_info=True)
            has_next_page = False # Assume no next page on error

        # Collect results from the (potentially restored) iterator
        results_list = [{"id": str(album_id), "title": title} for album_id, title in page_result]
        logger.info(f"Search successful for query='{query}', page={page}. Found {len(results_list)} results. Has next page: {has_next_page}")

        # Construct the successful response using Pydantic models
        response_data = SearchResponseData(
            results=results_list,
            current_page=page,
            has_next_page=has_next_page
        )
        return SearchSuccessResponse(data=response_data)

    except JmcomicException as e:
        logger.error(f"JmcomicException during search for query='{query}', page={page}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Jmcomic search error: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error during search for query='{query}', page={page}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected server error occurred during search.")
