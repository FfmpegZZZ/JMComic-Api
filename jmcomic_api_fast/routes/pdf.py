import base64
import logging

from fastapi import (
    APIRouter,
    Query,
    HTTPException,
    Path as FastApiPath,
    BackgroundTasks,
)
from fastapi.responses import JSONResponse, FileResponse

# Import necessary components from the new structure
# Assuming get_album_pdf_path will be adapted or correctly imported
from ..services.album_service import get_album_pdf_path_async
from ..core.settings import (
    settings,
    get_jm_option,
    get_jm_client,
)  # Import necessary settings and client getter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/pdf",  # Prefix for all routes in this router
    tags=["PDF"],  # Tag for API documentation
)


@router.get(
    "/path/{jm_album_id}",
    summary="Get the absolute path of the generated PDF for an album",
    response_description="JSON object containing the success status, message, absolute path, and filename",
)
async def get_pdf_file_path(
    jm_album_id: str = FastApiPath(
        ..., title="JMComic Album ID", description="The unique ID of the JMComic album"
    ),
    passwd: str = Query(
        "true",
        title="Password Protection Flag",
        description="Whether the PDF should be password protected ('true' or 'false')",
    ),
    Titletype: int = Query(
        1, title="Title Type", description="Type of title naming convention (integer)"
    ),  # Note: FastAPI converts query params to specified type
):
    """
    Retrieves the absolute local filesystem path for a generated PDF corresponding to a JMComic album ID.
    Downloads the album and generates the PDF if it doesn't exist.
    """
    enable_pwd = passwd.lower() not in ("false", "0")
    opt = get_jm_option()  # Get the currently loaded JmOption
    pdf_dir = settings.resolved_pdf_dir  # Get the resolved PDF directory path
    client = get_jm_client()  # Get client instance

    try:
        # Use the async version of get_album_pdf_path
        path_obj, name = await get_album_pdf_path_async(
            jm_album_id,
            pdf_dir,
            opt,
            client,
            enable_pwd=enable_pwd,
            Titletype=Titletype,
        )

        if path_obj is None or not path_obj.exists():
            logger.warning(
                f"PDF not found or generation failed for album {jm_album_id}"
            )
            raise HTTPException(
                status_code=404, detail="PDF file not found or could not be generated."
            )

        abspath = str(path_obj.resolve())  # Get absolute path as string
        logger.info(f"Successfully retrieved path for PDF: {name} ({abspath})")

        return JSONResponse(
            content={
                "success": True,
                "message": "PDF path retrieved successfully",
                "data": abspath,
                "name": name,
            }
        )
    except HTTPException as http_exc:
        raise http_exc  # Re-raise FastAPI specific exceptions
    except Exception as e:
        logger.exception(
            f"Error in get_pdf_file_path for {jm_album_id}: {e}"
        )  # Log full traceback
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected server error occurred while getting PDF path.",
        )


@router.get(
    "/{jm_album_id}",
    summary="Get the PDF file for an album, either as Base64 or direct download",
    response_description="Either a JSON object with Base64 encoded PDF data or a direct PDF file download",
)
async def get_pdf_file(
    background_tasks: BackgroundTasks,
    jm_album_id: str = FastApiPath(
        ..., title="JMComic Album ID", description="The unique ID of the JMComic album"
    ),
    passwd: str = Query(
        "true",
        title="Password Protection Flag",
        description="Whether the PDF should be password protected ('true' or 'false')",
    ),
    Titletype: int = Query(
        1, title="Title Type", description="Type of title naming convention (integer)"
    ),
    output_pdf_directly: bool = Query(
        False,
        alias="pdf",
        title="Direct PDF Output",
        description="If true, return the PDF file directly for download. If false, return JSON with Base64 data.",
    ),
):
    """
    Retrieves the PDF file for a JMComic album.
    - If `pdf=true`, returns the file directly for download.
    - If `pdf=false` (default), returns a JSON response containing the filename and Base64 encoded PDF data.
    Downloads the album and generates the PDF if it doesn't exist.
    """
    enable_pwd = passwd.lower() not in ("false", "0")
    opt = get_jm_option()
    pdf_dir = settings.resolved_pdf_dir
    client = get_jm_client()

    try:
        # Use the async version of get_album_pdf_path
        path_obj, name = await get_album_pdf_path_async(
            jm_album_id,
            pdf_dir,
            opt,
            client,
            enable_pwd=enable_pwd,
            Titletype=Titletype,
        )

        if path_obj is None or not path_obj.exists():
            logger.warning(
                f"PDF not found or generation failed for album {jm_album_id} before sending."
            )
            raise HTTPException(
                status_code=404, detail="PDF file not found or could not be generated."
            )

        if output_pdf_directly:
            logger.info(f"Serving PDF file directly: {name}")
            # Return the file directly
            return FileResponse(
                path=path_obj, filename=name, media_type="application/pdf"
            )
        else:
            # Return Base64 encoded data in JSON
            try:
                logger.info(f"Encoding PDF to Base64: {name}")

                # Read file in chunks to avoid loading large files entirely into memory
                async def read_file_in_chunks(file_path, chunk_size=65536):
                    with open(file_path, "rb") as f:
                        while chunk := f.read(chunk_size):
                            yield chunk

                # Use background task for large file encoding
                encoded_pdf = ""

                # For smaller files, we can do it directly
                if path_obj.stat().st_size < 10 * 1024 * 1024:  # Less than 10MB
                    with open(path_obj, "rb") as f:
                        encoded_pdf = base64.b64encode(f.read()).decode("utf-8")
                else:
                    # For larger files, we'll use a background task
                    # This is a simplified approach - for very large files, consider
                    # streaming the response or using a task queue system
                    chunks = []
                    async for chunk in read_file_in_chunks(path_obj):
                        chunks.append(chunk)
                    encoded_pdf = base64.b64encode(b"".join(chunks)).decode("utf-8")

                logger.info(f"Successfully encoded PDF: {name}")
                return JSONResponse(
                    content={
                        "success": True,
                        "message": "PDF retrieved successfully (Base64)",
                        "name": name,
                        "data": encoded_pdf,
                    }
                )
            except Exception as e:
                logger.exception(f"Error reading/encoding PDF for {jm_album_id}: {e}")
                raise HTTPException(
                    status_code=500, detail=f"Error reading or encoding PDF file."
                )

    except HTTPException as http_exc:
        # Re-raise HTTPExceptions directly
        raise http_exc
    except Exception as e:
        logger.exception(f"Error in get_pdf_file for {jm_album_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected server error occurred while getting PDF file.",
        )
