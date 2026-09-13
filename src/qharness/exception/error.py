# -*- coding: utf-8 -*-
"""QHarness 统一异常定义。

所有需要跨模块传递的业务异常统一定义在本文件中，调用方可以捕获
QHarnessError 处理所有已知异常，也可以捕获具体子类进行精细处理。
"""

from __future__ import annotations


class QHarnessError(Exception):
    """QHarness 所有业务异常的基类。"""


class LoopTransitionError(QHarnessError, ValueError):
    """推理状态转换违反阶段、身份、版本或任务契约约束。"""


class LoopConfigurationError(QHarnessError, ValueError):
    """Loop 配置或调用上下文不合法。"""


class LoopExecutionError(QHarnessError, RuntimeError):
    """Loop 调用失败，code 区分预算、身份冲突、协议错误及未知执行。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ModelConfigurationError(QHarnessError, ValueError):
    """模型配置不完整或不合法。"""


class ModelBackendError(QHarnessError, RuntimeError):
    """模型调用失败时向 Kernel 暴露的统一异常。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code


class ToolError(QHarnessError):
    """工具注册或执行失败时使用的异常基类。"""


class ToolConfigurationError(ToolError, ValueError):
    """工具运行策略配置不完整或不合法。"""


class ToolRegistrationError(ToolError, ValueError):
    """工具定义不合法或工具名称重复。"""


class ToolExecutionError(ToolError, RuntimeError):
    """工具执行流程中可转换为标准失败结果的异常。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ToolProviderError(ToolError, RuntimeError):
    """工具提供器加载工具失败。"""


class WorkspaceError(QHarnessError):
    """工作区初始化或路径访问失败时使用的异常基类。"""


class WorkspaceConfigurationError(WorkspaceError, ValueError):
    """工作区根目录不存在或不是有效目录。"""


class WorkspacePathError(WorkspaceError, ValueError):
    """目标路径越界、不存在或类型不符合要求。"""


class WorkspaceMutationError(WorkspaceError, RuntimeError):
    """工作区文件修改、历史记录或回滚操作失败。"""


class WorkspaceConflictError(WorkspaceMutationError):
    """文件状态已发生变化，为避免覆盖较新内容而拒绝修改。"""


class WorkspaceHistoryError(WorkspaceMutationError):
    """工作区私有 Git 历史或 SQLAlchemy 操作台账不可用。"""


class WorkspaceHistoryConfigurationError(WorkspaceHistoryError, ValueError):
    """工作区私有版本目录配置不合法。"""


class DatabaseError(QHarnessError):
    """数据库连接、会话或结构迁移失败时使用的异常基类。"""


class DatabaseConfigurationError(DatabaseError, ValueError):
    """全局数据库 URL 或连接池配置不合法。"""


class DatabaseMigrationError(DatabaseError):
    """应用数据库结构初始化或版本迁移失败。"""


class RuntimeManagerError(QHarnessError):
    """托管运行时发现、配置或安装失败时使用的异常基类。"""


class RuntimeConfigurationError(RuntimeManagerError, ValueError):
    """运行时清单、平台映射或路径配置不合法。"""


class RuntimeUnavailableError(RuntimeManagerError, RuntimeError):
    """当前平台尚未提供请求的托管运行时或资源归档。"""


class RuntimeInstallationError(RuntimeManagerError, RuntimeError):
    """运行时摘要校验、解压或原子安装失败。"""


class SandboxError(QHarnessError):
    """沙箱配置、预检或执行失败时使用的异常基类。"""


class SandboxConfigurationError(SandboxError, ValueError):
    """沙箱配置文件不完整或配置值不合法。"""


class SandboxInstallationError(SandboxError, RuntimeError):
    """下载或安装沙箱运行时依赖失败。"""


class SandboxUnavailableError(SandboxError, RuntimeError):
    """沙箱运行时缺失、版本不匹配或尚未完成系统初始化。"""

    def __init__(
        self,
        message: str,
        *,
        setup_command: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.setup_command = setup_command


class SandboxExecutionError(SandboxError, RuntimeError):
    """沙箱进程无法启动或沙箱基础设施异常。"""
