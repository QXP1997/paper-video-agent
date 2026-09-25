# -*- coding: utf-8 -*-
"""沙箱 TOML 配置和 SRT 策略定义。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qharness.exception import SandboxConfigurationError
from qharness.utils.toml import (
    TomlDocumentError,
    load_toml_document,
    read_bool,
    read_float,
    read_int,
    read_optional_string,
    read_string,
    read_string_list,
    read_table,
    reject_unknown_keys,
    resolve_config_path,
)


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
_SRT_KEYS = {
    "python_path",
    "node_path",
    "package_path",
    "expected_version",
    "debug",
}
_FILESYSTEM_KEYS = {"allow_read", "deny_read", "allow_write", "deny_write"}
_NETWORK_KEYS = {"allowed_domains", "denied_domains", "allow_local_binding"}


@dataclass(frozen=True, slots=True)
class SrtRuntimeConfig:
    """选择 Python、Node 和 Anthropic SRT 的托管或自定义来源。"""

    # Python 自定义路径；None 表示使用 QHarness 托管 Python。
    python_path: Path | None

    # Node.js 自定义路径；None 表示使用 QHarness 自动下载的托管 Node。
    node_path: Path | None

    # SRT npm 包自定义路径；None 表示自动下载并使用托管 SRT。
    package_path: Path | None

    # 要求使用的精确 SRT 版本，同时用于隔离不同版本的托管安装目录。
    expected_version: str = "0.0.74"

    # 是否开启 SRT 自身的调试日志，排查策略或启动问题时使用。
    debug: bool = False


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

    try:
        path, document = load_toml_document(config_path, "沙箱配置")
    except TomlDocumentError as error:
        raise SandboxConfigurationError(str(error)) from error

    raw = document.get("sandbox")
    if not isinstance(raw, dict):
        raise SandboxConfigurationError("配置文件缺少 [sandbox] 节。")

    try:
        reject_unknown_keys(raw, _SANDBOX_KEYS, "sandbox")
        srt_raw = read_table(raw, "srt", "sandbox")
        filesystem_raw = read_table(raw, "filesystem", "sandbox")
        network_raw = read_table(raw, "network", "sandbox")
        reject_unknown_keys(srt_raw, _SRT_KEYS, "sandbox.srt")
        reject_unknown_keys(
            filesystem_raw,
            _FILESYSTEM_KEYS,
            "sandbox.filesystem",
        )
        reject_unknown_keys(network_raw, _NETWORK_KEYS, "sandbox.network")

        backend = read_string(raw, "backend", "srt", "sandbox")
        if backend != "srt":
            raise ValueError("sandbox.backend 当前只支持 srt。")

        package_text = read_optional_string(
            srt_raw,
            "package_path",
            "sandbox.srt",
        )
        node_text = read_optional_string(srt_raw, "node_path", "sandbox.srt")
        python_text = read_optional_string(
            srt_raw,
            "python_path",
            "sandbox.srt",
        )
        expected_version = read_optional_string(
            srt_raw,
            "expected_version",
            "sandbox.srt",
        )
        if expected_version is None:
            expected_version = "0.0.74"
        runtime_text = read_string(
            raw,
            "runtime_directory",
            "../.qharness/runtime/sandbox",
            "sandbox",
        )
        return SandboxConfig(
            backend=backend,
            timeout_seconds=read_float(
                raw,
                "timeout_seconds",
                60.0,
                "sandbox",
                positive=True,
            ),
            max_stdout_chars=read_int(
                raw,
                "max_stdout_chars",
                100_000,
                "sandbox",
                positive=True,
            ),
            max_stderr_chars=read_int(
                raw,
                "max_stderr_chars",
                100_000,
                "sandbox",
                positive=True,
            ),
            runtime_directory=resolve_config_path(path, runtime_text),
            srt=SrtRuntimeConfig(
                python_path=(
                    resolve_config_path(path, python_text)
                    if python_text is not None
                    else None
                ),
                node_path=(
                    resolve_config_path(path, node_text)
                    if node_text is not None
                    else None
                ),
                package_path=(
                    resolve_config_path(path, package_text)
                    if package_text is not None
                    else None
                ),
                expected_version=expected_version,
                debug=read_bool(srt_raw, "debug", False, "sandbox.srt"),
            ),
            filesystem=SandboxFilesystemConfig(
                allow_read=read_string_list(
                    filesystem_raw,
                    "allow_read",
                    (".",),
                    "sandbox.filesystem",
                ),
                deny_read=read_string_list(
                    filesystem_raw,
                    "deny_read",
                    (),
                    "sandbox.filesystem",
                ),
                allow_write=read_string_list(
                    filesystem_raw,
                    "allow_write",
                    (".",),
                    "sandbox.filesystem",
                ),
                deny_write=read_string_list(
                    filesystem_raw,
                    "deny_write",
                    (),
                    "sandbox.filesystem",
                ),
            ),
            network=SandboxNetworkConfig(
                allowed_domains=read_string_list(
                    network_raw,
                    "allowed_domains",
                    (),
                    "sandbox.network",
                ),
                denied_domains=read_string_list(
                    network_raw,
                    "denied_domains",
                    (),
                    "sandbox.network",
                ),
                allow_local_binding=read_bool(
                    network_raw,
                    "allow_local_binding",
                    False,
                    "sandbox.network",
                ),
            ),
        )
    except (TypeError, ValueError) as error:
        raise SandboxConfigurationError(f"沙箱配置不合法：{error}") from error
