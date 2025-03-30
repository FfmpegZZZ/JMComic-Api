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
    get_process_pool,
    get_thread_pool,
    get_download_semaphore,
    get_pdf_semaphore,
)

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
    client: JmApiClient,
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
            raise FileNotFoundError(
                f"缓存的专辑文件夹 ID {jm_album_id} 在 {base_path} 未找到"
            )
        logger.info(f"找到缓存的WebP文件夹: {webp_folder_path}")

    # 确保标题不为None（如果找到缓存版本）
    if title is None:
        try:
            first_bracket_index = webp_folder_path.name.find("]")
            if first_bracket_index != -1:
                title = webp_folder_path.name[first_bracket_index + 1 :].strip()
                logger.info(f"从缓存的文件夹名称提取标题: {title}")
            else:
                title = f"Unknown Title {jm_album_id}"
        except Exception:
            title = f"Unknown Title {jm_album_id}"

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

    # 5. 返回路径对象和文件名
    return pdf_path_obj, pdf_filename


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
