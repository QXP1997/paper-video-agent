# -*- coding: utf-8 -*-
"""QHarness 托管运行时使用的领域对象。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypeAlias

from qharness.exception import RuntimeConfigurationError


class RuntimeName(StrEnum):
    """QHarness 能够管理的运行时稳定名称。"""

    PYTHON = "python"
    NODE = "node"


class RuntimePreparationPhase(StrEnum):
    """托管运行时准备过程中的稳定阶段名称。"""

    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    INSTALLING = "installing"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    """描述某个平台上一份可校验、可安装的托管运行时。"""

    # 运行时的稳定逻辑名称，例如 python 或 node。
    name: RuntimeName

    # 上游运行时的精确版本，用于目录隔离和升级判断。
    version: str

    # QHarness 使用的平台标识，例如 windows-x64。
    platform: str

    # 随包提供或未来下载到缓存中的原始归档文件。
    archive_path: Path

    # 当前归档格式；第一版支持 ZIP 兼容格式，包括 Python 的 nupkg。
    archive_format: str

    # 归档中真正运行时文件所在的顶层目录。
    archive_root: str

    # 运行时安装完成后，相对于安装目录的可执行文件路径。
    executable: Path

    # 运行时在 managed 根目录中的稳定安装目录名称。
    install_directory: str

    # 原始归档的小写 SHA-256，用于安装前供应链完整性校验。
    sha256: str

    # 归档未随包提供时使用的固定 HTTPS 下载地址；None 表示不允许下载。
    download_url: str | None = None

    # 上游归档的精确字节数；下载型运行时必须提供，用于限制异常响应体。
    archive_size_bytes: int | None = None

    def __post_init__(self) -> None:
        """拒绝会造成路径越界或无法验证的运行时清单。"""

        text_fields = {
            "version": self.version,
            "platform": self.platform,
            "archive_format": self.archive_format,
            "archive_root": self.archive_root,
            "install_directory": self.install_directory,
        }
        for field_name, value in text_fields.items():
            if not isinstance(value, str) or not value.strip():
                raise RuntimeConfigurationError(
                    f"运行时清单字段 {field_name} 必须是非空字符串。"
                )

        executable = PurePosixPath(self.executable.as_posix())
        if (
            self.executable.is_absolute()
            or self.executable.drive
            or executable.is_absolute()
            or executable.name in {"", ".", ".."}
            or ".." in executable.parts
        ):
            raise RuntimeConfigurationError(
                f"运行时可执行文件路径不合法：{self.executable}"
            )

        install_directory = PurePosixPath(self.install_directory)
        if (
            len(install_directory.parts) != 1
            or install_directory.name in {"", ".", ".."}
        ):
            raise RuntimeConfigurationError(
                f"运行时安装目录名称不合法：{self.install_directory}"
            )

        archive_root = PurePosixPath(self.archive_root)
        if (
            len(archive_root.parts) != 1
            or archive_root.name in {"", ".", ".."}
        ):
            raise RuntimeConfigurationError(
                f"运行时归档根目录不合法：{self.archive_root}"
            )

        normalized_sha256 = self.sha256.casefold()
        if len(normalized_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in normalized_sha256
        ):
            raise RuntimeConfigurationError(
                "运行时清单 sha256 必须是 64 位十六进制文本。"
            )
        if self.download_url is not None and not self.download_url.startswith(
            "https://"
        ):
            raise RuntimeConfigurationError(
                "运行时清单 download_url 必须使用 HTTPS。"
            )
        if self.download_url is not None and (
            isinstance(self.archive_size_bytes, bool)
            or not isinstance(self.archive_size_bytes, int)
            or self.archive_size_bytes <= 0
        ):
            raise RuntimeConfigurationError(
                "下载型运行时必须提供大于 0 的 archive_size_bytes。"
            )


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """表示一份托管运行时当前是否已经可以执行。"""

    # 被检查的运行时逻辑名称。
    name: RuntimeName

    # 当前平台是否存在这份运行时的有效清单和安装结果。
    available: bool

    # 便于客户端展示的状态说明或失败原因。
    message: str

    # 清单声明的运行时版本；清单不可用时为 None。
    version: str | None = None

    # 验证通过后的可执行文件绝对路径；未准备好时为 None。
    executable_path: Path | None = None

    # 是否仍需要执行下载或安装；运行时尚未可执行时为 True。
    install_required: bool = False

    # 是否需要先从固定上游地址下载归档。
    download_required: bool = False


@dataclass(frozen=True, slots=True)
class RuntimePreparationProgress:
    """传递给客户端进度条或日志系统的运行时准备进度。"""

    # 正在准备的运行时名称。
    name: RuntimeName

    # 下载、校验、安装或完成阶段。
    phase: RuntimePreparationPhase

    # 当前阶段已经处理的字节数；不适用时为 None。
    completed_bytes: int | None = None

    # 当前阶段预计总字节数；上游没有提供时为 None。
    total_bytes: int | None = None

    # 便于直接展示给用户的中文状态说明。
    message: str = ""


RuntimeProgressCallback: TypeAlias = Callable[
    [RuntimePreparationProgress],
    None,
]
