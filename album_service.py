import gc
from pathlib import Path
import os

from jmcomic import download_album
from PyPDF2 import PdfReader
from PyPDF2.errors import DependencyError, FileNotDecryptedError 

# 调用utils中封装的工具函数
from utils.pdf import merge_webp_to_pdf
from utils.file import IsJmBookExist

def get_album_pdf_path(jm_album_id, pdf_dir, opt, enable_pwd=True, Titletype=2):
    title = IsJmBookExist(opt.dir_rule.base_dir, jm_album_id)
    if title is None:   #本子不存在
        album, _ = download_album(jm_album_id, option=opt)
        title = f"{album.name}"
    else:
        print(f"本子已存在: {title}, 使用已缓存文件")

    if Titletype == 0:
        pdf_filename = f"{jm_album_id}.pdf"
    elif Titletype == 1:
        pdf_filename = f"{title}.pdf"
    else: # Default to TitleType 2 or any other value
        pdf_filename = f"[{jm_album_id}] {title}.pdf"

    pdf_path_obj = Path(pdf_dir) / pdf_filename
    pdf_path = str(pdf_path_obj) 

    use_cache = False
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
        print(f"开始生成 PDF (加密={enable_pwd}): {pdf_path}")

        base_path = Path(opt.dir_rule.base_dir)
        found_folders = list(base_path.glob(f"[{jm_album_id}]*"))

        if not found_folders:
            print(f"错误：在 {base_path} 中找不到 ID 为 {jm_album_id} 的本子文件夹。可能下载未完成或文件系统延迟。")
            raise FileNotFoundError(f"在 {base_path} 中找不到 ID 为 {jm_album_id} 的本子文件夹")
        elif len(found_folders) > 1:
            print(f"警告：找到多个匹配 ID {jm_album_id} 的文件夹，将使用第一个：{found_folders[0]}")

        webp_floder = str(found_folders[0])
        print(f"找到 WebP 文件夹：{webp_floder}") 

        merge_webp_to_pdf(
            webp_floder,
            pdf_path=pdf_path,
            password=jm_album_id if enable_pwd else None
        )
        gc.collect() 

    return pdf_path, pdf_filename
