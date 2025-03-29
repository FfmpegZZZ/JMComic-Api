import io
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image


# 将指定文件夹下的所有 .webp 文件按照文件名顺序合并为 PDF 长图，并可选加密。
# @param folder_path: 包含 .webp 文件的文件夹路径
# @param pdf_path: 输出文件夹
# @param is_pwd: 是否加密 PDF 文件

# @TR0MXI
def merge_webp_to_pdf(folder_path, pdf_path, password=None):
    """
    将指定文件夹下的所有 .webp 文件按照文件名顺序合并为 PDF 长图，并可选加密。

    :param folder_path: 包含 .webp 文件的文件夹路径
    :param pdf_path: 输出文件夹
    :param is_pwd: 输出 PDF 文件的路径
    :param password: PDF 文件的密码（可选）
    """
    output_dir = Path(pdf_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use rglob to search recursively for .webp files in subdirectories
    webp_files = sorted(Path(folder_path).rglob("*.webp"))

    if not webp_files:
        raise FileNotFoundError(f"文件夹 {folder_path} 中没有找到 .webp 文件")

    images = []
    for webp in webp_files:
        try:
            images.append(Image.open(webp).convert("RGB"))
        except Exception as e:
            print(f"警告：无法打开或转换图片 {webp}: {e}")

    if not images: # Check if images list is empty after potential errors
        print(f"警告：无法从 {folder_path} 加载任何有效的 .webp 图片。")
        # Optionally raise an error instead of just returning
        raise FileNotFoundError(f"文件夹 {folder_path} 中没有加载到有效的 .webp 图片")

    doc = fitz.open() # Create a new empty PDF
    try: # Use try...finally to ensure resources are cleaned up
        for i, image in enumerate(images):
            try:
                width, height = image.size
                rect = fitz.Rect(0, 0, width, height)
                page = doc.new_page(width=width, height=height)

                # Convert PIL image to bytes in memory (using PNG for broad compatibility)
                img_byte_arr = io.BytesIO()
                image.save(img_byte_arr, format='PNG')
                img_byte_arr = img_byte_arr.getvalue()

                page.insert_image(rect, stream=img_byte_arr)
            except Exception as e:
                print(f"警告：处理第 {i+1} 张图片时出错: {e}")
            finally:
                image.close() # Close image after processing or if error occurs

        print(f"准备保存 PDF 文件：{pdf_path}")

        # Define save options for PyMuPDF
        save_opts = {
            "garbage": 4,      # Remove unused objects (compaction)
            "deflate": True,   # Compress streams (compression)
            "clean": True,     # Clean/sanitize content streams
        }

        if password:
            save_opts["encryption"] = fitz.PDF_ENCRYPT_AES_256 # Use strong AES-256 encryption
            # Define permissions (e.g., allow printing and copying)
            save_opts["permissions"] = int(fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
            save_opts["owner_pw"] = password # Set owner password
            save_opts["user_pw"] = password # Set user password (same as owner here)
            print(f"PDF 文件将加密保存：{pdf_path}")
        else:
             save_opts["encryption"] = fitz.PDF_ENCRYPT_NONE # No encryption

        # Save the document with the specified options
        doc.save(str(pdf_path), **save_opts) # Use str(pdf_path) as fitz expects string path
        print(f"PDF 文件已生成并保存：{pdf_path}")

    finally:
        doc.close() # Ensure the fitz document is closed
        del images # Explicitly delete image list to free memory
