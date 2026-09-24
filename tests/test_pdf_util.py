import io
import json
import zipfile
from pathlib import Path

import pymupdf

from paper_video_agent.pdf_util import _read_content_list, parse_pdf


class FakeMinerUClient:
    def parse_pdf(self, pdf_path: Path) -> list[dict]:
        assert pdf_path.name == "paper.pdf"
        return [
            {"type": "text", "text": "Full page test", "page_idx": 0},
        ]


def test_parse_pdf_extracts_text_and_renders_complete_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 60), "Full page test")
    document.save(pdf_path)
    document.close()

    text_data, page_images = parse_pdf(
        pdf_path,
        tmp_path / "images",
        zoom=1,
        mineru_client=FakeMinerUClient(),
    )

    assert text_data == {
        "file_name": "paper.pdf",
        "page_count": 1,
        "pages": [{"page": 1, "text": "Full page test"}],
    }
    assert page_images[1].is_file()


def test_parse_pdf_combines_mineru_blocks_by_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    document.new_page()
    document.new_page()
    document.save(pdf_path)
    document.close()

    class StructuredMinerUClient:
        def parse_pdf(self, pdf_path: Path) -> list[dict]:
            return [
                {"type": "text", "text": "Heading", "page_idx": 0},
                {"type": "equation", "text": "E = mc^2", "page_idx": 0},
                {
                    "type": "table",
                    "table_caption": ["Results"],
                    "table_body": "| A | B |\n| - | - |",
                    "page_idx": 1,
                },
                {
                    "type": "image",
                    "image_caption": ["Figure 1"],
                    "page_idx": 1,
                },
            ]

    text_data, _ = parse_pdf(
        pdf_path,
        tmp_path / "images",
        zoom=0.2,
        mineru_client=StructuredMinerUClient(),
    )

    assert text_data["pages"] == [
        {"page": 1, "text": "Heading\n\nE = mc^2"},
        {
            "page": 2,
            "text": "Results\n| A | B |\n| - | - |\n\nFigure 1",
        },
    ]


def test_parse_pdf_reuses_matching_mineru_cache(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(pdf_path)
    document.close()

    class OneShotMinerUClient:
        calls = 0

        def parse_pdf(self, pdf_path: Path) -> list[dict]:
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("MinerU should not be called for a matching cache")
            return [{"type": "text", "text": "cached text", "page_idx": 0}]

    client = OneShotMinerUClient()
    first, _ = parse_pdf(pdf_path, tmp_path / "images", zoom=0.2, mineru_client=client)
    second, _ = parse_pdf(pdf_path, tmp_path / "images", zoom=0.2, mineru_client=client)

    assert first == second
    assert client.calls == 1


def test_read_content_list_from_mineru_zip() -> None:
    expected = [{"type": "text", "text": "hello", "page_idx": 0}]
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("result/document_content_list.json", json.dumps(expected))

    assert _read_content_list(archive_bytes.getvalue()) == expected
