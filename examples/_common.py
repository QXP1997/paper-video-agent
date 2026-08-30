# -*- coding: utf-8 -*-
"""示例程序共用的项目路径和 Backend 创建逻辑。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
CONFIG_PATH = PROJECT_ROOT / "config" / "model.toml"
TOOL_CONFIG_PATH = PROJECT_ROOT / "config" / "tool.toml"

# 允许示例在未执行 editable install 时直接从源码目录导入。
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from qharness.backends import OpenAICompatibleBackend  # noqa: E402
from qharness.model.config import load_model_config  # noqa: E402
from qharness.exception.error import (  # noqa: E402
    ModelBackendError,
    ModelConfigurationError,
    ToolConfigurationError,
)


def create_backend() -> OpenAICompatibleBackend:
    """读取动态 TOML 配置并创建模型 Backend。"""

    config = load_model_config(CONFIG_PATH)
    return OpenAICompatibleBackend(config)


def print_usage(usage: object | None) -> None:
    """使用适合人工测试的格式输出 Token 用量。"""

    if usage is None:
        print("\nToken 用量：Provider 未返回")
        return

    print(
        "\nToken 用量："
        f"输入={usage.prompt_tokens}，"
        f"输出={usage.completion_tokens}，"
        f"总计={usage.total_tokens}"
    )


def run_example(main_function: Callable[[], Awaitable[None]]) -> None:
    """运行异步示例，并把常见配置或网络错误转换为清晰提示。"""

    try:
        asyncio.run(main_function())
    except ModelConfigurationError as error:
        print(f"配置错误：{error}")
    except ToolConfigurationError as error:
        print(f"工具配置错误：{error}")
    except ModelBackendError as error:
        print(
            f"模型调用错误：{error}\n"
            f"Provider={error.provider}，"
            f"HTTP 状态={error.status_code}，"
            f"建议重试={error.retryable}"
        )

if __name__ == "__main__":
    config = load_model_config(CONFIG_PATH)
