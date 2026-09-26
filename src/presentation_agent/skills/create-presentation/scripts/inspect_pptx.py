"""使用 Python 标准库检查 PPTX 是否为基本完整的 Open XML 包。"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("用法: inspect_pptx.py <deck.pptx>", file=sys.stderr)
        return 2
    target = Path(arguments[0]).resolve()
    if target.suffix.lower() != ".pptx" or not target.is_file():
        print(f"PPTX 不存在: {target}", file=sys.stderr)
        return 1
    try:
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            missing = sorted(required - names)
            if missing:
                print(f"PPTX 缺少必要条目: {', '.join(missing)}", file=sys.stderr)
                return 1
            slide_count = sum(
                name.startswith("ppt/slides/slide") and name.endswith(".xml") for name in names
            )
    except (OSError, zipfile.BadZipFile) as error:
        print(f"PPTX 无法读取: {error}", file=sys.stderr)
        return 1
    if slide_count < 1:
        print("PPTX 中没有幻灯片", file=sys.stderr)
        return 1
    print(
        json.dumps({"valid": True, "path": str(target), "slides": slide_count}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
