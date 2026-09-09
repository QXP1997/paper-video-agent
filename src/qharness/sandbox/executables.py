# -*- coding: utf-8 -*-
"""沙箱目标程序的逻辑名称解析与内置运行时安装。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from qharness.exception import SandboxConfigurationError


_PYTHON_ALIASES = frozenset({"python", "python.exe", "python3", "python3.exe"})
_PYTHON_RESOURCE_DIRECTORIES: dict[tuple[str, str], str] = {
    ("win32", "amd64"): "windows-x64",
    ("win32", "x86_64"): "windows-x64",
}
_PYTHON_EXECUTABLE_NAMES = {"win32": "python.exe"}
_PYTHON_MANIFEST_NAME = "runtime.json"
_PYTHON_READY_MARKER_NAME = ".qharness-runtime.json"
_COPY_BUFFER_BYTES = 1024 * 1024


def resolve_sandbox_executable(
    executable: str,
    runtime_root: Path,
) -> str:
    """把 Agent 使用的逻辑程序名解析成实际可执行文件路径。

    当前只接管不含目录的 ``python`` 系列名称。显式路径和其他命令保持
    原样，因此调用方仍可执行 git、node 等系统命令，也可以明确指定另一份
    Python。内置运行时缺失时直接报错，不会悄悄退回用户 PATH 中的 Python。
    """

    if not _is_python_alias(executable):
        return executable
    return str(resolve_bundled_python_path(runtime_root))


def resolve_bundled_python_path(runtime_root: Path) -> Path:
    """准备当前平台的内置 Python，并返回解释器的绝对路径。"""

    resource_directory = _python_resource_directory()
    manifest = _load_manifest(resource_directory)
    version = _required_manifest_text(manifest, "version")
    archive_name = _required_manifest_text(manifest, "archive")
    expected_sha256 = _required_manifest_text(manifest, "sha256").casefold()
    archive_root = _required_manifest_text(manifest, "archive_root")
    executable_name = _PYTHON_EXECUTABLE_NAMES.get(sys.platform)
    if executable_name is None:
        raise SandboxConfigurationError(
            f"当前操作系统暂未配置内置 Python 可执行文件：{sys.platform}"
        )

    archive_path = resource_directory / archive_name
    if not archive_path.is_file():
        raise SandboxConfigurationError(
            f"内置 Python 资源包不存在：{archive_path}"
        )

    platform_directory = resource_directory.name
    install_directory = (
        runtime_root.expanduser().resolve(strict=False)
        / f"cpython-{version}-{platform_directory}"
    )
    executable_path = install_directory / executable_name
    if _runtime_is_ready(
        install_directory,
        executable_path,
        expected_sha256,
    ):
        return executable_path

    actual_sha256 = _file_sha256(archive_path)
    if actual_sha256 != expected_sha256:
        raise SandboxConfigurationError(
            "内置 Python 资源包校验失败："
            f"期望 SHA-256 {expected_sha256}，实际为 {actual_sha256}。"
        )

    _install_runtime_atomically(
        archive_path=archive_path,
        archive_root=archive_root,
        install_directory=install_directory,
        executable_name=executable_name,
        version=version,
        sha256=actual_sha256,
    )
    if not _runtime_is_ready(
        install_directory,
        executable_path,
        expected_sha256,
    ):
        raise SandboxConfigurationError(
            f"内置 Python 解压完成后仍不可用：{executable_path}"
        )
    return executable_path


def _is_python_alias(executable: str) -> bool:
    """仅识别裸命令名，避免替换调用方明确给出的 Python 路径。"""

    if "/" in executable or "\\" in executable:
        return False
    return executable.casefold() in _PYTHON_ALIASES


def _python_resource_directory() -> Path:
    """返回当前操作系统与处理器架构对应的 Python 资源目录。"""

    machine = platform.machine().casefold()
    directory_name = _PYTHON_RESOURCE_DIRECTORIES.get((sys.platform, machine))
    if directory_name is None:
        raise SandboxConfigurationError(
            "当前平台没有随 QHarness 分发的 Python 运行时："
            f"system={sys.platform}，machine={machine}"
        )
    package_root = Path(__file__).resolve().parents[1]
    return (
        package_root
        / "resources"
        / "python"
        / "bundles"
        / directory_name
    )


def _load_manifest(resource_directory: Path) -> dict[str, Any]:
    """读取内置 Python 的版本、归档名称和供应链摘要。"""

    manifest_path = resource_directory / _PYTHON_MANIFEST_NAME
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SandboxConfigurationError(
            f"内置 Python 清单不存在：{manifest_path}"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise SandboxConfigurationError(
            f"无法读取内置 Python 清单：{error}"
        ) from error
    if not isinstance(document, dict):
        raise SandboxConfigurationError("内置 Python 清单必须是 JSON 对象。")
    return document


def _required_manifest_text(manifest: dict[str, Any], name: str) -> str:
    """读取清单中的必需非空字符串。"""

    value = manifest.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SandboxConfigurationError(
            f"内置 Python 清单字段 {name} 必须是非空字符串。"
        )
    return value.strip()


def _runtime_is_ready(
    install_directory: Path,
    executable_path: Path,
    expected_sha256: str,
) -> bool:
    """通过完成标记确认目录不是一次中断后留下的半成品。"""

    marker_path = install_directory / _PYTHON_READY_MARKER_NAME
    if not executable_path.is_file() or not marker_path.is_file():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(marker, dict)
        and marker.get("sha256") == expected_sha256
    )


def _file_sha256(path: Path) -> str:
    """以流式方式计算大文件摘要，避免把运行时归档整体读入内存。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(_COPY_BUFFER_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise SandboxConfigurationError(
            f"无法校验内置 Python 资源包：{error}"
        ) from error
    return digest.hexdigest()


def _install_runtime_atomically(
    *,
    archive_path: Path,
    archive_root: str,
    install_directory: Path,
    executable_name: str,
    version: str,
    sha256: str,
) -> None:
    """先解压到同盘临时目录，再原子发布完整的 Python 运行时。"""

    parent = install_directory.parent
    temporary_directory = parent / (
        f".{install_directory.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        parent.mkdir(parents=True, exist_ok=True)
        temporary_directory.mkdir()
        _extract_runtime_archive(
            archive_path,
            archive_root,
            temporary_directory,
        )
        temporary_executable = temporary_directory / executable_name
        if not temporary_executable.is_file():
            raise SandboxConfigurationError(
                f"Python 资源包缺少可执行文件：{executable_name}"
            )
        marker = {
            "version": version,
            "sha256": sha256,
        }
        (temporary_directory / _PYTHON_READY_MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.replace(temporary_directory, install_directory)
        except FileExistsError:
            # 另一个并发请求可能已经完成相同版本安装，交由调用方复核标记。
            pass
    except SandboxConfigurationError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise SandboxConfigurationError(
            f"无法安装内置 Python 运行时：{error}"
        ) from error
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)


def _extract_runtime_archive(
    archive_path: Path,
    archive_root: str,
    destination: Path,
) -> None:
    """安全解压 NuGet 包中的运行时根目录，拒绝越界路径和链接。"""

    expected_root = PurePosixPath(archive_root)
    if len(expected_root.parts) != 1 or expected_root.name in {"", ".", ".."}:
        raise SandboxConfigurationError("Python 资源包根目录配置不合法。")

    with zipfile.ZipFile(archive_path, mode="r") as archive:
        for member in archive.infolist():
            member_path = PurePosixPath(member.filename.replace("\\", "/"))
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or not member_path.parts
            ):
                raise SandboxConfigurationError(
                    f"Python 资源包包含越界路径：{member.filename}"
                )
            # NuGet 包还包含签名和包描述，只提取 tools 下的 CPython 文件。
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
                raise SandboxConfigurationError(
                    f"Python 资源包包含不允许的链接：{member.filename}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, mode="r") as source:
                _copy_archive_file(source, target)


def _copy_archive_file(source: BinaryIO, target: Path) -> None:
    """把单个归档成员流式写入临时运行时目录。"""

    with target.open("wb") as output:
        shutil.copyfileobj(source, output, length=_COPY_BUFFER_BYTES)
