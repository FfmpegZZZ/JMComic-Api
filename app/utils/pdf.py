from pathlib import Path
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter

def merge_webp_to_pdf(folder_path, pdf_path, password=None):
    """Merge all .webp files (recursively) in folder_path into a single PDF.

    If password is provided, encrypt the resulting PDF using it.
    """
    output_dir = Path(pdf_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    webp_files = sorted(Path(folder_path).rglob("*.webp"))
    if not webp_files:
        raise FileNotFoundError(f"文件夹 {folder_path} 中没有找到 .webp 文件")

    images = [Image.open(webp).convert("RGB") for webp in webp_files]

    images[0].save(pdf_path, save_all=True, append_images=images[1:])
    for image in images:
        image.close()
    del images

    print(f"PDF 文件已生成：{pdf_path}")

    pdf_reader = PdfReader(pdf_path)
    pdf_writer = PdfWriter()

    for page in pdf_reader.pages:
        page.compress_content_streams()
        pdf_writer.add_page(page)

    if password:
        pdf_writer.encrypt(password)
        print(f"PDF 文件已加密：{pdf_path}")

    with open(pdf_path, "wb") as f:
        pdf_writer.write(f)
