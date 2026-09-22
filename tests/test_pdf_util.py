from pathlib import Path

import pymupdf

from paper_video_agent.pdf_util import parse_pdf


def test_parse_pdf_extracts_text_and_renders_complete_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 60), "Full page test")
    document.save(pdf_path)
    document.close()

    text_data, page_images = parse_pdf(pdf_path, tmp_path / "images", zoom=1)

    assert text_data == {
        "file_name": "paper.pdf",
        "page_count": 1,
        "pages": [{"page": 1, "text": "Full page test"}],
    }
    assert page_images[1].is_file()
