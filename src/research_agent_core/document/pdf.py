"""Reusable PDF ingestion backed by MinerU and PyMuPDF."""

import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import httpx
import pymupdf

from research_agent_core.artifacts import sha256_file as _sha256_file
from research_agent_core.env import load_local_env as _load_local_env

MINERU_API_BASE_URL = "https://mineru.net/api/v4"
MINERU_PARSE_VERSION = 3
MINERU_MODEL_VERSION = "vlm"
MINERU_LANGUAGE = "en"


class MinerUError(RuntimeError):
    """Raised when MinerU cannot parse a PDF."""


_load_local_env()


def _mineru_error_message(result: object, fallback: str) -> str:
    if isinstance(result, dict):
        message = result.get("msg") or result.get("message")
        if message:
            return str(message)
    return fallback


class MinerUClient:
    """Small synchronous client for MinerU's official precision API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 600,
        poll_interval: float = 3,
        request_timeout: float = 60,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("MINERU_API_KEY", "")).strip()
        if not self.api_key:
            raise MinerUError(
                "未配置 MINERU_API_KEY，无法调用 MinerU 解析 PDF；"
                "请在本地 .env 或系统环境变量中配置"
            )
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=request_timeout,
            follow_redirects=True,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "MinerUClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def parse_pdf(
        self,
        pdf_path: str | Path,
        *,
        result_dir: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Upload a PDF, retain its extracted result, and return content records."""
        pdf_path = Path(pdf_path)
        batch_id, upload_url = self._create_upload(pdf_path.name)

        with pdf_path.open("rb") as source:
            upload_response = self.client.put(upload_url, content=source)
        upload_response.raise_for_status()

        zip_bytes = self._poll_result(batch_id)
        content_list = _read_content_list(zip_bytes)
        if result_dir is not None:
            _extract_mineru_result(zip_bytes, Path(result_dir))
        return content_list

    def _create_upload(self, file_name: str) -> tuple[str, str]:
        response = self.client.post(
            f"{MINERU_API_BASE_URL}/file-urls/batch",
            headers={**self._headers, "Content-Type": "application/json"},
            json={
                "files": [{"name": file_name}],
                "language": MINERU_LANGUAGE,
                "model_version": MINERU_MODEL_VERSION,
                "enable_formula": True,
                "enable_table": True,
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise MinerUError(_mineru_error_message(result, "申请 MinerU 上传地址失败"))

        data = result.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise MinerUError(f"MinerU 上传响应缺少 batch_id 或 file_urls: {result}")
        return str(batch_id), str(file_urls[0])

    def _poll_result(self, batch_id: str) -> bytes:
        deadline = time.monotonic() + self.timeout
        result_url = f"{MINERU_API_BASE_URL}/extract-results/batch/{batch_id}"

        while time.monotonic() < deadline:
            response = self.client.get(result_url, headers=self._headers)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise MinerUError(_mineru_error_message(result, "查询 MinerU 解析结果失败"))

            items = (result.get("data") or {}).get("extract_result") or []
            if not items:
                time.sleep(self.poll_interval)
                continue

            item = items[0]
            state = item.get("state")
            if state == "done":
                zip_url = item.get("full_zip_url")
                if not zip_url:
                    raise MinerUError(f"MinerU 解析完成但未返回 full_zip_url: {item}")
                download = self.client.get(zip_url)
                download.raise_for_status()
                return download.content
            if state == "failed":
                message = item.get("err_msg") or item.get("error") or "未知错误"
                raise MinerUError(f"MinerU PDF 解析失败: {message}")

            time.sleep(self.poll_interval)

        raise MinerUError(f"MinerU PDF 解析超时（batch_id: {batch_id}）")


def _read_content_list(zip_bytes: bytes) -> list[dict[str, Any]]:
    """Read MinerU's page-aware content list from a result archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.lower().endswith("content_list.json")
                and not name.endswith("/")
            ]
            if not candidates:
                raise MinerUError("MinerU 结果压缩包中不存在 content_list.json")

            preferred = min(candidates, key=lambda name: (name.count("/"), len(name)))
            raw_content = archive.read(preferred).decode("utf-8-sig")
    except zipfile.BadZipFile as exc:
        raise MinerUError("MinerU 返回的结果不是有效 ZIP 文件") from exc

    content_list = json.loads(raw_content)
    if not isinstance(content_list, list):
        raise MinerUError("MinerU content_list.json 的根节点不是列表")
    return [item for item in content_list if isinstance(item, dict)]


def _load_extracted_content_list(result_dir: Path) -> list[dict[str, Any]] | None:
    """Load the legacy page-aware content list from an extracted MinerU result."""
    if not result_dir.is_dir():
        return None

    candidates = [
        path
        for path in result_dir.rglob("*.json")
        if path.name.lower().endswith("content_list.json")
    ]
    if not candidates:
        return None

    preferred = min(
        candidates,
        key=lambda path: (len(path.relative_to(result_dir).parts), len(str(path))),
    )
    try:
        content_list = json.loads(preferred.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(content_list, list):
        return None
    return [item for item in content_list if isinstance(item, dict)]


def _extract_mineru_result(zip_bytes: bytes, result_dir: Path) -> None:
    """Extract a MinerU archive without retaining the downloaded ZIP file."""
    result_dir = result_dir.resolve()
    result_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{result_dir.name}-", dir=result_dir.parent)
    )

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            for member in archive.infolist():
                # ZIP member names are POSIX-style, but also reject backslash-based
                # traversal produced by non-conforming archive writers.
                normalized_name = member.filename.replace("\\", "/")
                archive_path = PurePosixPath(normalized_name)
                parts = archive_path.parts
                if (
                    not parts
                    or archive_path.is_absolute()
                    or PureWindowsPath(normalized_name).drive
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    raise MinerUError(f"MinerU 结果压缩包包含不安全路径: {member.filename}")

                destination = temporary_dir.joinpath(*parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise MinerUError("MinerU 返回的结果不是有效 ZIP 文件") from exc
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise

    try:
        if result_dir.exists():
            if result_dir.is_dir():
                shutil.rmtree(result_dir)
            else:
                result_dir.unlink()
        temporary_dir.replace(result_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _content_item_text(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "table":
        fields = ("table_caption", "table_body", "table_footnote")
    elif item_type == "image":
        fields = ("image_caption", "image_footnote")
    elif item_type == "code":
        # Captioned code blocks can be important figures (as in the OpenHands
        # paper). Skip long appendix listings that have no caption.
        fields = ("code_caption", "code_body") if item.get("code_caption") else ()
    elif item_type in {"equation", "formula"}:
        fields = ("text", "latex", "equation", "content")
    else:
        fields = ("text",)

    parts = [_as_text(item.get(field)) for field in fields]
    return "\n".join(part for part in parts if part)


def _visual_kind(item: dict[str, Any]) -> str | None:
    item_type = item.get("type")
    if item_type in {"image", "table"}:
        return str(item_type)
    if item_type in {"equation", "formula"}:
        return "formula"
    if item_type == "code" and item.get("code_caption"):
        return "code"
    return None


def _visual_caption(item: dict[str, Any], kind: str) -> str:
    fields = {
        "image": ("image_caption", "image_footnote"),
        "table": ("table_caption", "table_footnote"),
        "code": ("code_caption",),
        "formula": ("text", "latex", "equation", "content"),
    }[kind]
    parts = [_as_text(item.get(field)) for field in fields]
    return "\n".join(part for part in parts if part)


def _to_page_texts(
    content_list: list[dict[str, Any]],
    page_count: int,
) -> list[dict[str, Any]]:
    page_parts: dict[int, list[str]] = {page: [] for page in range(1, page_count + 1)}
    page_visuals: dict[int, list[dict[str, Any]]] = {
        page: [] for page in range(1, page_count + 1)
    }
    visual_counts: dict[tuple[int, str], int] = {}
    for item in content_list:
        page_index = item.get("page_idx")
        if not isinstance(page_index, int):
            continue
        page_number = page_index + 1
        if page_number not in page_parts:
            continue
        text = _content_item_text(item)
        if text:
            page_parts[page_number].append(text)

        kind = _visual_kind(item)
        if kind is None:
            continue
        count_key = (page_number, kind)
        visual_counts[count_key] = visual_counts.get(count_key, 0) + 1
        visual = {
            "id": f"page_{page_number:03d}_{kind}_{visual_counts[count_key]:02d}",
            "type": kind,
            "page": page_number,
            "caption": _visual_caption(item, kind),
        }
        asset_path = _as_text(item.get("img_path"))
        if asset_path:
            visual["asset_path"] = asset_path
        bbox = item.get("bbox")
        if (
            isinstance(bbox, list)
            and len(bbox) == 4
            and all(isinstance(value, (int, float)) for value in bbox)
        ):
            visual["bbox"] = bbox
        page_visuals[page_number].append(visual)

    return [
        {
            "page": page,
            "text": "\n\n".join(page_parts[page]),
            "visuals": page_visuals[page],
        }
        for page in range(1, page_count + 1)
    ]


def _load_cached_text(cache_path: Path, pdf_sha256: str) -> dict[str, Any] | None:
    if not cache_path.is_file():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if (
        cached.get("parse_version") != MINERU_PARSE_VERSION
        or cached.get("pdf_sha256") != pdf_sha256
        or cached.get("model_version") != MINERU_MODEL_VERSION
    ):
        return None
    text_data = cached.get("text_data")
    return text_data if isinstance(text_data, dict) else None


def _save_cached_text(
    cache_path: Path,
    pdf_sha256: str,
    text_data: dict[str, Any],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "parse_version": MINERU_PARSE_VERSION,
                "pdf_sha256": pdf_sha256,
                "model_version": MINERU_MODEL_VERSION,
                "text_data": text_data,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)


def pdf2text(
    pdf_path: str | Path,
    cache_path: str | Path | None = None,
    *,
    mineru_result_dir: str | Path | None = None,
    mineru_client: MinerUClient | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF 不存在: {pdf_path}")

    with pymupdf.open(pdf_path) as document:
        page_count = len(document)

    pdf_sha256 = _sha256_file(pdf_path)
    resolved_cache_path = Path(cache_path) if cache_path is not None else None
    resolved_result_dir = Path(mineru_result_dir) if mineru_result_dir is not None else None
    extracted_content_list = (
        _load_extracted_content_list(resolved_result_dir)
        if resolved_result_dir is not None
        else None
    )
    if resolved_cache_path is not None:
        cached = _load_cached_text(resolved_cache_path, pdf_sha256)
        result_is_available = resolved_result_dir is None or extracted_content_list is not None
        if cached is not None and result_is_available:
            print(f"复用已有 MinerU 解析结果: {resolved_cache_path}")
            return cached

    if extracted_content_list is not None:
        print(f"从已解压的 MinerU 结果重建分页缓存: {resolved_result_dir}")
        content_list = extracted_content_list
    elif mineru_client is None:
        with MinerUClient() as client:
            content_list = client.parse_pdf(pdf_path, result_dir=resolved_result_dir)
    else:
        content_list = mineru_client.parse_pdf(pdf_path, result_dir=resolved_result_dir)

    text_data = {
        "file_name": pdf_path.name,
        "page_count": page_count,
        "pages": _to_page_texts(content_list, page_count),
    }
    if resolved_cache_path is not None:
        _save_cached_text(resolved_cache_path, pdf_sha256, text_data)
    return text_data


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
    page_image_dir: str | Path,
    zoom: float = 2.0,
    *,
    cache_path: str | Path | None = None,
    mineru_result_dir: str | Path | None = None,
    mineru_client: MinerUClient | None = None,
) -> tuple[dict[str, Any], dict[int, Path]]:
    """Parse one PDF into page evidence and complete page images.

    Storage locations are explicit so each agent can choose its own workspace
    layout while sharing the same parsing and cache implementation.
    """
    text_data = pdf2text(
        pdf_path,
        cache_path,
        mineru_result_dir=mineru_result_dir,
        mineru_client=mineru_client,
    )
    page_images = pdf2images(pdf_path, page_image_dir, zoom)
    return text_data, page_images

