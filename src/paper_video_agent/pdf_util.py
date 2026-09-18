import json
import re
from typing import Any

from pathlib import Path

import pymupdf

import fitz


_CAPTION_RE = re.compile(
    r"^\s*(?P<kind>figure|fig\.?|table|图|表)"
    r"(?:\s*(?:\d+|[IVX]+|[一二三四五六七八九十]+))?"
    r"\s*[:：．.]",
    re.IGNORECASE,
)


def _rect_from_bbox(bbox: tuple[float, float, float, float]):
    return pymupdf.Rect(*bbox)


def _rects_are_near(
    first: pymupdf.Rect,
    second: pymupdf.Rect,
    gap: float = 10.0,
) -> bool:
    return not (
        first.x1 + gap < second.x0
        or second.x1 + gap < first.x0
        or first.y1 + gap < second.y0
        or second.y1 + gap < first.y0
    )


def _merge_rects(
    rects: list[pymupdf.Rect],
    gap: float = 10.0,
) -> list[pymupdf.Rect]:
    """Merge nearby drawing/image boxes into complete visual regions."""
    merged: list[pymupdf.Rect] = []

    for rect in rects:
        current = pymupdf.Rect(rect)
        changed = True
        while changed:
            changed = False
            remaining = []
            for previous in merged:
                if _rects_are_near(current, previous, gap=gap):
                    current |= previous
                    changed = True
                else:
                    remaining.append(previous)
            merged = remaining
        merged.append(current)

    return merged


def _visual_regions(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Find large vector/raster regions that can represent figures."""
    candidates: list[pymupdf.Rect] = []

    # Many papers draw figures as vector paths rather than one embedded image.
    # Large drawing boxes catch those figures while ignoring individual glyphs.
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"])
        if rect.y1 < 65 or rect.y0 > page.rect.height - 65:
            continue
        if rect.width < 30 or rect.height < 15:
            continue
        if rect.width * rect.height < 1200:
            continue
        candidates.append(rect)

    # Also include sizeable embedded images. Tiny image placements are often
    # font glyphs or chart icons and are intentionally filtered out here.
    for image in page.get_image_info(xrefs=True):
        rect = pymupdf.Rect(image["bbox"])
        if image.get("xref", 0) == 0:
            continue
        if rect.y1 < 65 or rect.y0 > page.rect.height - 65:
            continue
        if rect.width * rect.height < 1200:
            continue
        candidates.append(rect)

    regions = _merge_rects(candidates, gap=10.0)
    return [
        rect
        for rect in regions
        if rect.width >= 50
        and rect.height >= 25
        and rect.width * rect.height >= 3000
    ]


def _caption_blocks(page: pymupdf.Page) -> list[dict[str, Any]]:
    captions = []
    for block in page.get_text("blocks"):
        if len(block) < 7 or block[6] != 0:
            continue
        text = " ".join(str(block[4]).split())
        match = _CAPTION_RE.match(text)
        if not match:
            continue
        raw_kind = match.group("kind").lower().rstrip(".")
        kind = "table" if raw_kind in {"table", "表"} else "figure"
        captions.append({
            "kind": kind,
            "text": text,
            "bbox": _rect_from_bbox(tuple(block[:4])),
        })
    return captions


def _table_regions(page: pymupdf.Page) -> list[pymupdf.Rect]:
    if not hasattr(page, "find_tables"):
        return []

    try:
        found_tables = page.find_tables().tables
    except Exception:
        return []

    regions = []
    for table in found_tables:
        rect = pymupdf.Rect(table.bbox)
        # find_tables can return tiny text groups as false positives.
        if rect.width < 100 or rect.height < 25:
            continue
        if rect.width * rect.height < 2500:
            continue
        if getattr(table, "row_count", 0) < 2:
            continue
        regions.append(rect)
    return _merge_rects(regions, gap=4.0)


def _table_region_from_rules(
    page: pymupdf.Page,
    caption: pymupdf.Rect,
    stop_y: float | None = None,
) -> pymupdf.Rect | None:
    """Recover borderless/text tables from their horizontal rule lines."""
    lines = []
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"])
        if rect.width < 150 or rect.height > 3:
            continue
        upper_bound = (
            stop_y - 8
            if stop_y is not None
            else caption.y1 + 480
        )
        if caption.y1 - 8 <= rect.y0 <= upper_bound:
            lines.append(rect)

    if len(lines) < 2:
        return None

    return pymupdf.Rect(
        max(page.rect.x0, min(line.x0 for line in lines) - 3),
        # Leave the caption above the first rule out of the crop. The renderer
        # adds a small margin around this rectangle for the table borders.
        max(page.rect.y0, min(line.y0 for line in lines) + 4),
        min(page.rect.x1, max(line.x1 for line in lines) + 3),
        min(page.rect.y1, max(line.y1 for line in lines) + 3),
    )


def _select_nearest_region(
    caption: pymupdf.Rect,
    regions: list[pymupdf.Rect],
    page: pymupdf.Page,
    *,
    allow_below: bool = False,
) -> pymupdf.Rect | None:
    candidates = []
    for region in regions:
        horizontal_overlap = max(
            0.0,
            min(caption.x1, region.x1) - max(caption.x0, region.x0),
        )
        if horizontal_overlap < min(caption.width, region.width) * 0.15:
            continue

        if region.y1 <= caption.y0 + 8:
            distance = caption.y0 - region.y1
        elif allow_below and region.y0 >= caption.y1 - 8:
            distance = region.y0 - caption.y1
        else:
            continue

        if distance > page.rect.height * 0.65:
            continue
        candidates.append((distance, -region.width * region.height, region))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _render_asset(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    output_path: Path,
    zoom: float,
    margin: float = 5.0,
) -> None:
    clipped = pymupdf.Rect(rect)
    clipped.x0 = max(page.rect.x0, clipped.x0 - margin)
    clipped.y0 = max(page.rect.y0, clipped.y0 - margin)
    clipped.x1 = min(page.rect.x1, clipped.x1 + margin)
    clipped.y1 = min(page.rect.y1, clipped.y1 + margin)
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(zoom, zoom),
        clip=clipped,
        alpha=False,
    )
    pixmap.save(output_path)


def extract_pdf_assets(
    pdf_path: str | Path,
    output_dir: str | Path,
    zoom: float = 2.0,
) -> list[dict[str, Any]]:
    """截取论文中的图和表，保存到 metadata 目录。"""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    asset_dirs = {
        "figure": output_dir / "images",
        "table": output_dir / "tables",
    }
    # formulas 是旧版本生成的目录，保留目录清理以免遗留素材继续被误用。
    directories_to_clean = [
        *asset_dirs.values(),
        output_dir / "formulas",
    ]
    for directory in directories_to_clean:
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the metadata directory deterministic when a PDF is re-parsed
        # after a layout or detector change.
        for stale_asset in directory.glob("page_*_*.png"):
            stale_asset.unlink()

    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    doc = pymupdf.open(pdf_path)

    try:
        for page_index, page in enumerate(doc, start=1):
            captions = _caption_blocks(page)
            visual_regions = _visual_regions(page)
            table_regions = _table_regions(page)

            selected_visuals: list[pymupdf.Rect] = []
            selected_tables: list[pymupdf.Rect] = []

            for caption in captions:
                kind = caption["kind"]
                caption_rect = caption["bbox"]
                rect = None
                source = "caption"
                next_caption_y = min(
                    (
                        item["bbox"].y0
                        for item in captions
                        if item["bbox"].y0 > caption_rect.y0
                    ),
                    default=None,
                )

                if kind == "table":
                    rect = _select_nearest_region(
                        caption_rect,
                        table_regions,
                        page,
                        allow_below=True,
                    )
                    if rect is None:
                        rect = _select_nearest_region(
                            caption_rect,
                            visual_regions,
                            page,
                            allow_below=True,
                        )
                        source = "drawing"
                    if rect is None:
                        rect = _table_region_from_rules(
                            page,
                            caption_rect,
                            stop_y=next_caption_y,
                        )
                        if rect is not None:
                            source = "table_rules"
                elif kind == "figure":
                    rect = _select_nearest_region(
                        caption_rect,
                        visual_regions,
                        page,
                    )
                    if rect is not None:
                        selected_visuals.append(rect)

                if rect is None:
                    # Captions are usually directly next to their visual, but
                    # this fallback still produces a useful crop for unusual
                    # layouts or vector-only pages.
                    if kind == "table":
                        # Table captions are conventionally placed above the
                        # table, and are often centered, so use the full text
                        # column rather than only the caption's own width.
                        rect = pymupdf.Rect(
                            max(page.rect.x0, page.rect.x0 + 65),
                            max(page.rect.y0, caption_rect.y0 - 5),
                            min(page.rect.x1, page.rect.x1 - 65),
                            min(page.rect.y1, caption_rect.y1 + 320),
                        )
                    else:
                        rect = pymupdf.Rect(
                            max(page.rect.x0, page.rect.x0 + 65),
                            max(page.rect.y0, caption_rect.y0 - 320),
                            min(page.rect.x1, page.rect.x1 - 65),
                            min(page.rect.y1, caption_rect.y1 + 8),
                        )
                    source = "caption_fallback"

                if kind == "table":
                    selected_tables.append(rect)
                elif kind == "figure":
                    selected_visuals.append(rect)

                key = (
                    kind,
                    page_index,
                    round(rect.x0),
                    round(rect.y0),
                    round(rect.x1 + rect.y1),
                )
                if key in seen:
                    continue
                seen.add(key)

                asset_index = sum(
                    1 for asset in assets if asset["kind"] == kind
                ) + 1
                relative_path = (
                    asset_dirs[kind]
                    / f"page_{page_index:03d}_{kind}_{asset_index:03d}.png"
                )
                _render_asset(page, rect, relative_path, zoom)
                assets.append({
                    "asset_id": relative_path.stem,
                    "kind": kind,
                    "page": page_index,
                    "source": source,
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "normalized_bbox": normalize_bbox(
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        page.rect.width,
                        page.rect.height,
                    ),
                    "path": str(relative_path.relative_to(output_dir)).replace(
                        "\\", "/"
                    ),
                    "caption": caption["text"],
                })

            # Some papers omit a Figure caption (for example an abstract
            # dashboard or a standalone diagram). Preserve those large visual
            # regions as image assets instead of losing them entirely.
            for rect in visual_regions:
                if rect.width * rect.height < 5000:
                    continue
                if any(
                    rect.intersects(previous)
                    for previous in selected_visuals + selected_tables
                ):
                    continue
                asset_index = sum(
                    1 for asset in assets if asset["kind"] == "figure"
                ) + 1
                relative_path = (
                    asset_dirs["figure"]
                    / f"page_{page_index:03d}_figure_{asset_index:03d}.png"
                )
                _render_asset(page, rect, relative_path, zoom)
                assets.append({
                    "asset_id": relative_path.stem,
                    "kind": "figure",
                    "page": page_index,
                    "source": "visual_detector",
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "normalized_bbox": normalize_bbox(
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        page.rect.width,
                        page.rect.height,
                    ),
                    "path": str(relative_path.relative_to(output_dir)).replace(
                        "\\", "/"
                    ),
                })

            # Tables without a caption are still useful as future visual
            # targets. Usually these are appendix tables.
            for rect in table_regions:
                if any(
                    rect.intersects(previous)
                    for previous in selected_tables + selected_visuals
                ):
                    continue
                asset_index = sum(
                    1 for asset in assets if asset["kind"] == "table"
                ) + 1
                relative_path = (
                    asset_dirs["table"]
                    / f"page_{page_index:03d}_table_{asset_index:03d}.png"
                )
                _render_asset(page, rect, relative_path, zoom)
                assets.append({
                    "asset_id": relative_path.stem,
                    "kind": "table",
                    "page": page_index,
                    "source": "table_detector",
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "normalized_bbox": normalize_bbox(
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        page.rect.width,
                        page.rect.height,
                    ),
                    "path": str(relative_path.relative_to(output_dir)).replace(
                        "\\", "/"
                    ),
                })

        (output_dir / "assets.json").write_text(
            json.dumps(
                {"pdf": pdf_path.name, "assets": assets},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return assets
    finally:
        doc.close()


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
    metadata_dir: str | Path | None = None,
) -> tuple[dict[str, Any], dict[int, Path]]:
    text_data = pdf2text(pdf_path)
    page_images = pdf2images(pdf_path, output_dir, zoom)

    if metadata_dir is None:
        metadata_dir = Path(output_dir).parent / "metadata"

    text_data["assets"] = extract_pdf_assets(
        pdf_path=pdf_path,
        output_dir=metadata_dir,
        zoom=zoom,
    )
    return text_data, page_images
