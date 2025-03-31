import gc
import logging
import os
import asyncio
import functools
from pathlib import Path
from typing import Tuple, List, Optional

# 导入 jmcomic 相关
from jmcomic import download_album, JmApiClient, JmOption, JmAlbumDetail

# 导入项目内部工具函数
from ..utils.pdf import merge_webp_to_pdf_async
from ..utils.file import IsJmBookExist
from ..core.settings import (
    get_process_pool, # Keep for potential future use or if pdf utils still use it
    get_thread_pool,
    get_download_semaphore,
    # get_pdf_semaphore, # No longer generating full PDFs here directly
)
# from ..utils.file import ensure_path_suffix # Removed unused import

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


async def download_album_async(
    jm_album_id: str, option: JmOption
) -> Tuple[JmAlbumDetail, Path]:
    """
    异步下载漫画专辑。

    :param jm_album_id: 漫画ID
    :param option: JmOption配置
    :return: 专辑详情和下载路径
    """
    # 获取下载信号量，限制并发下载数量
    semaphore = get_download_semaphore()
    thread_pool = get_thread_pool()

    async with semaphore:
        logger.info(f"开始异步下载专辑 {jm_album_id}")
        loop = asyncio.get_event_loop()

        # 在线程池中执行I/O密集型的下载操作
        try:
            album, download_path = await loop.run_in_executor(
                thread_pool,
                functools.partial(download_album, jm_album_id, option=option),
            )

            # 等待文件系统同步
            logger.info("等待1.5秒以确保文件系统同步...")
            await asyncio.sleep(1.5)

            return album, download_path
        except Exception as e:
            logger.exception(f"下载专辑 {jm_album_id} 失败: {e}")
            raise


async def get_album_pdf_path_async(
    jm_album_id: str,
    pdf_dir: Path,
    opt: JmOption,
    enable_pwd: bool = True,
    Titletype: int = 2,
) -> Tuple[Path, str]:
    """
    异步获取PDF路径，如有必要则下载专辑并生成PDF。

    :param jm_album_id: 漫画ID
    :param pdf_dir: PDF保存目录
    :param opt: JmOption配置
    :param client: JmApiClient客户端
    :param enable_pwd: 是否启用密码保护
    :param Titletype: 标题类型
    :return: PDF路径对象和文件名
    """
    logger.info(
        f"异步请求PDF路径: 专辑 {jm_album_id}, enable_pwd={enable_pwd}, Titletype={Titletype}"
    )

    webp_folder_path: Optional[Path] = None
    title: Optional[str] = None
    base_path = Path(opt.dir_rule.base_dir)
    folder_prefix = f"[{jm_album_id}]"

    # 1. 检查专辑是否已下载，如果没有则下载
    title = IsJmBookExist(base_path, jm_album_id)
    if title is None:
        logger.info(f"专辑 {jm_album_id} 未在本地找到，开始下载。")
        try:
            album, _ = await download_album_async(jm_album_id, opt)
            title = album.title
            logger.info(f"专辑 {jm_album_id} 下载成功。标题: {title}")

            # 查找下载路径
            logger.info(
                f"在 {base_path} 中手动搜索前的内容: {list_directory_contents(base_path)}"
            )
            webp_folder_path = find_folder_by_prefix(base_path, folder_prefix)

            if webp_folder_path is None:
                logger.error(
                    f"手动搜索未能找到以 '{folder_prefix}' 开头的文件夹，在 {base_path} 下载后。"
                )
                raise FileNotFoundError(
                    f"下载的专辑文件夹 ID {jm_album_id} 在 {base_path} 下载后未找到"
                )
            logger.info(f"通过手动搜索找到下载的文件夹: {webp_folder_path}")

        except Exception as e:
            logger.exception(f"下载或查找专辑 {jm_album_id} 的路径时失败: {e}")
            raise
    else:
        logger.info(f"专辑 {jm_album_id} 在本地找到: {title}，使用现有文件。")
        # 即使使用缓存也需要找到路径
        logger.info(
            f"手动搜索前 {base_path} 的内容 (缓存): {list_directory_contents(base_path)}"
        )

        webp_folder_path = find_folder_by_prefix(base_path, folder_prefix)

        if webp_folder_path is None:
            logger.error(
                f"无法找到缓存的专辑文件夹，以 '{folder_prefix}' 开头，在 {base_path}"
            )
            # Consider if we should attempt download here if ensure_downloaded is True?
            # For now, assume if IsJmBookExist found it, the folder should exist.
            raise FileNotFoundError(
                f"缓存的专辑文件夹 ID {jm_album_id} 在 {base_path} 未找到，尽管 IsJmBookExist 报告存在。"
            )
        logger.info(f"找到缓存的WebP文件夹: {webp_folder_path}")

    # 确保标题不为None（如果找到缓存版本）
    if title is None and webp_folder_path: # Check webp_folder_path exists
        try:
            first_bracket_index = webp_folder_path.name.find("]")
            if first_bracket_index != -1:
                title = webp_folder_path.name[first_bracket_index + 1 :].strip()
                logger.info(f"从缓存的文件夹名称提取标题: {title}")
            else:
                title = f"Unknown Title {jm_album_id}" # Fallback title
        except Exception as e:
            logger.warning(f"从文件夹名称提取标题失败: {e}")
            title = f"Unknown Title {jm_album_id}" # Fallback title
    elif title is None:
         # This case should ideally not happen if folder finding logic is correct
         logger.warning(f"无法确定专辑 {jm_album_id} 的标题。")
         title = f"Unknown Title {jm_album_id}" # Fallback title


    # --- Start: Logic specific to get_album_pdf_path_async ---
    # This part generates the full PDF path and potentially the PDF itself.
    # We keep it for now but won't use it for sharding.

    # 2. 根据Titletype确定PDF文件名
    if Titletype == 0:
        pdf_filename = f"{jm_album_id}.pdf"
    elif Titletype == 1:
        sanitized_title = "".join(
            c for c in title if c.isalnum() or c in (" ", "_", "-")
        ).rstrip()
        pdf_filename = f"{sanitized_title}.pdf"
    else:  # 默认为TitleType 2或任何其他值
        pdf_filename = f"[{jm_album_id}] {title}.pdf"
        pdf_filename = "".join(
            c for c in pdf_filename if c.isalnum() or c in (" ", "_", "-", "[", "]")
        ).rstrip()

    pdf_path_obj = pdf_dir / pdf_filename
    logger.debug(f"目标PDF路径: {pdf_path_obj}")

    # 3. 检查缓存并决定是否需要重新生成
    regenerate = False
    if pdf_path_obj.exists():
        logger.info(f"找到现有的PDF缓存文件: {pdf_path_obj}")
        use_cache = True
    else:
        logger.info(f"未找到PDF的缓存: {pdf_path_obj}。将生成。")
        use_cache = False

    if not use_cache:
        regenerate = True

    # 4. 如果需要，重新生成PDF
    if regenerate:
        if pdf_path_obj.exists():
            try:
                os.remove(pdf_path_obj)
                logger.info(f"在重新生成前移除现有的缓存文件: {pdf_path_obj}")
            except OSError as e:
                logger.warning(f"无法移除现有的缓存文件 {pdf_path_obj}: {e}")

        logger.info(f"开始PDF生成 (enable_pwd={enable_pwd}): {pdf_path_obj}")

        # 使用确定的webp_folder_path
        if not webp_folder_path or not webp_folder_path.is_dir():
            logger.error(f"合并前WebP文件夹路径无效或不是目录: {webp_folder_path}")
            raise FileNotFoundError(f"无效的WebP源文件夹路径: {webp_folder_path}")

        logger.info(f"使用WebP文件夹进行合并: {webp_folder_path}")

        # 获取PDF生成信号量，限制并发PDF生成数量
        pdf_semaphore = get_pdf_semaphore()
        process_pool = get_process_pool()

        try:
            # 使用进程池和信号量异步生成PDF
            async with pdf_semaphore:
                await merge_webp_to_pdf_async(
                    folder_path=str(webp_folder_path),
                    pdf_path=str(pdf_path_obj),
                    password=jm_album_id if enable_pwd else None,
                    process_pool=process_pool,
                )
                logger.info(f"成功生成PDF: {pdf_path_obj}")
        except Exception as e:
            logger.exception(
                f"从文件夹 {webp_folder_path} 为专辑 {jm_album_id} 生成PDF失败: {e}"
            )
            if pdf_path_obj.exists():
                try:
                    os.remove(pdf_path_obj)
                    logger.info(f"生成错误后移除可能不完整的PDF文件: {pdf_path_obj}")
                except OSError as rm_e:
                    logger.warning(f"无法移除不完整的PDF文件 {pdf_path_obj}: {rm_e}")
            raise
        finally:
            gc.collect()
            logger.debug("PDF生成尝试后触发垃圾回收。")
    # --- End: Logic specific to get_album_pdf_path_async ---

    # 5. 返回路径对象和文件名
    return pdf_path_obj, pdf_filename


# --- New Functions for Sharding ---

async def get_album_image_info_async(
    jm_album_id: str,
    opt: JmOption,
    ensure_downloaded: bool = True,
) -> Tuple[int, Path, str]:
    """
    异步获取相册图片信息（总数、文件夹路径、标题）。

    :param jm_album_id: 漫画ID
    :param opt: JmOption配置
    :param ensure_downloaded: 如果为True，则在本地未找到时下载相册
    :return: 元组 (总图片数, 图片文件夹路径, 相册标题)
    :raises FileNotFoundError: 如果相册未找到且ensure_downloaded为False，或下载失败/文件夹未找到
    """
    logger.info(f"异步请求图片信息: 专辑 {jm_album_id}, ensure_downloaded={ensure_downloaded}")

    image_folder_path: Optional[Path] = None
    title: Optional[str] = None
    base_path = Path(opt.dir_rule.base_dir)
    folder_prefix = f"[{jm_album_id}]"

    # 1. 检查本地是否存在，如果需要则下载
    title = IsJmBookExist(base_path, jm_album_id)
    if title is None:
        if not ensure_downloaded:
            logger.warning(f"专辑 {jm_album_id} 未在本地找到，且未要求下载。")
            raise FileNotFoundError(f"专辑 {jm_album_id} 未在本地找到。")

        logger.info(f"专辑 {jm_album_id} 未在本地找到，开始下载。")
        try:
            album, _ = await download_album_async(jm_album_id, opt)
            title = album.title
            logger.info(f"专辑 {jm_album_id} 下载成功。标题: {title}")

            # 查找下载路径
            image_folder_path = find_folder_by_prefix(base_path, folder_prefix)
            if image_folder_path is None:
                logger.error(f"下载后未能找到文件夹 '{folder_prefix}' 在 {base_path}")
                raise FileNotFoundError(f"下载的专辑文件夹 {jm_album_id} 在 {base_path} 未找到")
            logger.info(f"找到下载的文件夹: {image_folder_path}")

        except Exception as e:
            logger.exception(f"下载或查找专辑 {jm_album_id} 失败: {e}")
            raise
    else:
        logger.info(f"专辑 {jm_album_id} 在本地找到: {title}，使用现有文件。")
        image_folder_path = find_folder_by_prefix(base_path, folder_prefix)
        if image_folder_path is None:
            logger.error(f"无法找到缓存的专辑文件夹 '{folder_prefix}' 在 {base_path}")
            # This might indicate an inconsistency if IsJmBookExist returned a title
            raise FileNotFoundError(f"缓存的专辑文件夹 {jm_album_id} 在 {base_path} 未找到")
        logger.info(f"找到缓存的图片文件夹: {image_folder_path}")

    # 确保标题有效
    if title is None and image_folder_path:
        try:
            first_bracket_index = image_folder_path.name.find("]")
            if first_bracket_index != -1:
                title = image_folder_path.name[first_bracket_index + 1 :].strip()
            else: title = f"Unknown Title {jm_album_id}"
        except Exception: title = f"Unknown Title {jm_album_id}"
    elif title is None:
        title = f"Unknown Title {jm_album_id}"


    # 2. 计算图片总数
    if not image_folder_path or not image_folder_path.is_dir():
        logger.error(f"图片文件夹路径无效或不是目录: {image_folder_path}")
        raise FileNotFoundError(f"无效的图片源文件夹路径: {image_folder_path}")

    try:
        # 使用 glob 高效查找所有 .jpeg 文件
        image_files = list(image_folder_path.glob("*.jpeg"))
        total_images = len(image_files)
        logger.info(f"在 {image_folder_path} 中找到 {total_images} 个 .jpeg 文件")
        if total_images == 0:
             logger.warning(f"警告：在文件夹 {image_folder_path} 中没有找到 .jpeg 文件。")

    except Exception as e:
        logger.exception(f"列出或计数图片文件时出错 {image_folder_path}: {e}")
        raise RuntimeError(f"无法计数图片文件在 {image_folder_path}")

    return total_images, image_folder_path, title


async def get_album_image_paths_in_range_async(
    image_folder_path: Path,
    total_images: int, # Pass total images to avoid recounting
    start_page: int,    # 1-based index
    end_page: int,      # 1-based index
) -> List[Path]:
    """
    异步获取指定页面范围内的图片文件路径列表。
    假设图片已下载且按顺序命名（例如 001.jpeg, 002.jpeg ...）。

    :param image_folder_path: 包含图片的文件夹路径。
    :param total_images: 该文件夹中的图片总数。
    :param start_page: 开始页码（包含，基于1）。
    :param end_page: 结束页码（包含，基于1）。
    :return: 范围内图片路径的 Path 对象列表。
    :raises ValueError: 如果页面范围无效。
    :raises FileNotFoundError: 如果预期的图片文件丢失。
    """
    logger.info(f"请求图片路径范围: {start_page}-{end_page} 在 {image_folder_path}")

    if not image_folder_path.is_dir():
         raise FileNotFoundError(f"图片文件夹不存在: {image_folder_path}")

    # 验证页面范围
    start_page = max(1, start_page)
    end_page = min(total_images, end_page)

    if start_page > end_page:
        logger.warning(f"无效的页面范围请求: start={start_page}, end={end_page}, total={total_images}")
        return [] # 返回空列表表示范围内没有有效页面

    image_paths = []
    missing_files = []

    # 确定文件名的填充宽度 (例如，001 vs 0001)
    # 尝试查找第一个和最后一个预期的文件来猜测填充
    first_expected_name = f"{1:03d}.jpeg" # Assume 3 digits first
    last_expected_name = f"{total_images:03d}.jpeg"
    padding = 3
    if not (image_folder_path / first_expected_name).exists() and not (image_folder_path / last_expected_name).exists():
         # Try 4 digits if 3 digits don't seem right
         first_expected_name_4 = f"{1:04d}.jpeg"
         last_expected_name_4 = f"{total_images:04d}.jpeg"
         if (image_folder_path / first_expected_name_4).exists() or (image_folder_path / last_expected_name_4).exists():
              padding = 4
              logger.debug("检测到图片文件名使用4位数字填充。")
         else:
              # Fallback or further detection needed? For now, stick to 3 or log warning.
              logger.warning(f"无法确定 {image_folder_path} 中图片文件名的填充宽度，假设为3。")


    for i in range(start_page, end_page + 1):
        # 假设文件名是数字填充的，例如 001.jpeg, 002.jpeg ...
        filename = f"{i:0{padding}d}.jpeg" # Use detected padding
        file_path = image_folder_path / filename
        if file_path.exists():
            image_paths.append(file_path)
        else:
            logger.warning(f"预期的图片文件未找到: {file_path}")
            missing_files.append(filename)

    # Decide how to handle missing files. For now, log and return what was found.
    # Optionally, raise an error if any file in the requested range is missing.
    if missing_files:
         logger.error(f"在请求的范围 {start_page}-{end_page} 内缺少以下文件: {missing_files}")
         # raise FileNotFoundError(f"缺少范围内的图片文件: {missing_files}") # Uncomment to be strict

    logger.info(f"找到 {len(image_paths)} 个图片路径，范围 {start_page}-{end_page}")
    return image_paths


# 保留原始同步函数以兼容旧代码
def get_album_pdf_path(
    jm_album_id: str,
    pdf_dir: Path,
    opt: JmOption,
    client: JmApiClient,
    enable_pwd: bool = True,
    Titletype: int = 2,
) -> Tuple[Path, str]:
    """
    获取PDF路径，如有必要则下载专辑并生成PDF。
    这是同步版本，新代码应使用异步版本。

    :param jm_album_id: 漫画ID
    :param pdf_dir: PDF保存目录
    :param opt: JmOption配置
    :param client: JmApiClient客户端
    :param enable_pwd: 是否启用密码保护
    :param Titletype: 标题类型
    :return: PDF路径对象和文件名
    """
    # 创建事件循环并运行异步函数
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            get_album_pdf_path_async(
                jm_album_id, pdf_dir, opt, client, enable_pwd, Titletype
            )
        )
    finally:
        loop.close()
