# -*- coding: utf-8 -*-
"""托管运行时归档的受限 HTTPS 下载器。"""

from __future__ import annotations

import logging
import os
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from qharness.exception import RuntimeInstallationError
from qharness.runtime.models import (
    RuntimePreparationPhase,
    RuntimePreparationProgress,
    RuntimeProgressCallback,
    RuntimeSpec,
)


_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_LOGGER = logging.getLogger(__name__)


def download_runtime_archive(
    spec: RuntimeSpec,
    destination: Path,
    *,
    progress_callback: RuntimeProgressCallback | None = None,
    cancellation_event: threading.Event | None = None,
) -> None:
    """下载清单固定的归档，并在同目录内原子发布完整文件。"""

    if spec.download_url is None or spec.archive_size_bytes is None:
        raise RuntimeInstallationError(
            f"{spec.name.value} 运行时清单没有可用下载信息。"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (
        f".{destination.name}.{uuid.uuid4().hex}.part"
    )
    request = urllib.request.Request(
        spec.download_url,
        headers={"User-Agent": "QHarness-RuntimeManager/0.1"},
        method="GET",
    )
    completed = 0
    _LOGGER.info(
        "开始下载托管运行时：name=%s，version=%s，url=%s",
        spec.name.value,
        spec.version,
        spec.download_url,
    )
    _emit_progress(
        spec,
        RuntimePreparationPhase.DOWNLOADING,
        progress_callback,
        completed_bytes=0,
        total_bytes=spec.archive_size_bytes,
        message=f"正在下载 {spec.name.value} {spec.version}。",
    )
    try:
        _raise_if_cancelled(cancellation_event)
        with urllib.request.urlopen(
            request,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            if not response.geturl().startswith("https://"):
                raise RuntimeInstallationError(
                    "运行时下载被重定向到非 HTTPS 地址，已拒绝继续。"
                )
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    response_size = int(declared_length)
                except ValueError as error:
                    raise RuntimeInstallationError(
                        "运行时下载响应包含无效 Content-Length。"
                    ) from error
                if response_size != spec.archive_size_bytes:
                    raise RuntimeInstallationError(
                        f"{spec.name.value} 运行时下载大小不匹配："
                        f"期望 {spec.archive_size_bytes}，实际 {response_size}。"
                    )

            with temporary.open("xb") as output:
                while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                    _raise_if_cancelled(cancellation_event)
                    completed += len(chunk)
                    if completed > spec.archive_size_bytes:
                        raise RuntimeInstallationError(
                            f"{spec.name.value} 运行时下载内容超过清单限制。"
                        )
                    output.write(chunk)
                    _emit_progress(
                        spec,
                        RuntimePreparationPhase.DOWNLOADING,
                        progress_callback,
                        completed_bytes=completed,
                        total_bytes=spec.archive_size_bytes,
                        message=f"正在下载 {spec.name.value} {spec.version}。",
                    )

        if completed != spec.archive_size_bytes:
            raise RuntimeInstallationError(
                f"{spec.name.value} 运行时下载不完整："
                f"期望 {spec.archive_size_bytes} 字节，实际 {completed} 字节。"
            )
        os.replace(temporary, destination)
        _LOGGER.info(
            "托管运行时下载完成：name=%s，version=%s，bytes=%s",
            spec.name.value,
            spec.version,
            completed,
        )
    except RuntimeInstallationError:
        raise
    except urllib.error.URLError as error:
        raise RuntimeInstallationError(
            f"下载 {spec.name.value} 托管运行时失败：{error}"
        ) from error
    except OSError as error:
        raise RuntimeInstallationError(
            f"保存 {spec.name.value} 运行时归档失败：{error}"
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            _LOGGER.warning("无法清理运行时下载临时文件：%s", temporary)


def _raise_if_cancelled(event: threading.Event | None) -> None:
    """在调用方请求取消时立即停止下载。"""

    if event is not None and event.is_set():
        raise RuntimeInstallationError("运行时下载已被用户取消。")


def _emit_progress(
    spec: RuntimeSpec,
    phase: RuntimePreparationPhase,
    callback: RuntimeProgressCallback | None,
    *,
    completed_bytes: int | None = None,
    total_bytes: int | None = None,
    message: str,
) -> None:
    """向可选客户端回调发送一份不可变进度快照。"""

    if callback is None:
        return
    callback(
        RuntimePreparationProgress(
            name=spec.name,
            phase=phase,
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            message=message,
        )
    )
