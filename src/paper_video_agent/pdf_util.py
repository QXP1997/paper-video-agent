from typing import Any

from pathlib import Path

import pymupdf

import fitz


def normalize_bbox(
    bbox: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> list[float]:
    """
    将 PDF 坐标转换成 0~1 的归一化坐标。

    原始：
    [x0, y0, x1, y1]

    返回：
    [left, top, right, bottom]
    """
    x0, y0, x1, y1 = bbox

    return [
        x0 / page_width,
        y0 / page_height,
        x1 / page_width,
        y1 / page_height,
    ]


def pdf2text(pdf_path: str | Path) -> dict[str, Any]:
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 不存在: {pdf_path}")

    doc = fitz.open(pdf_path)

    result = {
        "file_name": pdf_path.name,
        "page_count": len(doc),
        "pages": [],
    }

    try:
        for page_index, page in enumerate(doc):
            page_width = page.rect.width
            page_height = page.rect.height

            page_data = {
                "page": page_index + 1,
                "width": page_width,
                "height": page_height,
                "text": page.get_text("text").strip(),
                "blocks": [],
            }

            # 每一个 block 都带坐标
            blocks = page.get_text("blocks")

            for block_index, block in enumerate(blocks):
                # PyMuPDF blocks:
                # x0, y0, x1, y1, text, block_no, block_type
                x0, y0, x1, y1, text, block_no, block_type = block[:7]

                text = text.strip()

                # block_type == 0 一般表示文本块
                if block_type != 0:
                    continue

                if not text:
                    continue

                bbox = (x0, y0, x1, y1)

                page_data["blocks"].append({
                    "block_id": f"page_{page_index + 1}_block_{block_index}",
                    "text": text,
                    "bbox": [
                        x0,
                        y0,
                        x1,
                        y1,
                    ],
                    "normalized_bbox": normalize_bbox(
                        bbox,
                        page_width,
                        page_height,
                    ),
                })

            result["pages"].append(page_data)

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

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    doc = pymupdf.open(pdf_path)

    page_images: dict[int, Path] = {}

    try:
        matrix = pymupdf.Matrix(zoom, zoom)

        for index, page in enumerate(doc):
            page_number = index + 1

            output_path = (
                output_dir
                / f"page_{page_number:03d}.png"
            )

            pix = page.get_pixmap(
                matrix=matrix,
                alpha=False,
            )

            pix.save(output_path)

            page_images[page_number] = output_path

        return page_images

    finally:
        doc.close()


def parse_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    zoom: float = 2.0,
) -> tuple[dict[str, Any], dict[int, Path]]:
    return pdf2text(pdf_path), pdf2images(pdf_path, output_dir, zoom)
