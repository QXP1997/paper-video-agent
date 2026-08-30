# -*- coding: utf-8 -*-
"""示例六：创建工作区并验证安全路径边界。"""

from __future__ import annotations

from _common import PROJECT_ROOT
from qharness.exception import WorkspacePathError
from qharness.workspace import WorkspaceContext


def main() -> None:
    """演示合法文件、工作区根目录和越界路径的处理结果。"""

    workspace = WorkspaceContext(PROJECT_ROOT)
    print(f"工作区根目录：{workspace.root}")

    readme_path = workspace.resolve_file("README.md")
    print(f"合法文件：{workspace.relative_path(readme_path)}")

    root_directory = workspace.resolve_directory(".")
    print(f"合法目录：{workspace.relative_path(root_directory)}")

    future_file = workspace.resolve_path(
        "artifacts/future-result.txt",
        must_exist=False,
    )
    print(f"允许创建的工作区路径：{workspace.relative_path(future_file, must_exist=False)}")

    try:
        workspace.resolve_directory(PROJECT_ROOT.parent)
    except WorkspacePathError as error:
        print(f"已阻止越界访问：{error}")


if __name__ == "__main__":
    main()
