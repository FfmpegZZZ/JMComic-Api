import os
import subprocess
from pathlib import Path
import time
import shutil
import img2pdf


def merge_webp_to_pdf(folder_path, pdf_path, password=None):
    """
    将指定文件夹下的所有 .jpeg 文件按照文件名顺序合并为 PDF 长图，并可选使用 qpdf 命令行工具加密。

    :param folder_path: 包含 .jpeg 文件的文件夹路径
    :param pdf_path: 输出 PDF 文件的路径
    :param password: PDF 文件的密码（可选）
    """
    output_dir = Path(pdf_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 使用 img2pdf 合并 .webp 文件为 PDF
    try:
        merge_start = time.time()
        webp_files = sorted(Path(folder_path).glob("*.jpeg"))  # 按文件名排序
        if not webp_files:
            raise RuntimeError("指定文件夹中没有找到 .jpeg 文件")

        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert([str(file) for file in webp_files]))

        merge_end = time.time()
        merge_duration = merge_end - merge_start
        print(f"PDF 文件已生成并保存：{pdf_path}")
        print(f"合并耗时：{merge_duration:.2f} 秒")
    except Exception as e:
        raise RuntimeError(f"img2pdf 合并失败: {e}")

    # 如果需要密码，则调用加密函数
    if password:
        encrypt_pdf(pdf_path, password)


def encrypt_pdf(pdf_path, password):
    """
    使用 qpdf 对 PDF 文件进行加密。

    :param pdf_path: 要加密的 PDF 文件路径
    :param password: PDF 文件的密码
    """
    encryption_start = time.time()
    print(f"PDF 文件将加密保存：{pdf_path}")
    pdf_temp_path = str(pdf_path) + ".temp"
    shutil.move(pdf_path, pdf_temp_path)  # 临时重命名文件
    try:
        subprocess.run([
            "qpdf",
            "--encrypt", password, password, "256",
            "--print=full",
            "--extract=y",
            "--", pdf_temp_path, str(pdf_path)
        ], check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"PDF 加密失败: {e}")
    finally:
        os.remove(pdf_temp_path)  # 删除临时文件

    encryption_end = time.time()
    encryption_duration = encryption_end - encryption_start
    print(f"PDF 文件已加密并保存：{pdf_path}")
    print(f"加密耗时：{encryption_duration:.2f} 秒")