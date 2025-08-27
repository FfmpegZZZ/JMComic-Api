from __future__ import annotations
import gc
from pathlib import Path
from typing import Tuple
import os

from jmcomic import download_album
from PyPDF2 import PdfReader
from PyPDF2.errors import DependencyError, FileNotDecryptedError

from app.utils.pdf import merge_webp_to_pdf
from app.utils.file import is_jm_book_exist
from app.queue.manager import queue_manager


def get_album_pdf_path(jm_album_id: str, pdf_dir: str, opt, enable_pwd: bool = True, title_type: int = 2) -> Tuple[str, str]:
    """Return (pdf_path, pdf_filename) for an album, generating it if needed.

    title_type:
        0 => <id>.pdf
        1 => <title>.pdf
        2 (default) => [<id>] <title>.pdf
    """
    # 队列化：同一 jm_album_id 的生成串行，避免并发冲突
    key = f"album_{jm_album_id}"

    def ensure_title():
        t = is_jm_book_exist(opt.dir_rule.base_dir, jm_album_id)
        if t is None:
            album, _ = download_album(jm_album_id, option=opt)
            return f"{album.name}"
        print(f"本子已存在: {t}, 使用已缓存文件")
        return t

    title = queue_manager.submit(key, ensure_title).get()

    # 清洗文件名（Windows 不允许的字符）
    def sanitize(name: str) -> str:
        return ''.join(c for c in name if c not in '\\/:*?"<>|')

    safe_title = sanitize(title)
    if title_type == 0:
        pdf_filename = f"{jm_album_id}.pdf"
    elif title_type == 1:
        pdf_filename = f"{safe_title}.pdf"
    else:
        pdf_filename = f"[{jm_album_id}] {safe_title}.pdf"

    # 解析为绝对路径，避免当前工作目录变更导致的相对路径失效
    pdf_dir_path = Path(pdf_dir).resolve()
    pdf_dir_path.mkdir(parents=True, exist_ok=True)
    pdf_path_obj = pdf_dir_path / pdf_filename
    pdf_path = str(pdf_path_obj)

    use_cache = False
    def validate_or_build():
        nonlocal use_cache
        if pdf_path_obj.exists():
            try:
                reader = PdfReader(pdf_path)
                if enable_pwd:
                    if reader.is_encrypted:
                        try:
                            if reader.decrypt(jm_album_id):
                                print(f"缓存 PDF 已使用 '{jm_album_id}' 成功解密，使用缓存: {pdf_path}")
                                use_cache = True
                            else:
                                print(f"缓存 PDF 使用 '{jm_album_id}' 解密失败 (decrypt returned false)，重新生成: {pdf_path}")
                        except (FileNotDecryptedError, DependencyError, NotImplementedError) as decrypt_error:
                            print(f"缓存 PDF 使用 '{jm_album_id}' 解密失败 ({decrypt_error})，重新生成: {pdf_path}")
                    else:
                        print(f"缓存 PDF 未加密，但请求需要加密，重新生成: {pdf_path}")
                else:
                    if not reader.is_encrypted:
                        print(f"缓存 PDF 未加密，符合请求，使用缓存: {pdf_path}")
                        use_cache = True
                    else:
                        print(f"缓存 PDF 已加密，但请求不需要加密，重新生成: {pdf_path}")
            except Exception as e:
                print(f"检查缓存 PDF 时出错 ({e})，将重新生成: {pdf_path}")
                use_cache = False
            if not use_cache:
                try:
                    os.remove(pdf_path)
                except OSError as e:
                    print(f"删除旧缓存 PDF 时出错 ({e}): {pdf_path}")

        if not use_cache:
            # double-check 其他任务是否已经在我们等待期间生成
            if pdf_path_obj.exists():
                print(f"队列等待期间已生成 PDF，复用: {pdf_path}")
                use_cache = True
                return
            print(f"开始生成 PDF (加密={enable_pwd}): {pdf_path}")
            webp_folder = str(Path(opt.dir_rule.base_dir) / f"[{jm_album_id}]{title}")
            merge_webp_to_pdf(
                webp_folder,
                pdf_path=pdf_path,
                password=jm_album_id if enable_pwd else None,
            )
            gc.collect()

    # 串行执行生成/验证任务
    queue_manager.submit(key, validate_or_build).get()

    return pdf_path, pdf_filename
