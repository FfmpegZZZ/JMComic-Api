import logging
from typing import List
from itertools import tee
from enum import Enum

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from jmcomic import JmCategoryPage, JmcomicException
from jmcomic.jm_config import JmMagicConstants
from ..core.settings import get_jm_client # Import client getter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/categories",
    tags=["Categories"],
)

# --- Enums for Parameter Validation (Optional but recommended) ---
# Using Enums makes the API docs clearer and provides validation
class TimeOption(str, Enum):
    today = JmMagicConstants.TIME_TODAY
    week = JmMagicConstants.TIME_WEEK
    month = JmMagicConstants.TIME_MONTH
    all = JmMagicConstants.TIME_ALL
    t = JmMagicConstants.TIME_TODAY # Alias
    w = JmMagicConstants.TIME_WEEK   # Alias
    m = JmMagicConstants.TIME_MONTH  # Alias
    a = JmMagicConstants.TIME_ALL    # Alias

class CategoryOption(str, Enum):
    all = JmMagicConstants.CATEGORY_ALL
    doujin = JmMagicConstants.CATEGORY_DOUJIN
    single = JmMagicConstants.CATEGORY_SINGLE
    short = JmMagicConstants.CATEGORY_SHORT
    another = JmMagicConstants.CATEGORY_ANOTHER
    hanman = JmMagicConstants.CATEGORY_HANMAN
    meiman = JmMagicConstants.CATEGORY_MEIMAN
    doujin_cosplay = JmMagicConstants.CATEGORY_DOUJIN_COSPLAY
    cosplay = JmMagicConstants.CATEGORY_DOUJIN_COSPLAY # Alias
    three_d = JmMagicConstants.CATEGORY_3D # Alias '3d'
    english_site = JmMagicConstants.CATEGORY_ENGLISH_SITE

class OrderByOption(str, Enum):
    latest = JmMagicConstants.ORDER_BY_LATEST
    view = JmMagicConstants.ORDER_BY_VIEW
    picture = JmMagicConstants.ORDER_BY_PICTURE
    like = JmMagicConstants.ORDER_BY_LIKE
    month_rank = JmMagicConstants.ORDER_MONTH_RANKING
    week_rank = JmMagicConstants.ORDER_WEEK_RANKING
    day_rank = JmMagicConstants.ORDER_DAY_RANKING

# --- Pydantic Models for Response ---
class CategoryResultItem(BaseModel):
    id: str
    title: str

class ParamsUsed(BaseModel):
    time: str
    category: str
    order_by: str

class CategoryResponseData(BaseModel):
    results: List[CategoryResultItem]
    current_page: int
    has_next_page: bool
    params_used: ParamsUsed

class CategorySuccessResponse(BaseModel):
    success: bool = True
    message: str = "Categories retrieved successfully"
    data: CategoryResponseData

class ErrorResponse(BaseModel):
    success: bool = False
    message: str

# --- Route Implementation ---
@router.get("",
            response_model=CategorySuccessResponse,
            summary="Get comics based on category filters",
            response_description="JSON object containing filtered comic results and pagination info",
            responses={
                500: {"model": ErrorResponse, "description": "Server error during category filtering"}
            })
async def get_categories(
    page: int = Query(1, ge=1, title="Page Number", description="The page number of the results"),
    time: TimeOption = Query(TimeOption.all, title="Time Filter", description="Filter comics by time period"),
    category: CategoryOption = Query(CategoryOption.all, title="Category Filter", description="Filter comics by category"),
    order_by: OrderByOption = Query(OrderByOption.latest, alias="order_by", title="Ordering", description="Order results by specified criteria")
):
    """
    Retrieves a paginated list of comics based on selected time period, category, and ordering.
    """
    client = get_jm_client()

    # Parameters are already validated by FastAPI using the Enums
    time_param = time.value
    category_param = category.value
    order_by_param = order_by.value

    logger.info(f"Fetching categories: page={page}, time='{time_param}', category='{category_param}', order_by='{order_by_param}'")

    try:
        # Fetch the current page
        page_result: JmCategoryPage = client.categories_filter(
            page=page,
            time=time_param,
            category=category_param,
            order_by=order_by_param,
        )

        # Check for the next page
        has_next_page = False
        try:
            page_iter_copy, page_iter_check = tee(page_result)
            # Check if current page has items
            next(page_iter_check)
            # Try to fetch the next page
            next_page_check = client.categories_filter(
                page=page + 1,
                time=time_param,
                category=category_param,
                order_by=order_by_param,
            )
            next(iter(next_page_check))
            has_next_page = True
            page_result = page_iter_copy # Restore iterator
            logger.debug(f"Next category page check successful for page={page}")
        except StopIteration:
            has_next_page = False
            logger.debug(f"Next category page check indicates no more pages for page={page}")
        except JmcomicException as e:
            logger.warning(f"JmcomicException during next category page check for page={page + 1}: {e}")
            has_next_page = False
        except Exception as e:
            logger.error(f"Unexpected exception during next category page check for page={page + 1}: {e}", exc_info=True)
            has_next_page = False

        # Collect results
        results_list = [{"id": str(album_id), "title": title} for album_id, title in page_result]
        logger.info(f"Category fetch successful for page={page}. Found {len(results_list)} results. Has next page: {has_next_page}")

        # Construct response
        response_data = CategoryResponseData(
            results=results_list,
            current_page=page,
            has_next_page=has_next_page,
            params_used=ParamsUsed(
                time=time_param,
                category=category_param,
                order_by=order_by_param,
            )
        )
        return CategorySuccessResponse(data=response_data)

    except JmcomicException as e:
        logger.error(f"JmcomicException during category fetch for page={page}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Jmcomic categories error: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error during category fetch for page={page}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected server error occurred during category fetch.")
