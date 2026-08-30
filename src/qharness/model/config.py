# -*- coding: utf-8 -*-
"""模型 Backend 的动态配置读取模块。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qharness.exception.error import ModelConfigurationError
from qharness.utils.text import strip_to_none


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
    """
    获取模型配置信息
    """

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ModelConfigurationError(f"模型配置文件不存在：{path}")

    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ModelConfigurationError(
            f"模型配置文件格式错误：{error}"
        ) from error

    model_data = document.get("model")
    if not isinstance(model_data, dict):
        raise ModelConfigurationError("配置文件缺少 [model] 节。")

    provider = str(model_data.get("provider", "openai-compatible")).strip()
    base_url = str(model_data.get("base_url", "")).strip()
    model = str(model_data.get("model", "")).strip()

    if not base_url:
        raise ModelConfigurationError("model.base_url 不能为空。")
    if not model:
        raise ModelConfigurationError("model.model 不能为空。")

    api_key = str(model_data.get("api_key", "")).strip()

    thinking_mode = strip_to_none(model_data.get("thinking_mode"))
    if thinking_mode not in {None, "enabled", "disabled"}:
        raise ModelConfigurationError(
            "model.thinking_mode 只能是 enabled、disabled 或空字符串。"
        )

    extra_body = model_data.get("extra_body", {})
    if not isinstance(extra_body, dict):
        raise ModelConfigurationError("model.extra_body 必须是 TOML 表。")

    return ModelBackendConfig(
        provider=provider,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        timeout_seconds=float(model_data.get("timeout_seconds", 120.0)),
        max_retries=int(model_data.get("max_retries", 2)),
        temperature=(
            float(model_data["temperature"])
            if "temperature" in model_data
            else None
        ),
        max_tokens=(
            int(model_data["max_tokens"])
            if "max_tokens" in model_data
            else None
        ),
        stream_include_usage=bool(model_data.get("stream_include_usage", True)),
        thinking_mode=thinking_mode,
        reasoning_effort=strip_to_none(model_data.get("reasoning_effort")),
        extra_body=dict(extra_body),
    )
