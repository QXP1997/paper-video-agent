# -*- coding: utf-8 -*-
"""沙箱后端的统一领域对象与抽象接口。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from qharness.exception import SandboxExecutionError


@dataclass(frozen=True, slots=True)
class SandboxExecutionRequest:
    """描述一次不经过宿主 Shell 拼接的沙箱进程调用。"""

    # 要启动的程序名称或绝对路径，例如 python、git 或 python.exe。
    executable: str

    # 直接传给目标程序的参数；每一项都是独立 argv，不经过宿主 Shell 拼接。
    arguments: tuple[str, ...] = ()

    # 目标进程的工作目录，只允许填写当前 WorkspaceContext 内的路径。
    cwd: str | Path = "."

    # 写入目标进程标准输入的 UTF-8 文本；None 表示不提供标准输入。
    stdin: str | None = None

    # 本次执行的超时秒数；None 表示使用 SandboxConfig 中的全局默认值。
    timeout_seconds: float | None = None

    # 本次标准输出最多保留的字符数；None 表示使用全局默认值。
    max_stdout_chars: int | None = None

    # 本次标准错误最多保留的字符数；None 表示使用全局默认值。
    max_stderr_chars: int | None = None

    # 所属 Agent Run 的标识，后续用于串联日志、审计记录和文件变更。
    run_id: str | None = None

    # 本次具体操作的标识，后续可以与版本控制或撤回记录进行关联。
    operation_id: str | None = None

    # 外部主动取消信号；事件被设置后会终止本次沙箱进程树。
    cancellation_event: asyncio.Event | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """尽早拒绝会破坏 argv 边界或执行限制的非法输入。"""

        if not isinstance(self.executable, str) or not self.executable.strip():
            raise SandboxExecutionError("沙箱可执行文件名称不能为空。")
        if "\x00" in self.executable:
            raise SandboxExecutionError("沙箱可执行文件名称不能包含空字符。")
        if not isinstance(self.arguments, tuple):
            raise SandboxExecutionError("arguments 必须是字符串元组。")
        for argument in self.arguments:
            if not isinstance(argument, str) or "\x00" in argument:
                raise SandboxExecutionError(
                    "每个沙箱命令参数都必须是不含空字符的字符串。"
                )
        _validate_optional_positive_number(
            self.timeout_seconds,
            "timeout_seconds",
        )
        _validate_optional_positive_int(
            self.max_stdout_chars,
            "max_stdout_chars",
        )
        _validate_optional_positive_int(
            self.max_stderr_chars,
            "max_stderr_chars",
        )


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    """保存命令本身的退出状态以及 QHarness 主动中止原因。"""

    # 目标进程退出码；0 通常表示成功，None 表示未取得有效退出码。
    exit_code: int | None

    # 从目标进程标准输出读取到的 UTF-8 文本。
    stdout: str

    # 从目标进程标准错误读取到的 UTF-8 文本。
    stderr: str

    # 从启动到退出或被终止的总耗时，单位为秒。
    duration_seconds: float

    # 是否因为超过 timeout_seconds 而被 QHarness 主动终止。
    timed_out: bool = False

    # 是否因为 cancellation_event 被设置而主动终止。
    cancelled: bool = False

    # 标准输出是否超过字符上限；超出部分不会保存在内存中。
    stdout_truncated: bool = False

    # 标准错误是否超过字符上限；超出部分不会保存在内存中。
    stderr_truncated: bool = False

    # 原样回传请求中的 Agent Run 标识，方便调用方关联上下文。
    run_id: str | None = None

    # 原样回传请求中的操作标识，方便调用方定位本次执行。
    operation_id: str | None = None

    @property
    def succeeded(self) -> bool:
        """仅在进程正常以零退出且未被 QHarness 中止时返回真。"""

        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and not self.stdout_truncated
            and not self.stderr_truncated
        )


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    """表示沙箱运行时和系统隔离能力的预检结果。"""

    # 沙箱后端名称，例如 srt。
    backend: str

    # 当前机器是否已经满足安全执行条件；为 False 时不得降级执行。
    available: bool

    # 面向用户的状态说明或不可用原因。
    message: str

    # 实际检测到的沙箱运行时版本；无法识别时为 None。
    version: str | None = None

    # 是否还需要用户执行一次系统级初始化，例如 Windows SRT 安装。
    setup_required: bool = False

    # 建议用户人工执行的初始化 argv；仅展示，不由 QHarness 自动提权运行。
    setup_command: tuple[str, ...] | None = None


class SandboxBackend(ABC):
    """所有本地沙箱实现共同遵循的最小接口。"""

    @abstractmethod
    async def check_status(self) -> SandboxStatus:
        """以只读方式检查依赖、身份、版本和系统初始化状态。"""

    @abstractmethod
    async def execute(
        self,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResult:
        """在沙箱内执行一个 argv 进程，并收集受限输出。"""


def _validate_optional_positive_number(
    value: float | None,
    name: str,
) -> None:
    """校验可选正数。"""

    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SandboxExecutionError(f"{name} 必须是正数。")
    if value <= 0:
        raise SandboxExecutionError(f"{name} 必须大于 0。")


def _validate_optional_positive_int(value: int | None, name: str) -> None:
    """校验可选正整数。"""

    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SandboxExecutionError(f"{name} 必须是大于 0 的整数。")
