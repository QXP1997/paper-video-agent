# -*- coding: utf-8 -*-
"""Anthropic SRT npm 包的本地托管安装器。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from qharness.exception import SandboxInstallationError


_PACKAGE_NAME = "@anthropic-ai/sandbox-runtime"
_INSTALL_TIMEOUT_SECONDS = 300.0
_READY_MARKER_NAME = ".qharness-srt-ready.json"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SrtPackageStatus:
    """描述一份托管 SRT npm 包当前是否完整可用。"""

    # npm 包根目录，也就是包含 package.json 和 dist/cli.js 的目录。
    package_path: Path

    # 包身份、精确版本、CLI 和完成标记是否均已验证通过。
    available: bool

    # 面向日志或客户端的中文状态说明。
    message: str

    # 实际读取到的 SRT 版本；包不完整时为 None。
    version: str | None = None


class SrtPackageManager:
    """用托管 Node 和 npm 锁文件安装固定版本的 SRT。"""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        version: str,
        resource_directory: str | Path | None = None,
    ) -> None:
        """保存安装根目录、精确版本和随应用发布的 npm 锁文件位置。"""

        if not version or any(character in version for character in "/\\\x00"):
            raise SandboxInstallationError("SRT 托管版本号不合法。")
        self._runtime_root = Path(runtime_root).expanduser().resolve(
            strict=False
        )
        self._version = version
        self._resource_directory = (
            Path(resource_directory).expanduser().resolve(strict=False)
            if resource_directory is not None
            else Path(__file__).resolve().parents[1] / "resources" / "srt"
        )
        self._install_lock = threading.Lock()

    @property
    def package_path(self) -> Path:
        """返回该固定版本安装完成后的 npm 包根目录。"""

        return (
            self._installation_directory
            / "node_modules"
            / "@anthropic-ai"
            / "sandbox-runtime"
        )

    @property
    def _installation_directory(self) -> Path:
        """返回由 QHarness 独占管理的 SRT 版本目录。"""

        return self._runtime_root / "srt" / "packages" / f"srt-{self._version}"

    def check_status(self) -> SrtPackageStatus:
        """只读检查完成标记、包身份、版本和 CLI 文件。"""

        package_path = self.package_path
        metadata_path = package_path / "package.json"
        cli_path = package_path / "dist" / "cli.js"
        marker_path = self._installation_directory / _READY_MARKER_NAME
        if not metadata_path.is_file() or not cli_path.is_file():
            return SrtPackageStatus(
                package_path=package_path,
                available=False,
                message=f"SRT {self._version} 尚未下载。",
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return SrtPackageStatus(
                package_path=package_path,
                available=False,
                message=f"SRT 安装记录无法读取：{error}",
            )
        version = metadata.get("version")
        ready = (
            metadata.get("name") == _PACKAGE_NAME
            and version == self._version
            and isinstance(marker, dict)
            and marker.get("package") == _PACKAGE_NAME
            and marker.get("version") == self._version
        )
        return SrtPackageStatus(
            package_path=package_path,
            available=ready,
            version=version if isinstance(version, str) else None,
            message=(
                f"SRT {self._version} 已准备完成。"
                if ready
                else "SRT 安装目录不完整或版本不匹配，需要重新安装。"
            ),
        )

    def ensure(self, node_path: Path) -> Path:
        """必要时通过固定锁文件安装 SRT，并返回 npm 包根目录。"""

        with self._install_lock:
            status = self.check_status()
            if status.available:
                _LOGGER.debug("复用托管 SRT：path=%s", status.package_path)
                return status.package_path

            npm_cli_path = _find_npm_cli(node_path)
            package_json = self._resource_directory / "package.json"
            package_lock = self._resource_directory / "package-lock.json"
            if not package_json.is_file() or not package_lock.is_file():
                raise SandboxInstallationError(
                    f"QHarness 缺少 SRT 安装清单或锁文件：{self._resource_directory}"
                )

            destination = self._installation_directory
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.parent / (
                f".{destination.name}.{uuid.uuid4().hex}.installing"
            )
            backup: Path | None = None
            published = False
            try:
                temporary.mkdir()
                shutil.copy2(package_json, temporary / "package.json")
                shutil.copy2(package_lock, temporary / "package-lock.json")
                _LOGGER.info(
                    "开始下载并安装托管 SRT：version=%s，path=%s",
                    self._version,
                    destination,
                )
                completed = subprocess.run(
                    (
                        str(node_path),
                        str(npm_cli_path),
                        "ci",
                        "--prefix",
                        str(temporary),
                        "--omit=dev",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                        "--registry",
                        "https://registry.npmjs.org",
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=_INSTALL_TIMEOUT_SECONDS,
                    check=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    ),
                )
                if completed.stdout.strip():
                    _LOGGER.info("npm 安装输出：\n%s", completed.stdout.strip())
                if completed.returncode != 0:
                    detail = completed.stderr.strip() or completed.stdout.strip()
                    raise SandboxInstallationError(
                        "SRT npm 安装失败："
                        f"{detail or f'退出码 {completed.returncode}'}"
                    )
                _validate_installed_package(
                    temporary,
                    expected_version=self._version,
                )
                (temporary / _READY_MARKER_NAME).write_text(
                    json.dumps(
                        {"package": _PACKAGE_NAME, "version": self._version},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                if destination.exists():
                    backup = destination.parent / (
                        f".{destination.name}.{uuid.uuid4().hex}.old"
                    )
                    os.replace(destination, backup)
                os.replace(temporary, destination)
                published = True
                _LOGGER.info(
                    "托管 SRT 安装完成：version=%s，path=%s",
                    self._version,
                    self.package_path,
                )
                return self.package_path
            except subprocess.TimeoutExpired as error:
                raise SandboxInstallationError(
                    f"SRT npm 安装超过 {_INSTALL_TIMEOUT_SECONDS:.0f} 秒。"
                ) from error
            except SandboxInstallationError:
                raise
            except OSError as error:
                raise SandboxInstallationError(
                    f"SRT 托管安装失败：{error}"
                ) from error
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
                # 发布失败时恢复旧的完整安装；发布成功后才清理旧目录。
                if (
                    not published
                    and backup is not None
                    and backup.exists()
                    and not destination.exists()
                ):
                    try:
                        os.replace(backup, destination)
                    except OSError:
                        _LOGGER.exception("无法恢复上一份托管 SRT 安装：%s", backup)
                if published and backup is not None and backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)


def _find_npm_cli(node_path: Path) -> Path:
    """从 Node 安装目录寻找 npm 的跨平台 JavaScript 入口。"""

    candidates = (
        node_path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
        node_path.parent / ".." / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
    )
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved.is_file():
            return resolved
    raise SandboxInstallationError(
        "所选 Node.js 没有附带 npm，无法自动安装 SRT。"
        f"Node 路径：{node_path}"
    )


def _validate_installed_package(
    installation_directory: Path,
    *,
    expected_version: str,
) -> None:
    """在发布安装目录前验证 npm 确实得到预期包和 CLI。"""

    package_path = (
        installation_directory
        / "node_modules"
        / "@anthropic-ai"
        / "sandbox-runtime"
    )
    metadata_path = package_path / "package.json"
    cli_path = package_path / "dist" / "cli.js"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SandboxInstallationError(
            "npm 返回成功，但 SRT package.json 无法读取。"
        ) from error
    if (
        metadata.get("name") != _PACKAGE_NAME
        or metadata.get("version") != expected_version
        or not cli_path.is_file()
    ):
        raise SandboxInstallationError(
            "npm 返回成功，但安装结果不是锁定版本的 Anthropic SRT。"
        )
