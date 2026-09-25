# -*- coding: utf-8 -*-
"""模型 Backend 的动态配置读取模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qharness.exception.error import ModelConfigurationError
from qharness.utils.text import strip_to_none
from qharness.utils.toml import (
    TomlDocumentError,
    load_toml_document,
    read_bool,
    read_float,
    read_int,
    read_optional_float,
    read_optional_int,
    read_optional_string,
    read_required_string,
    read_string,
    read_table,
    reject_unknown_keys,
)


_MODEL_KEYS = {
    "provider",
    "base_url",
    "api_key",
    "model",
    "timeout_seconds",
    "max_retries",
    "temperature",
    "max_tokens",
    "stream_include_usage",
    "thinking_mode",
    "reasoning_effort",
    "extra_body",
}


@dataclass(frozen=True, slots=True)
class ModelBackendConfig:
    """OpenAI-compatible Backend 的运行配置。"""

    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    max_retries: int = 2
    temperature: float | None = None
    max_tokens: int | None = None
    stream_include_usage: bool = True
    thinking_mode: str | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def require_api_key(self) -> str:
        """返回 API Key；未配置时给出便于用户处理的错误。"""

        if self.api_key:
            return self.api_key

        raise ModelConfigurationError(
            "未找到模型 API Key。请在模型配置文件中设置 model.api_key。"
        )


def load_model_config(config_path: str | Path) -> ModelBackendConfig:
    """从 TOML 文件动态读取模型 Backend 配置。"""

    try:
        _, document = load_toml_document(config_path, "模型配置")
    except TomlDocumentError as error:
        raise ModelConfigurationError(str(error)) from error

    model_data = document.get("model")
    if not isinstance(model_data, dict):
        raise ModelConfigurationError("配置文件缺少 [model] 节。")

    try:
        reject_unknown_keys(model_data, _MODEL_KEYS, "model")
        thinking_mode = strip_to_none(
            read_optional_string(
                model_data,
                "thinking_mode",
                "model",
                allow_empty=True,
            )
        )
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError(
                "model.thinking_mode 只能是 enabled、disabled 或空字符串。"
            )

        return ModelBackendConfig(
            provider=read_string(
                model_data,
                "provider",
                "openai-compatible",
                "model",
            ),
            base_url=read_required_string(
                model_data,
                "base_url",
                "model",
            ).rstrip("/"),
            api_key=read_string(
                model_data,
                "api_key",
                "",
                "model",
                allow_empty=True,
            ),
            model=read_required_string(model_data, "model", "model"),
            timeout_seconds=read_float(
                model_data,
                "timeout_seconds",
                120.0,
                "model",
                positive=True,
            ),
            max_retries=read_int(
                model_data,
                "max_retries",
                2,
                "model",
            ),
            temperature=read_optional_float(
                model_data,
                "temperature",
                "model",
            ),
            max_tokens=read_optional_int(
                model_data,
                "max_tokens",
                "model",
                positive=True,
            ),
            stream_include_usage=read_bool(
                model_data,
                "stream_include_usage",
                True,
                "model",
            ),
            thinking_mode=thinking_mode,
            reasoning_effort=strip_to_none(
                read_optional_string(
                    model_data,
                    "reasoning_effort",
                    "model",
                    allow_empty=True,
                )
            ),
            # Provider 专属 extra_body 允许任意内部键，仅校验它本身是 TOML 表。
            extra_body=dict(
                read_table(
                    model_data,
                    "extra_body",
                    "model",
                    required=False,
                )
            ),
        )
    except (TypeError, ValueError) as error:
        raise ModelConfigurationError(f"模型配置不合法：{error}") from error
