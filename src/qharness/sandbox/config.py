# -*- coding: utf-8 -*-
"""沙箱 TOML 配置和 SRT 策略定义。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qharness.exception import SandboxConfigurationError


_SANDBOX_KEYS = {
    "backend",
    "timeout_seconds",
    "max_stdout_chars",
    "max_stderr_chars",
    "runtime_directory",
    "srt",
    "filesystem",
    "network",
}
_SRT_KEYS = {"node_path", "package_path", "expected_version", "debug"}
_FILESYSTEM_KEYS = {"allow_read", "deny_read", "allow_write", "deny_write"}
_NETWORK_KEYS = {"allowed_domains", "denied_domains", "allow_local_binding"}


@dataclass(frozen=True, slots=True)
class SrtRuntimeConfig:
    """定位并校验 Anthropic Sandbox Runtime npm 包。"""

    # Node.js 可执行文件路径；None 表示启动时通过系统 PATH 自动查找 node。
    node_path: Path | None

    # @anthropic-ai/sandbox-runtime npm 包根目录，其中必须包含 package.json。
    package_path: Path

    # 要求使用的精确 SRT 版本；None 表示不限制版本，不建议生产环境使用。
    expected_version: str | None = None

    # 是否开启 SRT 自身的调试日志，排查策略或启动问题时使用。
    debug: bool = False

    @property
    def cli_path(self) -> Path:
        """返回 npm 包声明的 CLI 默认位置。"""

        return self.package_path / "dist" / "cli.js"


@dataclass(frozen=True, slots=True)
class SandboxFilesystemConfig:
    """定义 SRT 文件读取和写入策略。"""

    # 在 deny_read 的大范围限制中重新允许读取的路径，默认允许当前工作区。
    allow_read: tuple[str, ...] = (".",)

    # 禁止目标进程读取的路径，例如用户主目录、API Key 配置和 .env 文件。
    deny_read: tuple[str, ...] = ()

    # 允许目标进程写入的路径；SRT 默认不允许写入其他位置。
    allow_write: tuple[str, ...] = (".",)

    # 即使位于 allow_write 内也禁止写入的更具体路径，优先级高于允许规则。
    deny_write: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SandboxNetworkConfig:
    """定义 SRT 网络域名白名单和本地监听策略。"""

    # 允许目标进程访问的域名白名单；空数组表示默认完全断网。
    allowed_domains: tuple[str, ...] = ()

    # 即使匹配允许规则也必须阻止的域名，适合声明更具体的例外。
    denied_domains: tuple[str, ...] = ()

    # 是否允许沙箱进程监听本机端口；普通脚本执行通常应保持 False。
    allow_local_binding: bool = False


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """QHarness 单机沙箱的完整动态配置。"""

    # 选择使用的沙箱实现名称；当前只支持 srt。
    backend: str

    # 单次执行未单独设置超时时使用的默认秒数。
    timeout_seconds: float

    # 单次执行未覆盖限制时，标准输出最多保留的默认字符数。
    max_stdout_chars: int

    # 单次执行未覆盖限制时，标准错误最多保留的默认字符数。
    max_stderr_chars: int

    # 保存动态生成的 SRT JSON 策略等临时运行文件的目录。
    runtime_directory: Path

    # SRT npm 包、Node.js 和固定版本等运行时配置。
    srt: SrtRuntimeConfig

    # 应用到沙箱目标进程及其所有子进程的文件系统策略。
    filesystem: SandboxFilesystemConfig

    # 应用到沙箱目标进程及其所有子进程的网络策略。
    network: SandboxNetworkConfig


def load_sandbox_config(config_path: str | Path) -> SandboxConfig:
    """从 TOML 文件动态读取沙箱配置，不使用环境变量覆盖。"""

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise SandboxConfigurationError(f"沙箱配置文件不存在：{path}")

    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise SandboxConfigurationError(f"沙箱配置文件格式错误：{error}") from error

    raw = document.get("sandbox")
    if not isinstance(raw, dict):
        raise SandboxConfigurationError("配置文件缺少 [sandbox] 节。")

    try:
        _reject_unknown_keys(raw, _SANDBOX_KEYS, "sandbox")
        srt_raw = _read_table(raw, "srt")
        filesystem_raw = _read_table(raw, "filesystem")
        network_raw = _read_table(raw, "network")
        _reject_unknown_keys(srt_raw, _SRT_KEYS, "sandbox.srt")
        _reject_unknown_keys(
            filesystem_raw,
            _FILESYSTEM_KEYS,
            "sandbox.filesystem",
        )
        _reject_unknown_keys(network_raw, _NETWORK_KEYS, "sandbox.network")

        backend = _read_string(raw, "backend", "srt", "sandbox")
        if backend != "srt":
            raise ValueError("sandbox.backend 当前只支持 srt。")

        package_text = _read_required_string(
            srt_raw,
            "package_path",
            "sandbox.srt",
        )
        node_text = _read_optional_string(srt_raw, "node_path", "sandbox.srt")
        expected_version = _read_optional_string(
            srt_raw,
            "expected_version",
            "sandbox.srt",
        )
        runtime_text = _read_string(
            raw,
            "runtime_directory",
            "../.qharness/runtime/sandbox",
            "sandbox",
        )
        return SandboxConfig(
            backend=backend,
            timeout_seconds=_read_positive_float(
                raw,
                "timeout_seconds",
                60.0,
                "sandbox",
            ),
            max_stdout_chars=_read_positive_int(
                raw,
                "max_stdout_chars",
                100_000,
                "sandbox",
            ),
            max_stderr_chars=_read_positive_int(
                raw,
                "max_stderr_chars",
                100_000,
                "sandbox",
            ),
            runtime_directory=_resolve_config_path(path, runtime_text),
            srt=SrtRuntimeConfig(
                node_path=(
                    _resolve_config_path(path, node_text)
                    if node_text is not None
                    else None
                ),
                package_path=_resolve_config_path(path, package_text),
                expected_version=expected_version,
                debug=_read_bool(srt_raw, "debug", False, "sandbox.srt"),
            ),
            filesystem=SandboxFilesystemConfig(
                allow_read=_read_string_list(
                    filesystem_raw,
                    "allow_read",
                    (".",),
                    "sandbox.filesystem",
                ),
                deny_read=_read_string_list(
                    filesystem_raw,
                    "deny_read",
                    (),
                    "sandbox.filesystem",
                ),
                allow_write=_read_string_list(
                    filesystem_raw,
                    "allow_write",
                    (".",),
                    "sandbox.filesystem",
                ),
                deny_write=_read_string_list(
                    filesystem_raw,
                    "deny_write",
                    (),
                    "sandbox.filesystem",
                ),
            ),
            network=SandboxNetworkConfig(
                allowed_domains=_read_string_list(
                    network_raw,
                    "allowed_domains",
                    (),
                    "sandbox.network",
                ),
                denied_domains=_read_string_list(
                    network_raw,
                    "denied_domains",
                    (),
                    "sandbox.network",
                ),
                allow_local_binding=_read_bool(
                    network_raw,
                    "allow_local_binding",
                    False,
                    "sandbox.network",
                ),
            ),
        )
    except (TypeError, ValueError) as error:
        raise SandboxConfigurationError(f"沙箱配置不合法：{error}") from error


def _resolve_config_path(config_path: Path, value: str) -> Path:
    """将相对路径固定解析为相对于配置文件所在目录。"""

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve(strict=False)


def _read_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    """读取必需的 TOML 子表。"""

    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"sandbox.{name} 必须是 TOML 表。")
    return value


def _reject_unknown_keys(
    data: dict[str, Any],
    allowed: set[str],
    section: str,
) -> None:
    """拒绝拼写错误或当前版本不支持的配置项。"""

    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{section} 包含未知配置项：{'、'.join(unknown)}")


def _read_string(
    data: dict[str, Any],
    name: str,
    default: str,
    section: str,
) -> str:
    """读取非空字符串。"""

    value = data.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{section}.{name} 必须是非空字符串。")
    if "\x00" in value:
        raise ValueError(f"{section}.{name} 不能包含空字符。")
    return value.strip()


def _read_required_string(
    data: dict[str, Any],
    name: str,
    section: str,
) -> str:
    """读取没有默认值的必需字符串。"""

    if name not in data:
        raise ValueError(f"{section} 缺少 {name}。")
    return _read_string(data, name, "", section)


def _read_optional_string(
    data: dict[str, Any],
    name: str,
    section: str,
) -> str | None:
    """读取可选非空字符串。"""

    if name not in data:
        return None
    return _read_string(data, name, "", section)


def _read_positive_float(
    data: dict[str, Any],
    name: str,
    default: float,
    section: str,
) -> float:
    """读取大于零的数字配置。"""

    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section}.{name} 必须是数字。")
    if value <= 0:
        raise ValueError(f"{section}.{name} 必须大于 0。")
    return float(value)


def _read_positive_int(
    data: dict[str, Any],
    name: str,
    default: int,
    section: str,
) -> int:
    """读取大于零的整数配置。"""

    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{section}.{name} 必须是大于 0 的整数。")
    return value


def _read_bool(
    data: dict[str, Any],
    name: str,
    default: bool,
    section: str,
) -> bool:
    """严格读取布尔配置。"""

    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{section}.{name} 必须是布尔值。")
    return value


def _read_string_list(
    data: dict[str, Any],
    name: str,
    default: tuple[str, ...],
    section: str,
) -> tuple[str, ...]:
    """读取不含空值和空字符的字符串数组。"""

    value = data.get(name, list(default))
    if not isinstance(value, list):
        raise ValueError(f"{section}.{name} 必须是字符串数组。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ValueError(f"{section}.{name} 只能包含非空字符串。")
        result.append(item.strip())
    return tuple(result)
