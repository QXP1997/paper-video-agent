"""Skill 内可独立运行的 PptxGenJS 命令行入口。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """使用 QHarness 注入的 Node 执行同目录渲染器。"""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("用法: render_pptx.py <deck_spec.json> <output.pptx>", file=sys.stderr)
        return 2
    node = os.environ.get("RUNTIME_NODE") or shutil.which("node")
    if not node:
        print("找不到 Node.js；请由 QHarness 注入托管 Node。", file=sys.stderr)
        return 2
    modules = os.environ.get("RUNTIME_NODE_MODULES") or os.environ.get(
        "PPTXGENJS_MODULES"
    )
    if not modules:
        print("找不到托管 PptxGenJS；请注入 RUNTIME_NODE_MODULES。", file=sys.stderr)
        return 2
    script = Path(__file__).with_name("pptx_renderer.mjs")
    completed = subprocess.run(
        [node, str(script), arguments[0], arguments[1]],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
