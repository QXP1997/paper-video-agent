"""Backward-compatible Paper Video adapter for shared PDF ingestion."""

from pathlib import Path

from research_agent_core.document.pdf import (
    MINERU_API_BASE_URL,
    MINERU_LANGUAGE,
    MINERU_MODEL_VERSION,
    MINERU_PARSE_VERSION,
    MinerUClient,
    MinerUError,
    _as_text,
    _content_item_text,
    _extract_mineru_result,
    _load_cached_text,
    _load_extracted_content_list,
    _mineru_error_message,
    _read_content_list,
    _save_cached_text,
    _to_page_texts,
    _visual_caption,
    _visual_kind,
    pdf2images,
    pdf2text,
)
from research_agent_core.document.pdf import (
    parse_pdf as _parse_pdf,
)

__all__ = [
    "MINERU_API_BASE_URL",
    "MINERU_LANGUAGE",
    "MINERU_MODEL_VERSION",
    "MINERU_PARSE_VERSION",
    "MinerUClient",
    "MinerUError",
    "_as_text",
    "_content_item_text",
    "_extract_mineru_result",
    "_load_cached_text",
    "_load_extracted_content_list",
    "_mineru_error_message",
    "_read_content_list",
    "_save_cached_text",
    "_to_page_texts",
    "_visual_caption",
    "_visual_kind",
    "parse_pdf",
    "pdf2images",
    "pdf2text",
]


def parse_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    zoom: float = 2.0,
    *,
    mineru_client: MinerUClient | None = None,
) -> tuple[dict, dict[int, Path]]:
    """Preserve the Paper Video workspace layout while using shared parsing."""
    output_dir = Path(output_dir)
    mineru_output_dir = output_dir.parent / "output"
    return _parse_pdf(
        pdf_path,
        output_dir,
        zoom,
        cache_path=mineru_output_dir / "mineru_parse.json",
        mineru_result_dir=mineru_output_dir / "mineru",
        mineru_client=mineru_client,
    )
