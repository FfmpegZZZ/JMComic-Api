import logging
import math
import os
import asyncio
import functools
import base64 # <-- Import base64
from pathlib import Path

from fastapi import (
    APIRouter,
    Query,
    HTTPException,
    Path as FastApiPath,
    BackgroundTasks,
)
from fastapi.responses import JSONResponse, FileResponse

# Import project components
from ..services.album_service import (
    get_album_image_info_async, # 保留给 /shard 使用
    get_album_image_paths_in_range_async, # 保留给 /shard 使用
    get_album_metadata_async, # 导入新函数
)
# 导入 jmcomic 异常基类，用于更精确的捕获
from jmcomic import JmcomicException
from ..utils.pdf import _generate_pdf_data # Import the core PDF generation logic
from ..core.settings import (
    settings,
    get_jm_option,
    get_process_pool, # Needed for running img2pdf in a separate process
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/pdf",
    tags=["PDF Sharding"], # Updated tag
)

# Helper function to remove file in background
async def _cleanup_temp_file(file_path: Path):
    try:
        if file_path.exists():
            os.remove(file_path)
            logger.info(f"临时分片文件已清理: {file_path}")
    except OSError as e:
        logger.error(f"清理临时分片文件失败 {file_path}: {e}")


@router.get(
    "/info/{jm_album_id}",
    summary="Get PDF sharding info for an album",
    response_description="JSON object with total pages, shard size, and shard list",
)
async def get_pdf_info(
    jm_album_id: str = FastApiPath(
        ..., title="JMComic Album ID", description="The unique ID of the JMComic album"
    ),
    # shard_size parameter removed, fixed to 100
):
    """
    Retrieves information needed for PDF sharding (fixed at 100 pages per shard),
    including total pages and calculated shard ranges.
    Downloads album images if not found locally.
    """
    opt = get_jm_option()
    shard_size = 100 # Hardcoded shard size
    try:
        # Get total pages and album title using metadata function (no download)
        try:
            total_pages, title = await get_album_metadata_async(jm_album_id, opt)
        except JmcomicException as jm_e: # 捕获 jmcomic 库特定的异常
            # 检查是否是资源未找到的特定子类异常 (如果 jmcomic 库提供了的话)
            # 这里假设 JmcomicException 基类包含了网络错误和找不到资源的情况
            logger.warning(f"请求元数据时 jmcomic 库出错 (专辑: {jm_album_id}): {jm_e}")
            # 可以根据 jm_e 的具体类型或消息判断是否是 404
            # 为了简单起见，暂时都归为 500，或者可以尝试更智能的判断
            if "not found" in str(jm_e).lower() or "missing" in str(jm_e).lower(): # 简单的字符串匹配判断
                 raise HTTPException(status_code=404, detail=f"Album {jm_album_id} not found online or access error.")
            else:
                 # 502 Bad Gateway 表示上游服务（JMComic API）出错
                 raise HTTPException(status_code=502, detail=f"Error communicating with upstream JMComic service: {jm_e}")
        except Exception as e: # 捕获其他可能的错误 (如 asyncio 问题或创建客户端失败)
            logger.exception(f"获取专辑 {jm_album_id} 的元数据时发生意外错误: {e}")
            raise HTTPException(
                status_code=500, detail="Internal server error retrieving album metadata."
            )

        if total_pages == 0:
             logger.warning(f"专辑 {jm_album_id} 没有找到图片，无法提供分片信息。")
             # Return info indicating zero pages, client should handle this
             return JSONResponse(
                content={
                    "success": True,
                    "message": "Album found, but contains no images.",
                    "data": {
                        "jm_album_id": jm_album_id,
                        "title": title,
                        "total_pages": 0,
                        "shard_size": 100, # Hardcoded
                        "shards": [],
                    },
                }
            )

        # Calculate shard information (using fixed size 100)
        num_shards = math.ceil(total_pages / 100)
        shards = []
        for i in range(num_shards):
            start_page = i * 100 + 1
            end_page = min((i + 1) * 100, total_pages)
            shards.append(
                {"shard_index": i + 1, "start_page": start_page, "end_page": end_page}
            )

        logger.info(f"为专辑 {jm_album_id} ({title}) 生成分片信息: {total_pages} 页, {num_shards} 个分片 (大小 100)")

        return JSONResponse(
            content={
                "success": True,
                "message": "PDF shard info retrieved successfully",
                "data": {
                    "jm_album_id": jm_album_id,
                    "title": title,
                    "total_pages": total_pages,
                    "shard_size": 100, # Hardcoded
                    "shards": shards,
                },
            }
        )
    except HTTPException as http_exc:
        # Re-raise HTTPExceptions raised from metadata fetching
        raise http_exc
    except Exception as e:
        # Catch any other unexpected errors during shard calculation or processing
        logger.exception(f"处理专辑 {jm_album_id} 的 PDF 信息时发生意外错误: {e}")
        raise HTTPException(
            status_code=500, detail="Server error processing PDF shard info."
        )


@router.get(
    "/shard/{jm_album_id}/{shard_index}",
    summary="Get a specific PDF shard for an album as Base64", # Updated summary
    response_description="JSON object containing the Base64 encoded PDF shard", # Updated description
)
async def get_pdf_shard(
    jm_album_id: str = FastApiPath(
        ..., title="JMComic Album ID", description="The unique ID of the JMComic album"
    ),
    shard_index: int = FastApiPath(
        ..., gt=0, title="Shard Index", description="The 1-based index of the shard to retrieve"
    ),
    # shard_size parameter removed, fixed to 100
):
    """
    Retrieves a specific shard (a PDF file containing a range of pages, fixed at 100 pages per shard)
    for the given album ID. Uses cached shard if available, otherwise generates it on demand.
    """
    opt = get_jm_option()
    shard_size = 100 # Hardcoded shard size
    cache_dir = settings.resolved_pdf_shard_cache_dir / jm_album_id
    cache_dir.mkdir(parents=True, exist_ok=True) # Ensure album-specific cache dir exists
    # Simplified cache filename (shard size is fixed)
    cache_filename = f"shard_{shard_index}.pdf"
    cache_path = cache_dir / cache_filename

    # 1. Check cache first
    if cache_path.exists():
        logger.info(f"缓存命中: 读取缓存的分片 {cache_path}")
        try:
            # Need title even for cache hit
            _, _, title = await get_album_image_info_async(
                jm_album_id, opt, ensure_downloaded=False # Don't force download if only getting title
            )
            with open(cache_path, "rb") as f:
                pdf_content = f.read()
            pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')
            logger.info(f"成功读取并编码缓存的分片 {shard_index} (专辑 {jm_album_id})")
            # Return JSON with base64 data
            return JSONResponse(
                content={
                    "title": title, # Changed key from name to title
                    "success": True,
                    "message": "PDF shard found in cache and encoded.",
                    "shard_index": shard_index, # Add shard index
                    "data": pdf_base64, # Base64 data
                }
            )
        except FileNotFoundError:
             logger.warning(f"缓存命中但获取标题时未找到专辑 {jm_album_id}，继续生成流程。")
             # Proceed to cache miss logic if title fetch fails unexpectedly
        except Exception as e:
            logger.exception(f"读取或编码缓存文件 {cache_path} 时出错: {e}")
            raise HTTPException(status_code=500, detail="Error processing cached PDF shard.")


    # 2. Cache miss: Get info and generate shard
    logger.info(f"缓存未命中: 专辑 {jm_album_id}, 分片 {shard_index} (大小 {shard_size})。正在生成...")
    try:
        total_pages, image_folder_path, title = await get_album_image_info_async(
            jm_album_id, opt, ensure_downloaded=True # Ensure images are there
        )

        # Validate shard_index (using fixed size 100)
        num_shards = math.ceil(total_pages / shard_size) # Use variable for calculation
        if not (1 <= shard_index <= num_shards):
            logger.warning(f"无效的分片索引请求: {shard_index} (总共 {num_shards} 个分片, 大小 {shard_size})")
            raise HTTPException(
                status_code=404,
                detail=f"Invalid shard index {shard_index}. Valid range is 1 to {num_shards} for shard size {shard_size}.",
            )

        # Calculate page range for this shard (using fixed size 100)
        start_page = (shard_index - 1) * shard_size + 1 # Use variable for calculation
        end_page = min(shard_index * shard_size, total_pages) # Use variable for calculation

        # Get image paths for the range
        image_paths = await get_album_image_paths_in_range_async(
            image_folder_path, total_pages, start_page, end_page
        )

        if not image_paths:
            logger.error(f"在范围 {start_page}-{end_page} 内没有找到图片文件，无法生成分片 {shard_index}")
            raise HTTPException(
                status_code=404,
                detail=f"No images found for page range {start_page}-{end_page} in shard {shard_index}.",
            )

        # Generate PDF data using the utility function in a process pool
        process_pool = get_process_pool()
        loop = asyncio.get_event_loop()
        image_paths_str = [str(p) for p in image_paths] # Convert Path objects to strings

        logger.info(f"使用 {len(image_paths_str)} 张图片生成分片 {shard_index}...")
        pdf_data = await loop.run_in_executor(
             process_pool,
             functools.partial(_generate_pdf_data, image_paths_str)
        )

        # Save generated data to cache
        try:
            with open(cache_path, "wb") as f:
                f.write(pdf_data)
            logger.info(f"分片已生成并缓存: {cache_path}")
        except IOError as e:
            logger.exception(f"无法写入缓存文件 {cache_path}: {e}")
            # If saving fails, maybe return data directly? Or raise 500?
            # For now, raise 500 as the cache save failed.
            raise HTTPException(status_code=500, detail="Failed to save generated PDF shard.")

        # Encode the generated data to Base64
        pdf_base64 = base64.b64encode(pdf_data).decode('utf-8')
        logger.info(f"成功生成并编码分片 {shard_index} (专辑 {jm_album_id})")

        # Return JSON with base64 data
        return JSONResponse(
            content={
                "title": title, # Changed key from name to title
                "success": True,
                "message": "PDF shard generated and encoded successfully.",
                "shard_index": shard_index, # Add shard index
                "data": pdf_base64, # Base64 data
            }
        )

    except FileNotFoundError as e:
        logger.warning(f"请求分片时未找到专辑 {jm_album_id} 或其图片: {e}")
        raise HTTPException(status_code=404, detail=f"Album {jm_album_id} or required images not found.")
    except HTTPException as http_exc:
         raise http_exc # Re-raise specific HTTP exceptions
    except Exception as e:
        logger.exception(f"生成或提供分片 {shard_index} (专辑 {jm_album_id}) 时出错: {e}")
        # Clean up potentially incomplete cache file if generation failed before saving
        if not cache_path.exists() and 'pdf_data' in locals(): # Check if generation succeeded but saving failed
             pass # Already handled above
        elif cache_path.exists(): # If file exists but some other error occurred after saving attempt
             # Maybe remove it? Or leave it? Let's leave it for now.
             pass
        raise HTTPException(
            status_code=500, detail="Server error generating or serving PDF shard."
        )
