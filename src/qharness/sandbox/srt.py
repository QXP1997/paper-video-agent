# -*- coding: utf-8 -*-
"""Anthropic Sandbox Runtime（SRT）沙箱后端。"""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import logging
import os
import platform
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qharness.exception import (
    RuntimeManagerError,
    SandboxConfigurationError,
    SandboxExecutionError,
    SandboxInstallationError,
    SandboxUnavailableError,
)
from qharness.runtime import RuntimeManager, RuntimeName
from qharness.sandbox.base import (
    SandboxBackend,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxSetupResult,
    SandboxStatus,
)
from qharness.sandbox.config import SandboxConfig
from qharness.sandbox.srt_package import SrtPackageManager
from qharness.sandbox.windows_elevation import (
    run_srt_install_with_confirmation,
)
from qharness.workspace import WorkspaceContext


_EXPECTED_PACKAGE_NAME = "@anthropic-ai/sandbox-runtime"
_MINIMUM_NODE_VERSION = (20, 11, 0)
_READ_CHUNK_BYTES = 16 * 1024
_LOGGER = logging.getLogger(__name__)
_SRT_DEBUG_PREFIX = "[SandboxDebug]"
_SENSITIVE_SRT_LOG_MARKERS = (
    "Command string mode (-c):",
    "Original command:",
)


class SrtSandboxBackend(SandboxBackend):
    """通过 SRT CLI 在本机操作系统隔离边界内执行进程。"""

    def __init__(
        self,
        config: SandboxConfig,
        workspace: WorkspaceContext,
        runtime_manager: RuntimeManager | None = None,
    ) -> None:
        """保存动态配置、当前 Agent Run 工作区和托管运行时管理器。"""

        self._config = config
        self._workspace = workspace
        # 保留直接构造 Backend 的便利性；工厂路径会显式注入同一实例。
        self._runtime_manager = runtime_manager or RuntimeManager(
            config.runtime_directory.parent
        )
        self._srt_package_manager = SrtPackageManager(
            self._runtime_manager.runtime_root,
            version=config.srt.expected_version,
        )

    async def check_status(self) -> SandboxStatus:
        """只读检查 npm 包身份、Node 版本和 Windows 初始化状态。"""

        try:
            version = self._read_package_version()
            node_path = self._resolve_node_path()
            await self._check_node_version(node_path)
            if os.name == "nt":
                return await self._check_windows_status(version)
            return SandboxStatus(
                backend="srt",
                available=True,
                version=version,
                message=(
                    "SRT 包和 Node.js 已通过预检；具体系统依赖会在首次执行时"
                    "由 SRT 自身继续校验。"
                ),
            )
        except (SandboxConfigurationError, OSError, ValueError) as error:
            return SandboxStatus(
                backend="srt",
                available=False,
                message=str(error),
            )

    async def prepare(self) -> SandboxStatus:
        """自动准备托管 Python、Node 和 SRT，并返回最新只读状态。"""

        try:
            if self._config.srt.python_path is None:
                await asyncio.to_thread(
                    self._runtime_manager.ensure,
                    RuntimeName.PYTHON,
                )
            else:
                self._resolve_python_path()
            if self._config.srt.node_path is None:
                node_path = await asyncio.to_thread(
                    self._runtime_manager.ensure,
                    RuntimeName.NODE,
                )
            else:
                node_path = self._resolve_node_path()
            if self._config.srt.package_path is None:
                await asyncio.to_thread(
                    self._srt_package_manager.ensure,
                    node_path,
                )
        except (
            RuntimeManagerError,
            SandboxConfigurationError,
            SandboxInstallationError,
        ) as error:
            return SandboxStatus(
                backend="srt",
                available=False,
                message=f"无法准备 SRT 本地依赖：{error}",
            )
        return await self.check_status()

    async def setup(self, *, force: bool = False) -> SandboxSetupResult:
        """在用户确认后通过 Windows UAC 初始化或修复 SRT 系统组件。

        下载 Node 和 npm 包不需要管理员权限，会先自动完成。真正改变系统
        账户与权限的 ``srt-win install`` 只能在本方法中由用户点击确认后运行。
        ``force=True`` 用于状态检查通过但实际执行暴露本地状态损坏时进行修复。
        """

        prepared = await self.prepare()
        if os.name != "nt":
            return SandboxSetupResult(
                completed=prepared.available,
                cancelled=False,
                message=(
                    prepared.message
                    if prepared.available
                    else "当前系统不支持 Windows SRT 初始化窗口。"
                ),
                status=prepared,
            )
        if not prepared.available and not prepared.setup_required:
            return SandboxSetupResult(
                completed=False,
                cancelled=False,
                message=prepared.message,
                status=prepared,
            )
        if prepared.available and not force:
            return SandboxSetupResult(
                completed=True,
                cancelled=False,
                message="SRT 已通过预检，无需重复初始化。",
                status=prepared,
            )

        _LOGGER.info("等待用户确认 SRT Windows 系统初始化。")
        elevated = await asyncio.to_thread(
            run_srt_install_with_confirmation,
            self._windows_helper_path(),
        )
        if elevated.cancelled or elevated.exit_code != 0:
            log_method = _LOGGER.info if elevated.cancelled else _LOGGER.error
            log_method("SRT Windows 系统初始化未完成：%s", elevated.message)
            return SandboxSetupResult(
                completed=False,
                cancelled=elevated.cancelled,
                message=elevated.message,
                exit_code=elevated.exit_code,
                status=prepared,
            )

        status = await self.check_status()
        completed = status.available
        message = (
            "SRT Windows 系统初始化完成，并已通过预检。"
            if completed
            else f"初始化程序已退出，但 SRT 预检仍未通过：{status.message}"
        )
        log_method = _LOGGER.info if completed else _LOGGER.error
        log_method(message)
        return SandboxSetupResult(
            completed=completed,
            cancelled=False,
            message=message,
            exit_code=elevated.exit_code,
            status=status,
        )

    async def execute(
        self,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResult:
        """在 SRT 沙箱中执行进程，并统一处理输出、超时和取消。

        目标程序正常退出或被执行限制中止时返回 SandboxExecutionResult；
        SRT 不可用、配置错误或进程根本无法启动时抛出对应业务异常。
        """

        _LOGGER.info(
            "SRT 执行开始：run_id=%s，operation_id=%s，command_chars=%s，cwd=%s",
            request.run_id or "-",
            request.operation_id or "-",
            len(request.command),
            request.cwd,
        )

        # 第一步：执行只读预检。沙箱不可用时明确失败，绝不绕过 SRT
        # 直接在宿主机运行目标命令。
        status = await self.prepare()
        if not status.available:
            _LOGGER.error("SRT 预检失败：%s", status.message)
            raise SandboxUnavailableError(
                status.message,
                setup_command=status.setup_command,
            )

        # 第二步：把工作目录限制在当前工作区，并构造托管运行时优先的 PATH。
        # 模型命令中的 python/node 保持原样，由沙箱内部 Shell 按 PATH 解析。
        cwd = self._workspace.resolve_directory(request.cwd)
        python_path = self._resolve_python_path()
        node_path = self._resolve_node_path()
        execution_environment = self._build_execution_environment(
            python_path,
            node_path,
        )
        settings_path = self._write_settings_file(
            additional_read_paths=(python_path.parent, node_path.parent)
        )
        _LOGGER.debug(
            "SRT 执行环境已解析：workspace=%s，settings=%s，python=%s，node=%s",
            self._workspace.root,
            settings_path,
            python_path,
            node_path,
        )

        # 第三步：使用 SRT 原生 ``-c`` 命令字符串模式。Windows 版 SRT 默认
        # 使用 cmd.exe，因此外层固定启动 PowerShell；Linux/macOS 直接由 SRT
        # 默认 Bash 解释模型命令。QHarness 不解析管道、重定向或变量语法。
        command = [
            str(node_path),
            str(self._resolve_package_path() / "dist" / "cli.js"),
            "--settings",
            str(settings_path),
        ]
        if self._config.srt.debug:
            command.append("--debug")
        if os.name == "nt":
            command.extend(("-c", _build_windows_sandbox_command(request.command)))
        else:
            command.extend(("-c", request.command))

        # 第四步：让 SRT CLI 拥有独立进程组。它不负责沙箱隔离，只用于
        # 超时或取消时从最外层 PID 开始清理 SRT、Python 及其所有后代进程。
        creationflags, start_new_session = _process_group_options()

        started_at = time.monotonic()
        try:
            # 第五步：直接使用 argv 启动，不设置 shell=True。标准输入输出使用
            # asyncio 管道，便于异步写入、增量读取和实施字符数上限。
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=(
                    asyncio.subprocess.PIPE
                    if request.stdin is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=execution_environment,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except OSError as error:
            _LOGGER.exception("SRT 进程无法启动。")
            raise SandboxExecutionError(
                f"SRT 进程无法启动：{error}"
            ) from error
        _LOGGER.info("SRT 隔离进程已启动：pid=%s", process.pid)

        # 第六步：每次请求可以覆盖全局输出上限。stdout 和 stderr 必须并发读取，
        # 否则其中一个系统管道写满后，子进程可能阻塞并形成死锁。
        stdout_limit = request.max_stdout_chars or self._config.max_stdout_chars
        stderr_limit = request.max_stderr_chars or self._config.max_stderr_chars
        output_exceeded = asyncio.Event()
        stdout_task = asyncio.create_task(
            _read_limited_stream(
                process.stdout,
                stdout_limit,
                output_exceeded,
            )
        )
        stderr_task = asyncio.create_task(
            _read_limited_stream(
                process.stderr,
                stderr_limit,
                output_exceeded,
                line_callback=(
                    _log_srt_debug_line if self._config.srt.debug else None
                ),
            )
        )
        stdin_task = asyncio.create_task(_feed_stdin(process, request.stdin))

        # process_task 等待目标进程退出；output_task 等待任一输出超限；
        # cancellation_task 只在调用方提供主动取消事件时创建。
        process_task = asyncio.create_task(process.wait())
        output_task = asyncio.create_task(output_exceeded.wait())
        cancellation_task: asyncio.Task[bool] | None = None
        if request.cancellation_event is not None:
            cancellation_task = asyncio.create_task(
                request.cancellation_event.wait()
            )

        wait_tasks: set[asyncio.Task[Any]] = {process_task, output_task}
        if cancellation_task is not None:
            wait_tasks.add(cancellation_task)

        # 第七步：同时等待正常退出、输出超限和主动取消，并额外应用执行超时。
        # 哪个条件最先发生，就由哪个条件决定后续处理。
        timeout = request.timeout_seconds or self._config.timeout_seconds
        timed_out = False
        cancelled = False
        try:
            done, _ = await asyncio.wait(
                wait_tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # asyncio.wait 在规定时间内没有任何任务完成，说明执行超时。
                timed_out = True
                _LOGGER.warning(
                    "SRT 执行超时，正在终止进程树：pid=%s，timeout=%.3f 秒",
                    process.pid,
                    timeout,
                )
                await _terminate_process_tree(process)
            elif process_task in done:
                # 进程正常结束优先，避免与同时抵达的取消信号产生错误归因。
                pass
            elif cancellation_task is not None and cancellation_task in done:
                # 调用方设置了 cancellation_event，终止完整进程树并记录归因。
                cancelled = True
                _LOGGER.info(
                    "SRT 收到主动取消信号，正在终止进程树：pid=%s",
                    process.pid,
                )
                await _terminate_process_tree(process)
            elif output_task in done and output_exceeded.is_set():
                # stdout 或 stderr 达到上限后立即终止，避免无限输出消耗资源。
                _LOGGER.warning(
                    "SRT 输出达到上限，正在终止进程树：pid=%s",
                    process.pid,
                )
                await _terminate_process_tree(process)

            # 第八步：终止信号发出后给进程树少量清理时间；仍未退出时，
            # 再强制杀死最外层进程，避免 execute 永久卡住。
            try:
                await asyncio.wait_for(process_task, timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        except asyncio.CancelledError:
            # asyncio Task 自身被取消与 request.cancellation_event 不同：这里完成
            # 进程树和内部任务清理后，必须继续抛出 CancelledError 给上层感知。
            _LOGGER.info(
                "SRT execute 任务被取消，正在清理进程树：pid=%s",
                process.pid,
            )
            await _terminate_process_tree(process)
            await asyncio.gather(process.wait(), return_exceptions=True)
            for task in (
                stdout_task,
                stderr_task,
                stdin_task,
                process_task,
                output_task,
                cancellation_task,
            ):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                stdin_task,
                process_task,
                output_task,
                *([cancellation_task] if cancellation_task is not None else []),
                return_exceptions=True,
            )
            raise
        finally:
            # output_task 和 cancellation_task 只是监控任务，主流程结束后不应残留。
            # stdin 仍在等待写入时也一并取消。
            for task in (output_task, cancellation_task):
                if task is not None and not task.done():
                    task.cancel()
            if not stdin_task.done():
                stdin_task.cancel()

        # 第九步：读取任务已经增量排空管道，这里收集最终文本和截断标志，
        # 再把执行归因字段原样带回调用方。
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
        # SRT debug 会把完整目标命令（可能包含密钥）写入 stderr。实时日志和
        # 返回结果都执行相同脱敏，避免上层把原文再次落盘。
        stderr = _redact_srt_debug_commands(stderr)
        await asyncio.gather(
            stdin_task,
            output_task,
            *([cancellation_task] if cancellation_task is not None else []),
            return_exceptions=True,
        )
        execution_succeeded = (
            process.returncode == 0
            and not timed_out
            and not cancelled
            and not stdout_truncated
            and not stderr_truncated
        )
        # SRT 把自身 DEBUG 日志与目标程序 stderr 写入同一条管道，无法可靠
        # 拆分。成功时这些诊断已经实时进入 QHarness 日志，不再回填给模型，
        # 避免浪费上下文；失败时保留完整内容，帮助模型定位命令或沙箱问题。
        result_stderr = (
            ""
            if self._config.srt.debug and execution_succeeded
            else stderr
        )
        result = SandboxExecutionResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=result_stderr,
            duration_seconds=time.monotonic() - started_at,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            run_id=request.run_id,
            operation_id=request.operation_id,
        )
        log_method = _LOGGER.info if result.succeeded else _LOGGER.warning
        log_method(
            "SRT 执行结束：pid=%s，exit_code=%s，耗时=%.3f 秒，"
            "timed_out=%s，cancelled=%s，stdout_truncated=%s，"
            "stderr_truncated=%s",
            process.pid,
            result.exit_code,
            result.duration_seconds,
            result.timed_out,
            result.cancelled,
            result.stdout_truncated,
            result.stderr_truncated,
        )
        return result

    def _read_package_version(self) -> str:
        """读取 package.json，避免误调用 PATH 中碰巧同名的程序。"""

        package_path = self._resolve_package_path()
        metadata_path = package_path / "package.json"
        cli_path = package_path / "dist" / "cli.js"
        if not metadata_path.is_file() or not cli_path.is_file():
            raise SandboxConfigurationError(
                f"SRT npm 包不完整：{package_path}"
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SandboxConfigurationError(
                f"无法读取 SRT package.json：{error}"
            ) from error
        if metadata.get("name") != _EXPECTED_PACKAGE_NAME:
            raise SandboxConfigurationError(
                "配置路径不是 Anthropic Sandbox Runtime npm 包："
                f"{package_path}"
            )
        version = metadata.get("version")
        if not isinstance(version, str) or not version:
            raise SandboxConfigurationError("SRT package.json 缺少有效版本号。")
        expected = self._config.srt.expected_version
        if version != expected:
            raise SandboxConfigurationError(
                f"SRT 版本不匹配：期望 {expected}，实际 {version}。"
            )
        return version

    def _resolve_package_path(self) -> Path:
        """优先使用用户配置路径，否则返回已验证的托管 SRT 路径。"""

        configured = self._config.srt.package_path
        if configured is not None:
            return configured
        status = self._srt_package_manager.check_status()
        if not status.available:
            raise SandboxConfigurationError(
                "QHarness 托管 SRT 尚未准备完成，请先调用 sandbox.prepare()："
                f"{status.message}"
            )
        return status.package_path

    def _resolve_node_path(self) -> Path:
        """优先使用用户配置路径，否则使用已准备好的 QHarness 托管 Node。"""

        configured = self._config.srt.node_path
        if configured is not None:
            if not configured.is_file():
                raise SandboxConfigurationError(
                    f"Node.js 可执行文件不存在：{configured}"
                )
            return configured
        status = self._runtime_manager.check_status(RuntimeName.NODE)
        if not status.available or status.executable_path is None:
            raise SandboxConfigurationError(
                "QHarness 托管 Node 尚未准备完成，请先调用 sandbox.prepare()："
                f"{status.message}"
            )
        return status.executable_path

    def _resolve_python_path(self) -> Path:
        """优先使用用户配置路径，否则使用 QHarness 托管 Python。"""

        configured = self._config.srt.python_path
        if configured is not None:
            if not configured.is_file():
                raise SandboxConfigurationError(
                    f"Python 可执行文件不存在：{configured}"
                )
            return configured
        status = self._runtime_manager.check_status(RuntimeName.PYTHON)
        if not status.available or status.executable_path is None:
            raise SandboxConfigurationError(
                "QHarness 托管 Python 尚未准备完成，请先调用 sandbox.prepare()："
                f"{status.message}"
            )
        return status.executable_path

    @staticmethod
    def _build_execution_environment(
        python_path: Path,
        node_path: Path,
    ) -> dict[str, str]:
        """构造传给 SRT 的环境，使裸 python/node 优先命中选定运行时。"""

        environment = dict(os.environ)
        runtime_directories = [str(python_path.parent), str(node_path.parent)]
        existing_path = environment.get("PATH", "")
        if existing_path:
            runtime_directories.append(existing_path)
        environment["PATH"] = os.pathsep.join(runtime_directories)
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    async def _check_node_version(self, node_path: Path) -> None:
        """确认 Node.js 满足 SRT npm 包声明的最低版本。"""

        try:
            process = await asyncio.create_subprocess_exec(
                str(node_path),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxConfigurationError(
                f"无法检查 Node.js 版本：{error}"
            ) from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=5.0,
            )
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise SandboxConfigurationError(
                "Node.js 版本检查超时。"
            ) from error
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise SandboxConfigurationError(
                f"Node.js 版本检查失败：{detail or process.returncode}"
            )
        text = stdout.decode("utf-8", errors="replace").strip().lstrip("v")
        try:
            version = tuple(int(item) for item in text.split(".")[:3])
        except ValueError as error:
            raise SandboxConfigurationError(
                f"无法识别 Node.js 版本：{text}"
            ) from error
        if len(version) != 3 or version < _MINIMUM_NODE_VERSION:
            required = ".".join(str(item) for item in _MINIMUM_NODE_VERSION)
            raise SandboxConfigurationError(
                f"Node.js 版本过低：{text}，SRT 至少需要 {required}。"
            )

    async def _check_windows_status(self, version: str) -> SandboxStatus:
        """通过 srt-win status 判断一次性系统安装是否已经完成。"""

        helper_path = self._windows_helper_path()
        setup_command = (str(helper_path), "install")
        try:
            process = await asyncio.create_subprocess_exec(
                str(helper_path),
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            return SandboxStatus(
                backend="srt",
                available=False,
                version=version,
                message=f"无法执行 srt-win 状态检查：{error}",
                setup_required=True,
                setup_command=setup_command,
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=10.0,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return SandboxStatus(
                backend="srt",
                available=False,
                version=version,
                message="srt-win 状态检查超时。",
                setup_command=setup_command,
            )
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return SandboxStatus(
                backend="srt",
                available=False,
                version=version,
                message=f"srt-win 状态检查失败：{detail or process.returncode}",
                setup_required=True,
                setup_command=setup_command,
            )
        try:
            status = json.loads(stdout.decode("utf-8"))
            user_status = status["user"]
            account_status = user_status["user"]
            ready = bool(account_status["exists"] and user_status["cred_present"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            return SandboxStatus(
                backend="srt",
                available=False,
                version=version,
                message=f"无法解析 srt-win 状态：{error}",
                setup_command=setup_command,
            )
        if not ready:
            return SandboxStatus(
                backend="srt",
                available=False,
                version=version,
                setup_required=True,
                setup_command=setup_command,
                message=(
                    "Anthropic SRT 已安装到项目，但 Windows 隔离账户尚未初始化。"
                    "请调用 sandbox.setup()，并由用户在窗口中确认 UAC。"
                ),
            )
        return SandboxStatus(
            backend="srt",
            available=True,
            version=version,
            message="Anthropic SRT 与 Windows 隔离账户已通过预检。",
            setup_command=setup_command,
        )

    def _windows_helper_path(self) -> Path:
        """根据当前 CPU 架构选择 npm 包内置的 srt-win.exe。"""

        machine = platform.machine().lower()
        architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        helper = (
            self._resolve_package_path()
            / "vendor"
            / "srt-win"
            / architecture
            / "srt-win.exe"
        )
        if not helper.is_file():
            raise SandboxConfigurationError(
                f"当前架构缺少 srt-win.exe：{helper}"
            )
        return helper

    def _write_settings_file(
        self,
        *,
        additional_read_paths: tuple[Path, ...] = (),
    ) -> Path:
        """生成仅含非密钥策略的 SRT JSON，并以内容摘要稳定命名。"""

        filesystem = self._config.filesystem
        allow_read = self._resolve_policy_paths(filesystem.allow_read)
        allow_read.extend(
            str(path.resolve(strict=False)) for path in additional_read_paths
        )
        allow_read = list(dict.fromkeys(allow_read))
        settings: dict[str, Any] = {
            "filesystem": {
                "allowRead": allow_read,
                "denyRead": self._resolve_policy_paths(filesystem.deny_read),
                "allowWrite": self._resolve_policy_paths(filesystem.allow_write),
                "denyWrite": self._resolve_policy_paths(filesystem.deny_write),
            },
            "network": {
                "allowedDomains": list(self._config.network.allowed_domains),
                "deniedDomains": list(self._config.network.denied_domains),
                "allowLocalBinding": self._config.network.allow_local_binding,
            },
        }
        if os.name == "nt":
            settings["windows"] = {
                "srtWin": {"path": str(self._windows_helper_path())}
            }
        payload = json.dumps(
            settings,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        directory = self._config.runtime_directory
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"srt-settings-{digest}.json"
            if target.is_file():
                return target
            temporary = directory / (
                f".{target.name}.{uuid.uuid4().hex}.tmp"
            )
            temporary.write_text(payload + "\n", encoding="utf-8")
            os.replace(temporary, target)
            if not target.is_file():
                raise OSError("策略文件写入后不存在。")
            return target
        except OSError as error:
            raise SandboxExecutionError(
                f"无法生成 SRT 运行策略：{error}"
            ) from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 临时文件清理失败不覆盖更有价值的主流程结果。
                    pass

    def _resolve_policy_paths(self, values: tuple[str, ...]) -> list[str]:
        """把相对策略路径绑定到工作区，把波浪号绑定到本机用户目录。"""

        result: list[str] = []
        for value in values:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = self._workspace.root / candidate
            result.append(str(candidate.resolve(strict=False)))
        return result


async def _feed_stdin(
    process: asyncio.subprocess.Process,
    content: str | None,
) -> None:
    """向子进程写入可选标准输入，并及时关闭管道。"""

    if process.stdin is None:
        return
    try:
        if content is not None:
            process.stdin.write(content.encode("utf-8"))
            await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


def _process_group_options() -> tuple[int, bool]:
    """返回当前操作系统创建独立进程组所需的 subprocess 参数。

    返回值依次对应 create_subprocess_exec 的 creationflags 和
    start_new_session。Windows 使用标准库常量；Linux/macOS 创建新 Session，
    使最外层进程 PID 同时成为后续可以整体终止的进程组入口。
    """

    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP, False
    return 0, True


def _build_windows_sandbox_command(command: str) -> str:
    """把模型命令编码为仅在 Windows SRT 内部解析的 PowerShell 脚本。

    SRT CLI 的 ``-c`` 最终由沙箱账户下的 cmd.exe 执行。这里只向 cmd.exe
    传递固定的 PowerShell 启动参数和 Base64 文本。模型命令本身作为完整的
    PowerShell 脚本运行，管道、重定向、变量和条件语法均不会被 QHarness 改写。
    """

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        # Windows 中文系统默认可能使用 GBK。QHarness 的跨平台输出协议统一为
        # UTF-8，因此必须在目标进程启动前同步控制台、PowerShell 管道和
        # Python 标准流编码，避免字节进入异步读取器后才被错误解码。
        "$utf8 = [System.Text.UTF8Encoding]::new($false)\n"
        "[Console]::InputEncoding = $utf8\n"
        "[Console]::OutputEncoding = $utf8\n"
        "$OutputEncoding = $utf8\n"
        "$env:PYTHONUTF8 = '1'\n"
        "$env:PYTHONIOENCODING = 'utf-8'\n"
        f"{command}\n"
        # PowerShell 的 $LASTEXITCODE 只记录最近一次原生进程，后续成功的
        # Cmdlet 不会清空它。因此先保存代表最后一条语句的 $?，避免把已经
        # 恢复成功的命令错误地判定为失败。
        "$qharnessCommandSucceeded = $?\n"
        "if ($qharnessCommandSucceeded) { exit 0 }\n"
        "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "exit 1\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive "
        f"-EncodedCommand {encoded}"
    )


async def _read_limited_stream(
    stream: asyncio.StreamReader | None,
    max_chars: int,
    exceeded_event: asyncio.Event,
    line_callback: Callable[[str], None] | None = None,
) -> tuple[str, bool]:
    """增量解码输出，达到字符上限后通知执行器并继续排空管道。

    ``line_callback`` 只接收上限以内的完整文本行，主要用于实时转发 SRT
    自身的调试日志。回调和最终结果共用同一个字符上限，防止日志转发绕过
    沙箱输出限制。
    """

    if stream is None:
        return "", False
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    remaining = max_chars
    truncated = False
    pending_line = ""
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            text = decoder.decode(b"", final=True)
            if text and remaining > 0:
                kept = text[:remaining]
                parts.append(kept)
                pending_line = _emit_complete_lines(
                    pending_line,
                    kept,
                    line_callback,
                )
                if len(text) > remaining:
                    truncated = True
            break
        text = decoder.decode(chunk)
        kept_length = 0
        if remaining > 0:
            kept = text[:remaining]
            parts.append(kept)
            pending_line = _emit_complete_lines(
                pending_line,
                kept,
                line_callback,
            )
            kept_length = len(kept)
            remaining -= len(kept)
        if len(text) > kept_length:
            truncated = True
        if truncated:
            exceeded_event.set()
    if pending_line and line_callback is not None:
        line_callback(pending_line.rstrip("\r"))
    return "".join(parts), truncated


def _emit_complete_lines(
    pending: str,
    text: str,
    callback: Callable[[str], None] | None,
) -> str:
    """向回调发送新出现的完整行，并返回尚未遇到换行符的尾部。"""

    if callback is None:
        return ""
    pending += text
    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        callback(line.rstrip("\r"))
    return pending


def _log_srt_debug_line(line: str) -> None:
    """实时记录 SRT 原生日志，并隐藏其中可能包含密钥的完整命令。"""

    if not line.startswith(_SRT_DEBUG_PREFIX):
        return
    if any(marker in line for marker in _SENSITIVE_SRT_LOG_MARKERS):
        _LOGGER.debug("SRT | %s 命令内容已隐藏", _SRT_DEBUG_PREFIX)
        return
    _LOGGER.debug("SRT | %s", line)


def _redact_srt_debug_commands(content: str) -> str:
    """隐藏 SRT stderr 中携带完整目标命令的调试行。"""

    lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if any(marker in line for marker in _SENSITIVE_SRT_LOG_MARKERS):
            if line.endswith("\r\n"):
                suffix = "\r\n"
            elif line.endswith("\n"):
                suffix = "\n"
            else:
                suffix = ""
            lines.append(f"{_SRT_DEBUG_PREFIX} 命令内容已隐藏{suffix}")
        else:
            lines.append(line)
    return "".join(lines)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """终止 SRT CLI 及其沙箱进程树，防止超时任务遗留后台进程。"""

    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
            return
        except (OSError, TimeoutError):
            process.kill()
            return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
