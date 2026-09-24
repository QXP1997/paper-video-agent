import io
import json
import zipfile
from pathlib import Path

import pymupdf

from paper_video_agent.pdf_util import (
    _extract_mineru_result,
    _read_content_list,
    parse_pdf,
    pdf2text,
)


class FakeMinerUClient:
    def parse_pdf(self, pdf_path: Path, *, result_dir: Path | None = None) -> list[dict]:
        assert pdf_path.name == "paper.pdf"
        if result_dir is not None:
            result_dir.mkdir(parents=True)
            (result_dir / "paper_content_list.json").write_text("[]", encoding="utf-8")
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
        "pages": [{"page": 1, "text": "Full page test", "visuals": []}],
    }
    assert page_images[1].is_file()
    assert (tmp_path / "output" / "mineru" / "paper_content_list.json").is_file()


def test_parse_pdf_combines_mineru_blocks_by_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    document.new_page()
    document.new_page()
    document.save(pdf_path)
    document.close()

    class StructuredMinerUClient:
        def parse_pdf(self, pdf_path: Path, *, result_dir: Path | None = None) -> list[dict]:
            if result_dir is not None:
                result_dir.mkdir(parents=True)
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
        {
            "page": 1,
            "text": "Heading\n\nE = mc^2",
            "visuals": [{
                "id": "page_001_formula_01",
                "type": "formula",
                "page": 1,
                "caption": "E = mc^2",
            }],
        },
        {
            "page": 2,
            "text": "Results\n| A | B |\n| - | - |\n\nFigure 1",
            "visuals": [
                {
                    "id": "page_002_table_01",
                    "type": "table",
                    "page": 2,
                    "caption": "Results",
                },
                {
                    "id": "page_002_image_01",
                    "type": "image",
                    "page": 2,
                    "caption": "Figure 1",
                },
            ],
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

        def parse_pdf(self, pdf_path: Path, *, result_dir: Path | None = None) -> list[dict]:
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("MinerU should not be called for a matching cache")
            if result_dir is not None:
                result_dir.mkdir(parents=True)
                (result_dir / "paper_content_list.json").write_text(
                    json.dumps([{
                        "type": "text",
                        "text": "cached text",
                        "page_idx": 0,
                    }]),
                    encoding="utf-8",
                )
            return [{"type": "text", "text": "cached text", "page_idx": 0}]

    client = OneShotMinerUClient()
    first, _ = parse_pdf(pdf_path, tmp_path / "images", zoom=0.2, mineru_client=client)
    second, _ = parse_pdf(pdf_path, tmp_path / "images", zoom=0.2, mineru_client=client)

    assert first == second
    assert client.calls == 1


def test_pdf2text_rebuilds_cache_from_extracted_mineru_result(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(pdf_path)
    document.close()

    result_dir = tmp_path / "output" / "mineru"
    result_dir.mkdir(parents=True)
    (result_dir / "paper_content_list.json").write_text(
        json.dumps([{
            "type": "image",
            "image_caption": ["Figure 1: Architecture"],
            "img_path": "images/figure.jpg",
            "bbox": [1, 2, 3, 4],
            "page_idx": 0,
        }]),
        encoding="utf-8",
    )

    class UnexpectedMinerUClient:
        def parse_pdf(self, pdf_path: Path, *, result_dir: Path | None = None) -> list[dict]:
            raise AssertionError("Retained MinerU result should be reused")

    text_data = pdf2text(
        pdf_path,
        tmp_path / "output" / "mineru_parse.json",
        mineru_result_dir=result_dir,
        mineru_client=UnexpectedMinerUClient(),
    )

    assert text_data["pages"][0]["visuals"] == [{
        "id": "page_001_image_01",
        "type": "image",
        "page": 1,
        "caption": "Figure 1: Architecture",
        "asset_path": "images/figure.jpg",
        "bbox": [1, 2, 3, 4],
    }]
    assert (tmp_path / "output" / "mineru_parse.json").is_file()


def test_read_content_list_from_mineru_zip() -> None:
    expected = [{"type": "text", "text": "hello", "page_idx": 0}]
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("result/document_content_list.json", json.dumps(expected))

    assert _read_content_list(archive_bytes.getvalue()) == expected


def test_extract_mineru_result_keeps_files_without_zip(tmp_path: Path) -> None:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("result/document.md", "# Paper")
        archive.writestr("result/images/figure.png", b"image")

    result_dir = tmp_path / "output" / "mineru"
    _extract_mineru_result(archive_bytes.getvalue(), result_dir)

    assert (result_dir / "result" / "document.md").read_text(encoding="utf-8") == "# Paper"
    assert (result_dir / "result" / "images" / "figure.png").read_bytes() == b"image"
    assert not list(tmp_path.rglob("*.zip"))


def test_extract_mineru_result_rejects_parent_traversal(tmp_path: Path) -> None:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")

    result_dir = tmp_path / "output" / "mineru"
    try:
        _extract_mineru_result(archive_bytes.getvalue(), result_dir)
    except RuntimeError as exc:
        assert "不安全路径" in str(exc)
    else:
        raise AssertionError("Unsafe MinerU archive should be rejected")

    assert not (tmp_path / "outside.txt").exists()
    assert not result_dir.exists()
