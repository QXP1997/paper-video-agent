# -*- coding: utf-8 -*-
"""示例程序共用的项目路径和 Backend 创建逻辑。"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
CONFIG_PATH = PROJECT_ROOT / "config" / "model.toml"
DATABASE_CONFIG_PATH = PROJECT_ROOT / "config" / "database.toml"
TOOL_CONFIG_PATH = PROJECT_ROOT / "config" / "tool.toml"
SANDBOX_CONFIG_PATH = PROJECT_ROOT / "config" / "sandbox.toml"
HISTORY_CONFIG_PATH = PROJECT_ROOT / "config" / "history.toml"

# 开发阶段由 QHarness 托管的默认工作区。项目源码、配置文件和运行时目录
# 都位于该目录之外，避免沙箱目标进程获得整个 Harness 仓库的访问权限。
SANDBOX_WORKSPACE_ROOT = PROJECT_ROOT / ".qharness" / "workspaces"

# 允许示例在未执行 editable install 时直接从源码目录导入。
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from qharness.backends import OpenAICompatibleBackend  # noqa: E402
from qharness.logging import configure_logging  # noqa: E402
from qharness.model.config import load_model_config  # noqa: E402
from qharness.exception.error import (  # noqa: E402
    DatabaseError,
    ModelBackendError,
    ModelConfigurationError,
    RuntimeManagerError,
    SandboxConfigurationError,
    SandboxExecutionError,
    SandboxInstallationError,
    SandboxUnavailableError,
    ToolConfigurationError,
    ToolProviderError,
    WorkspaceError,
)


_LOGGER = logging.getLogger("qharness.examples")


def create_backend() -> OpenAICompatibleBackend:
    """读取动态 TOML 配置并创建模型 Backend。"""

    config = load_model_config(CONFIG_PATH)
    return OpenAICompatibleBackend(config)


def log_usage(usage: object | None) -> None:
    """使用适合人工测试的格式记录 Token 用量。"""

    if usage is None:
        _LOGGER.info("Token 用量：Provider 未返回")
        return

    _LOGGER.info(
        "Token 用量：输入=%s，输出=%s，总计=%s",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )


def run_example(main_function: Callable[[], Awaitable[None]]) -> None:
    """运行异步示例，并把常见配置或网络错误转换为清晰提示。"""

    # 每个示例至少拥有标准控制台日志。具体示例可在 main 中再次调用该函数，
    # 动态开启 DEBUG 或增加轮转文件，重复配置不会产生重复 Handler。
    configure_logging()
    try:
        asyncio.run(main_function())
    except ModelConfigurationError as error:
        _LOGGER.error("配置错误：%s", error)
    except DatabaseError as error:
        _LOGGER.error("数据库错误：%s", error)
    except RuntimeManagerError as error:
        _LOGGER.error("托管运行时错误：%s", error)
    except ToolConfigurationError as error:
        _LOGGER.error("工具配置错误：%s", error)
    except ToolProviderError as error:
        _LOGGER.error("工具提供器错误：%s", error)
    except SandboxConfigurationError as error:
        _LOGGER.error("沙箱配置错误：%s", error)
    except SandboxInstallationError as error:
        _LOGGER.error("沙箱依赖安装错误：%s", error)
    except SandboxUnavailableError as error:
        _LOGGER.error("沙箱不可用：%s", error)
        if error.setup_command:
            _LOGGER.warning("初始化命令：%s", " ".join(error.setup_command))
    except SandboxExecutionError as error:
        _LOGGER.error("沙箱执行错误：%s", error)
    except WorkspaceError as error:
        _LOGGER.error("工作区错误：%s", error)
    except ModelBackendError as error:
        _LOGGER.error(
            "模型调用错误：%s；Provider=%s，HTTP 状态=%s，建议重试=%s",
            error,
            error.provider,
            error.status_code,
            error.retryable,
        )

if __name__ == "__main__":
    config = load_model_config(CONFIG_PATH)
