import os
import subprocess
from pathlib import Path
import time
import shutil
import img2pdf
import asyncio
import functools
import logging

logger = logging.getLogger(__name__)


async def merge_webp_to_pdf_async(
    folder_path, pdf_path, password=None, process_pool=None
):
    """
    异步将指定文件夹下的所有 .jpeg 文件按照文件名顺序合并为 PDF 长图，并可选使用 qpdf 命令行工具加密。
    利用进程池进行CPU密集型操作。

    :param folder_path: 包含 .jpeg 文件的文件夹路径
    :param pdf_path: 输出 PDF 文件的路径
    :param password: PDF 文件的密码（可选）
    :param process_pool: 进程池，用于CPU密集型任务
    """
    output_dir = Path(pdf_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 使用进程池处理CPU密集型的PDF生成任务
    try:
        merge_start = time.time()

        # 获取并排序所有.jpeg文件
        webp_files = sorted(Path(folder_path).glob("*.jpeg"))  # 按文件名排序
        if not webp_files:
            raise RuntimeError("指定文件夹中没有找到 .jpeg 文件")

        webp_files_str = [str(file) for file in webp_files]

        # 使用进程池执行PDF生成
        if process_pool:
            logger.info(f"使用进程池生成PDF: {pdf_path}")
            loop = asyncio.get_event_loop()
            pdf_data = await loop.run_in_executor(
                process_pool, functools.partial(_generate_pdf_data, webp_files_str)
            )
        else:
            logger.info(f"在主进程中生成PDF: {pdf_path}")
            pdf_data = _generate_pdf_data(webp_files_str)

        # 写入PDF文件
        with open(pdf_path, "wb") as f:
            f.write(pdf_data)

        merge_end = time.time()
        merge_duration = merge_end - merge_start
        logger.info(f"PDF 文件已生成并保存：{pdf_path}")
        logger.info(f"合并耗时：{merge_duration:.2f} 秒")
    except Exception as e:
        logger.error(f"PDF生成失败: {e}", exc_info=True)
        raise RuntimeError(f"img2pdf 合并失败: {e}")

    # 如果需要密码，则调用加密函数
    if password:
        await encrypt_pdf_async(pdf_path, password, process_pool)


def _generate_pdf_data(webp_files):
    """
    在单独的进程中生成PDF数据。

    :param webp_files: 要合并的webp文件路径列表
    :return: 生成的PDF数据
    """
    try:
        return img2pdf.convert(webp_files)
    except Exception as e:
        logging.error(f"在进程中生成PDF数据失败: {e}")
        raise


async def encrypt_pdf_async(pdf_path, password, process_pool=None):
    """
    异步使用 qpdf 对 PDF 文件进行加密。

    :param pdf_path: 要加密的 PDF 文件路径
    :param password: PDF 文件的密码
    :param process_pool: 进程池，用于CPU密集型任务
    """
    encryption_start = time.time()
    logger.info(f"PDF 文件将加密保存：{pdf_path}")
    pdf_temp_path = str(pdf_path) + ".temp"
    shutil.move(pdf_path, pdf_temp_path)  # 临时重命名文件

    try:
        # 使用进程池或直接在事件循环中运行加密
        if process_pool:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                process_pool,
                functools.partial(
                    _encrypt_pdf_process, pdf_temp_path, str(pdf_path), password
                ),
            )
        else:
            # 在事件循环中运行子进程
            proc = await asyncio.create_subprocess_exec(
                "qpdf",
                "--encrypt",
                password,
                password,
                "256",
                "--print=full",
                "--extract=y",
                "--",
                pdf_temp_path,
                str(pdf_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                raise RuntimeError(f"PDF 加密失败: {error_msg}")
    except Exception as e:
        logger.error(f"PDF加密失败: {e}", exc_info=True)
        # 尝试恢复原文件
        if os.path.exists(pdf_temp_path):
            try:
                shutil.move(pdf_temp_path, pdf_path)
                logger.info(f"已恢复原PDF文件: {pdf_path}")
            except Exception as restore_e:
                logger.error(f"恢复原PDF文件失败: {restore_e}")
        raise RuntimeError(f"PDF 加密失败: {e}")
    finally:
        # 清理临时文件
        if os.path.exists(pdf_temp_path):
            try:
                os.remove(pdf_temp_path)
            except OSError as e:
                logger.warning(f"删除临时文件失败 {pdf_temp_path}: {e}")

    encryption_end = time.time()
    encryption_duration = encryption_end - encryption_start
    logger.info(f"PDF 文件已加密并保存：{pdf_path}")
    logger.info(f"加密耗时：{encryption_duration:.2f} 秒")


def _encrypt_pdf_process(input_path, output_path, password):
    """
    在单独的进程中执行PDF加密。

    :param input_path: 输入PDF路径
    :param output_path: 输出PDF路径
    :param password: 加密密码
    """
    try:
        subprocess.run(
            [
                "qpdf",
                "--encrypt",
                password,
                password,
                "256",
                "--print=full",
                "--extract=y",
                "--",
                input_path,
                output_path,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        error_output = e.stderr.decode() if e.stderr else "Unknown error"
        raise RuntimeError(f"PDF 加密失败: {error_output}")


# 保留原始同步函数以兼容旧代码
def merge_webp_to_pdf(folder_path, pdf_path, password=None):
    """
    将指定文件夹下的所有 .jpeg 文件按照文件名顺序合并为 PDF 长图，并可选使用 qpdf 命令行工具加密。
    此函数是同步版本，新代码应使用异步版本。

    :param folder_path: 包含 .jpeg 文件的文件夹路径
    :param pdf_path: 输出 PDF 文件的路径
    :param password: PDF 文件的密码（可选）
    """
    # 创建事件循环并运行异步函数
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            merge_webp_to_pdf_async(folder_path, pdf_path, password)
        )
    finally:
        loop.close()


def encrypt_pdf(pdf_path, password):
    """
    使用 qpdf 对 PDF 文件进行加密。
    此函数是同步版本，新代码应使用异步版本。

    :param pdf_path: 要加密的 PDF 文件路径
    :param password: PDF 文件的密码
    """
    # 创建事件循环并运行异步函数
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(encrypt_pdf_async(pdf_path, password))
    finally:
        loop.close()
