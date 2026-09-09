# -*- coding: utf-8 -*-
"""QHarness 统一日志配置。

业务模块只负责通过 ``logging.getLogger(__name__)`` 记录日志；应用入口调用
``configure_logging`` 决定日志级别、控制台输出和轮转文件。这样既不会把
``print`` 混入生产日志，也不会强迫将某个第三方日志框架传遍所有业务对象。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOGGER_NAME = "qharness"
_MANAGED_HANDLER_ATTRIBUTE = "_qharness_managed_handler"
_DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 5


def configure_logging(
    *,
    level: int | str = logging.INFO,
    log_file: str | Path | None = None,
    console: bool = True,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """配置 QHarness 命名空间下的控制台日志和轮转文件日志。

    重复调用是安全的：本函数只替换自己创建的 Handler，不会修改宿主应用的
    根 Logger。日志文件始终使用 UTF-8；达到 ``max_file_bytes`` 后最多保留
    ``backup_count`` 个历史文件。
    """

    normalized_level = _normalize_level(level)
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes 必须大于 0。")
    if backup_count < 0:
        raise ValueError("backup_count 不能小于 0。")

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(normalized_level)
    # QHarness 自己管理 Handler，避免日志再传播到根 Logger 后被重复打印。
    logger.propagate = False

    for handler in tuple(logger.handlers):
        if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s.%(msecs)03d | %(levelname)-8s | "
            "%(name)s | %(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(normalized_level)
        console_handler.setFormatter(formatter)
        setattr(console_handler, _MANAGED_HANDLER_ATTRIBUTE, True)
        logger.addHandler(console_handler)

    if log_file is not None:
        file_path = Path(log_file).expanduser().resolve(strict=False)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=max_file_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(normalized_level)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _MANAGED_HANDLER_ATTRIBUTE, True)
        logger.addHandler(file_handler)

    return logger


def _normalize_level(level: int | str) -> int:
    """把数字或文本日志级别统一转换为 logging 使用的整数。"""

    if isinstance(level, int):
        return level
    normalized = logging.getLevelName(level.strip().upper())
    if not isinstance(normalized, int):
        raise ValueError(f"无效的日志级别：{level}")
    return normalized
