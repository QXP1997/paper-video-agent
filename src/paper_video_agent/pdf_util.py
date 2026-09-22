from pathlib import Path
from typing import Any

import pymupdf


def pdf2text(pdf_path: str | Path) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 不存在: {pdf_path}")

    doc = pymupdf.open(pdf_path)
    result = {
        "file_name": pdf_path.name,
        "page_count": len(doc),
        "pages": [],
    }

    try:
        for page_index, page in enumerate(doc):
            result["pages"].append({
                "page": page_index + 1,
                "text": page.get_text("text").strip(),
            })

        return result
    finally:
        doc.close()


def pdf2images(
    pdf_path: str | Path,
    output_dir: str | Path,
    zoom: float = 2.0,
) -> dict[int, Path]:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    page_images: dict[int, Path] = {}

    try:
        matrix = pymupdf.Matrix(zoom, zoom)
        for index, page in enumerate(doc):
            page_number = index + 1
            output_path = output_dir / f"page_{page_number:03d}.png"
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(output_path)
            page_images[page_number] = output_path

        return page_images
    finally:
        doc.close()


def parse_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    zoom: float = 2.0,
) -> tuple[dict[str, Any], dict[int, Path]]:
    """Extract page text and render each complete PDF page."""
    text_data = pdf2text(pdf_path)
    page_images = pdf2images(pdf_path, output_dir, zoom)
    return text_data, page_images
