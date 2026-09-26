"""使用 QHarness 注入的托管 Node 校验 SlideDeckSpec。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("用法: validate_spec.py <deck_spec.json>", file=sys.stderr)
        return 2
    node = os.environ.get("RUNTIME_NODE") or shutil.which("node")
    if not node:
        print("找不到 Node.js；请由 QHarness 注入托管 Node。", file=sys.stderr)
        return 2
    completed = subprocess.run(
        [node, str(Path(__file__).with_name("validate_spec.mjs")), arguments[0]],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
