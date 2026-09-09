# -*- coding: utf-8 -*-
"""示例六：创建工作区并验证安全路径边界。"""

from __future__ import annotations

import logging

from _common import PROJECT_ROOT
from qharness.exception import WorkspacePathError
from qharness.logging import configure_logging
from qharness.workspace import WorkspaceContext


_LOGGER = logging.getLogger("qharness.examples.workspace")


def main() -> None:
    """演示合法文件、工作区根目录和越界路径的处理结果。"""

    configure_logging()
    workspace = WorkspaceContext(PROJECT_ROOT)
    _LOGGER.info("工作区根目录：%s", workspace.root)

    readme_path = workspace.resolve_file("README.md")
    _LOGGER.info("合法文件：%s", workspace.relative_path(readme_path))

    root_directory = workspace.resolve_directory(".")
    _LOGGER.info("合法目录：%s", workspace.relative_path(root_directory))

    future_file = workspace.resolve_path(
        "artifacts/future-result.txt",
        must_exist=False,
    )
    _LOGGER.info(
        "允许创建的工作区路径：%s",
        workspace.relative_path(future_file, must_exist=False),
    )

    try:
        workspace.resolve_directory(PROJECT_ROOT.parent)
    except WorkspacePathError as error:
        _LOGGER.info("已阻止越界访问：%s", error)


if __name__ == "__main__":
    main()
