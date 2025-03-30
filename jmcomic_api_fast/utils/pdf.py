import os
import subprocess
from pathlib import Path
import time
import shutil


def merge_webp_to_pdf(folder_path, pdf_path, password=None):
    """
    将指定文件夹下的所有 .webp 文件按照文件名顺序合并为 PDF 长图，并可选使用 qpdf 命令行工具加密。

    :param folder_path: 包含 .webp 文件的文件夹路径
    :param pdf_path: 输出 PDF 文件的路径
    :param password: PDF 文件的密码（可选）
    """
    output_dir = Path(pdf_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 使用 ImageMagick 合并 .webp 文件为 PDF
    try:
        merge_start = time.time()
        subprocess.run(
            ["convert"] + [f"{folder_path}/*"] + [str(pdf_path)],
            check=True
        )
        merge_end = time.time()
        merge_duration = merge_end - merge_start
        print(f"PDF 文件已生成并保存：{pdf_path}")
        print(f"合并耗时：{merge_duration:.2f} 秒")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ImageMagick 合并失败: {e}")

    # 如果需要密码，则使用 qpdf 命令行工具进行加密
    if password:
        encryption_start = time.time()
        print(f"PDF 文件将加密保存：{pdf_path}")
        pdf_temp_path = str(pdf_path) + ".temp"
        shutil.move(pdf_path, pdf_temp_path)  # 临时重命名文件
        subprocess.run([
            "qpdf",
            "--encrypt", password, password, "256",
            "--print=full",
            "--extract=y",
            "--", pdf_temp_path, str(pdf_path)
        ], check=True)
        encryption_end = time.time()
        os.remove(pdf_temp_path)  # 删除临时文件
        encryption_duration = encryption_end - encryption_start

        print(f"PDF 文件已生成并保存：{pdf_path}")
        print(f"加密耗时：{encryption_duration:.2f} 秒")