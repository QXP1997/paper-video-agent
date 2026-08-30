# -*- coding: utf-8 -*-
"""QHarness 统一异常定义。

所有需要跨模块传递的业务异常统一定义在本文件中，调用方可以捕获
QHarnessError 处理所有已知异常，也可以捕获具体子类进行精细处理。
"""

from __future__ import annotations


class QHarnessError(Exception):
    """QHarness 所有业务异常的基类。"""


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


class WorkspaceError(QHarnessError):
    """工作区初始化或路径访问失败时使用的异常基类。"""


class WorkspaceConfigurationError(WorkspaceError, ValueError):
    """工作区根目录不存在或不是有效目录。"""


class WorkspacePathError(WorkspaceError, ValueError):
    """目标路径越界、不存在或类型不符合要求。"""
