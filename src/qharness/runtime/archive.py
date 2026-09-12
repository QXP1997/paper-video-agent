# -*- coding: utf-8 -*-
"""托管运行时归档的摘要校验和安全安装。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from qharness.exception import (
    RuntimeConfigurationError,
    RuntimeInstallationError,
)
from qharness.runtime.models import RuntimeSpec


RUNTIME_READY_MARKER_NAME = ".qharness-runtime.json"
_COPY_BUFFER_BYTES = 1024 * 1024


def file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256，避免把大型运行时归档整体读入内存。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(_COPY_BUFFER_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise RuntimeInstallationError(
            f"无法校验运行时归档 {path}：{error}"
        ) from error
    return digest.hexdigest()


def install_runtime_atomically(
    spec: RuntimeSpec,
    install_directory: Path,
) -> None:
    """先安装到同盘临时目录，再原子发布完整运行时。"""

    parent = install_directory.parent
    temporary_directory = parent / (
        f".{install_directory.name}.{uuid.uuid4().hex}.tmp"
    )
    backup_directory: Path | None = None
    published = False
    try:
        parent.mkdir(parents=True, exist_ok=True)
        temporary_directory.mkdir()
        _extract_runtime_archive(spec, temporary_directory)

        temporary_executable = temporary_directory / spec.executable
        if not temporary_executable.is_file():
            raise RuntimeInstallationError(
                f"{spec.name.value} 运行时归档缺少可执行文件："
                f"{spec.executable.as_posix()}"
            )

        marker = {
            "name": spec.name.value,
            "version": spec.version,
            "platform": spec.platform,
            "executable": spec.executable.as_posix(),
            "sha256": spec.sha256,
        }
        (temporary_directory / RUNTIME_READY_MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if install_directory.exists():
            # ensure() 仅在原目录未通过完成标记验证时进入安装。先保留旧目录，
            # 新目录发布成功后再删除，使损坏或中断安装能够自动修复。
            backup_directory = parent / (
                f".{install_directory.name}.{uuid.uuid4().hex}.old"
            )
            os.replace(install_directory, backup_directory)
        os.replace(temporary_directory, install_directory)
        published = True
    except (RuntimeConfigurationError, RuntimeInstallationError):
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise RuntimeInstallationError(
            f"无法安装 {spec.name.value} 托管运行时：{error}"
        ) from error
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        if (
            not published
            and backup_directory is not None
            and backup_directory.exists()
            and not install_directory.exists()
        ):
            try:
                os.replace(backup_directory, install_directory)
            except OSError:
                # 主异常更有诊断价值；保留 .old 目录供人工恢复。
                pass
        if (
            published
            and backup_directory is not None
            and backup_directory.exists()
        ):
            shutil.rmtree(backup_directory, ignore_errors=True)


def _extract_runtime_archive(spec: RuntimeSpec, destination: Path) -> None:
    """根据清单安全解压运行时，并拒绝越界路径和符号链接。"""

    if spec.archive_format.casefold() != "zip":
        raise RuntimeConfigurationError(
            f"暂不支持运行时归档格式：{spec.archive_format}"
        )

    expected_root = PurePosixPath(spec.archive_root)
    with zipfile.ZipFile(spec.archive_path, mode="r") as archive:
        for member in archive.infolist():
            member_path = PurePosixPath(member.filename.replace("\\", "/"))
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or not member_path.parts
                or any(
                    "\x00" in part or ":" in part
                    for part in member_path.parts
                )
            ):
                raise RuntimeInstallationError(
                    f"运行时归档包含越界路径：{member.filename}"
                )
            # 某些上游包还包含签名和描述文件，只提取清单声明的运行时根目录。
            if member_path.parts[0] != expected_root.name:
                continue
            relative_parts = member_path.parts[1:]
            if not relative_parts:
                continue
            target = destination.joinpath(*relative_parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            unix_mode = member.external_attr >> 16
            if unix_mode and (unix_mode & 0o170000) == 0o120000:
                raise RuntimeInstallationError(
                    f"运行时归档包含不允许的符号链接：{member.filename}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, mode="r") as source:
                _copy_archive_file(source, target)


def _copy_archive_file(source: BinaryIO, target: Path) -> None:
    """将单个归档成员流式写入临时运行时目录。"""

    with target.open("wb") as output:
        shutil.copyfileobj(source, output, length=_COPY_BUFFER_BYTES)
