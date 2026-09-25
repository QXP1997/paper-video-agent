# -*- coding: utf-8 -*-
"""QHarness 托管运行时的发现、校验、安装和可执行文件解析。"""

from __future__ import annotations

import json
import logging
import platform
import sys
import threading
from pathlib import Path
from typing import Any

from qharness.exception import (
    RuntimeConfigurationError,
    RuntimeInstallationError,
    RuntimeManagerError,
    RuntimeUnavailableError,
)
from qharness.runtime.archive import (
    RUNTIME_READY_MARKER_NAME,
    file_sha256,
    install_runtime_atomically,
)
from qharness.runtime.download import download_runtime_archive
from qharness.runtime.models import (
    RuntimeName,
    RuntimePreparationPhase,
    RuntimePreparationProgress,
    RuntimeProgressCallback,
    RuntimeSpec,
    RuntimeStatus,
)


_RUNTIME_MANIFEST_NAME = "runtime.json"
_RUNTIME_RESOURCE_DIRECTORIES = {
    RuntimeName.PYTHON: "python",
    RuntimeName.NODE: "node",
}
_EXECUTABLE_ALIASES = {
    "python": RuntimeName.PYTHON,
    "python.exe": RuntimeName.PYTHON,
    "python3": RuntimeName.PYTHON,
    "python3.exe": RuntimeName.PYTHON,
    "node": RuntimeName.NODE,
    "node.exe": RuntimeName.NODE,
}
_PLATFORM_TAGS: dict[tuple[str, str], str] = {
    ("win32", "amd64"): "windows-x64",
    ("win32", "x86_64"): "windows-x64",
}
_LOGGER = logging.getLogger(__name__)


class RuntimeManager:
    """管理当前 QHarness 实例拥有的私有语言运行时。"""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        resource_root: str | Path | None = None,
    ) -> None:
        """保存托管安装根目录和随包资源根目录。

        ``runtime_root`` 当前通常为 ``.qharness/runtime``。``resource_root``
        主要用于打包验证和测试；不传时自动定位 qharness/resources。
        """

        self._runtime_root = Path(runtime_root).expanduser().resolve(
            strict=False
        )
        package_root = Path(__file__).resolve().parents[1]
        self._resource_root = (
            Path(resource_root).expanduser().resolve(strict=False)
            if resource_root is not None
            else package_root / "resources"
        )
        # ensure() 会在线程池中运行；同一个 Manager 内只允许一个安装发布流程。
        self._install_lock = threading.Lock()

    @property
    def runtime_root(self) -> Path:
        """返回 QHarness 托管运行时的安装根目录。"""

        return self._runtime_root

    def resolve_executable(
        self,
        executable: str,
        *,
        progress_callback: RuntimeProgressCallback | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        """将受支持的裸逻辑名称解析成托管运行时绝对路径。

        当前接管 Python 和 Node 别名。显式路径和其他命令保持原样；托管
        运行时缺失时会准备固定版本，不会悄悄回退到系统 PATH。
        """

        if "/" in executable or "\\" in executable:
            return executable
        runtime_name = _EXECUTABLE_ALIASES.get(executable.casefold())
        if runtime_name is None:
            return executable
        return str(
            self.ensure(
                runtime_name,
                progress_callback=progress_callback,
                cancellation_event=cancellation_event,
            )
        )

    def ensure(
        self,
        name: RuntimeName | str,
        *,
        progress_callback: RuntimeProgressCallback | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> Path:
        """确保托管运行时完整安装，并返回可执行文件绝对路径。"""

        runtime_name = _normalize_runtime_name(name)
        with self._install_lock:
            spec = self._load_spec(runtime_name)
            install_directory = self._install_directory(spec)
            executable_path = install_directory / spec.executable
            if self._runtime_is_ready(
                spec,
                install_directory,
                executable_path,
            ):
                _LOGGER.debug(
                    "复用已安装的托管运行时：name=%s，version=%s，path=%s",
                    runtime_name.value,
                    spec.version,
                    executable_path,
                )
                return executable_path

            if not spec.archive_path.is_file():
                download_runtime_archive(
                    spec,
                    spec.archive_path,
                    progress_callback=progress_callback,
                    cancellation_event=cancellation_event,
                )
            _emit_progress(
                spec,
                RuntimePreparationPhase.VERIFYING,
                progress_callback,
                message=f"正在校验 {runtime_name.value} 运行时归档。",
            )
            actual_sha256 = file_sha256(spec.archive_path)
            if actual_sha256 != spec.sha256:
                # 只清理 RuntimeManager 自己下载到缓存的损坏文件；随安装包提供
                # 的资源属于应用文件，保留现场并直接报错。
                if spec.download_url is not None:
                    try:
                        spec.archive_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise RuntimeInstallationError(
                    f"{runtime_name.value} 运行时归档校验失败："
                    f"期望 SHA-256 {spec.sha256}，实际为 {actual_sha256}。"
                )

            _LOGGER.info(
                "开始安装托管运行时：name=%s，version=%s，platform=%s",
                runtime_name.value,
                spec.version,
                spec.platform,
            )
            _emit_progress(
                spec,
                RuntimePreparationPhase.INSTALLING,
                progress_callback,
                message=f"正在安装 {runtime_name.value} {spec.version}。",
            )
            install_runtime_atomically(spec, install_directory)
            if not self._runtime_is_ready(
                spec,
                install_directory,
                executable_path,
            ):
                raise RuntimeInstallationError(
                    f"{runtime_name.value} 托管运行时安装后仍不可用："
                    f"{executable_path}"
                )
            _LOGGER.info(
                "托管运行时安装完成：name=%s，version=%s，path=%s",
                runtime_name.value,
                spec.version,
                executable_path,
            )
            _emit_progress(
                spec,
                RuntimePreparationPhase.READY,
                progress_callback,
                message=f"{runtime_name.value} {spec.version} 已准备完成。",
            )
            return executable_path

    def check_status(self, name: RuntimeName | str) -> RuntimeStatus:
        """只读检查运行时清单和既有安装，不触发摘要计算或解压。"""

        runtime_name = _normalize_runtime_name(name)
        try:
            spec = self._load_spec(runtime_name)
            install_directory = self._install_directory(spec)
            executable_path = install_directory / spec.executable
            if self._runtime_is_ready(
                spec,
                install_directory,
                executable_path,
            ):
                return RuntimeStatus(
                    name=runtime_name,
                    available=True,
                    version=spec.version,
                    executable_path=executable_path,
                    message=f"{runtime_name.value} 托管运行时已经准备完成。",
                )
            if not spec.archive_path.is_file():
                if spec.download_url is None:
                    raise RuntimeUnavailableError(
                        f"{runtime_name.value} 托管运行时资源包不存在："
                        f"{spec.archive_path}"
                    )
                return RuntimeStatus(
                    name=runtime_name,
                    available=False,
                    version=spec.version,
                    install_required=True,
                    download_required=True,
                    message=f"{runtime_name.value} 托管运行时需要下载。",
                )
            return RuntimeStatus(
                name=runtime_name,
                available=False,
                version=spec.version,
                install_required=True,
                message=f"{runtime_name.value} 托管运行时尚未安装。",
            )
        except RuntimeManagerError as error:
            return RuntimeStatus(
                name=runtime_name,
                available=False,
                message=str(error),
            )

    def _load_spec(self, name: RuntimeName) -> RuntimeSpec:
        """读取并严格校验当前平台对应的托管运行时清单。"""

        resource_name = _RUNTIME_RESOURCE_DIRECTORIES.get(name)
        if resource_name is None:
            raise RuntimeUnavailableError(
                f"{name.value} 托管运行时尚未提供。"
            )
        platform_tag = _current_platform_tag()
        resource_directory = (
            self._resource_root
            / resource_name
            / "bundles"
            / platform_tag
        )
        manifest_path = resource_directory / _RUNTIME_MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeUnavailableError(
                f"{name.value} 托管运行时清单不存在：{manifest_path}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeConfigurationError(
                f"无法读取 {name.value} 运行时清单：{error}"
            ) from error
        if not isinstance(manifest, dict):
            raise RuntimeConfigurationError(
                f"{name.value} 运行时清单必须是 JSON 对象。"
            )

        manifest_name = _required_manifest_text(manifest, "name")
        manifest_platform = _required_manifest_text(manifest, "platform")
        if manifest_name != name.value:
            raise RuntimeConfigurationError(
                f"运行时清单名称不匹配：期望 {name.value}，"
                f"实际 {manifest_name}。"
            )
        if manifest_platform != platform_tag:
            raise RuntimeConfigurationError(
                f"运行时清单平台不匹配：期望 {platform_tag}，"
                f"实际 {manifest_platform}。"
            )

        archive_name = _required_manifest_text(manifest, "archive")
        bundled_archive = resource_directory / archive_name
        download_url = _optional_manifest_text(manifest, "download_url")
        archive_path = (
            bundled_archive
            if bundled_archive.is_file() or download_url is None
            else (
                self._runtime_root
                / "downloads"
                / name.value
                / _required_manifest_text(manifest, "version")
                / archive_name
            )
        )
        return RuntimeSpec(
            name=name,
            version=_required_manifest_text(manifest, "version"),
            platform=manifest_platform,
            archive_path=archive_path,
            archive_format=_required_manifest_text(
                manifest,
                "archive_format",
            ),
            archive_root=_required_manifest_text(manifest, "archive_root"),
            executable=Path(
                _required_manifest_text(manifest, "executable")
            ),
            install_directory=_required_manifest_text(
                manifest,
                "install_directory",
            ),
            sha256=_required_manifest_text(manifest, "sha256").casefold(),
            download_url=download_url,
            archive_size_bytes=_optional_manifest_positive_int(
                manifest,
                "archive_size_bytes",
            ),
        )

    def _install_directory(self, spec: RuntimeSpec) -> Path:
        """返回运行时名称下按版本和平台隔离的最终安装目录。"""

        return self._runtime_root / spec.name.value / spec.install_directory

    @staticmethod
    def _runtime_is_ready(
        spec: RuntimeSpec,
        install_directory: Path,
        executable_path: Path,
    ) -> bool:
        """通过完成标记确认目录不是中断安装留下的半成品。"""

        marker_path = install_directory / RUNTIME_READY_MARKER_NAME
        if not executable_path.is_file() or not marker_path.is_file():
            return False
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        # 兼容重构前只记录 version 和 sha256 的完成标记，避免重复解压。
        return (
            isinstance(marker, dict)
            and marker.get("version") == spec.version
            and marker.get("sha256") == spec.sha256
        )


def _normalize_runtime_name(name: RuntimeName | str) -> RuntimeName:
    """将外部字符串转换为稳定枚举，并给出明确的未知运行时错误。"""

    if isinstance(name, RuntimeName):
        return name
    try:
        return RuntimeName(name.casefold())
    except (AttributeError, ValueError) as error:
        raise RuntimeUnavailableError(f"未知托管运行时：{name}") from error


def _current_platform_tag() -> str:
    """返回当前操作系统和 CPU 架构对应的 QHarness 平台标识。"""

    machine = platform.machine().casefold()
    platform_tag = _PLATFORM_TAGS.get((sys.platform, machine))
    if platform_tag is None:
        raise RuntimeUnavailableError(
            "当前平台暂未提供托管运行时："
            f"system={sys.platform}，machine={machine}"
        )
    return platform_tag


def _required_manifest_text(manifest: dict[str, Any], name: str) -> str:
    """读取运行时清单中的必需非空字符串。"""

    value = manifest.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigurationError(
            f"运行时清单字段 {name} 必须是非空字符串。"
        )
    return value.strip()


def _optional_manifest_text(
    manifest: dict[str, Any],
    name: str,
) -> str | None:
    """读取运行时清单中的可选非空字符串。"""

    if name not in manifest:
        return None
    return _required_manifest_text(manifest, name)


def _optional_manifest_positive_int(
    manifest: dict[str, Any],
    name: str,
) -> int | None:
    """读取运行时清单中的可选正整数。"""

    if name not in manifest:
        return None
    value = manifest[name]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeConfigurationError(
            f"运行时清单字段 {name} 必须是大于 0 的整数。"
        )
    return value


def _emit_progress(
    spec: RuntimeSpec,
    phase: RuntimePreparationPhase,
    callback: RuntimeProgressCallback | None,
    *,
    message: str,
) -> None:
    """发送不涉及字节计数的准备阶段进度。"""

    if callback is None:
        return
    callback(
        RuntimePreparationProgress(
            name=spec.name,
            phase=phase,
            message=message,
        )
    )
