"""SlideDeckSpec 的确定性校验与 JSON Schema 导出。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from research_presentation_core.models import SlideDeckSpec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-deck")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="校验一个 SlideDeckSpec JSON 文件")
    validate.add_argument("path", type=Path)
    commands.add_parser("schema", help="输出 SlideDeckSpec JSON Schema")
    return parser


def validate_file(path: Path) -> SlideDeckSpec:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return SlideDeckSpec.model_validate(data)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "schema":
        print(json.dumps(SlideDeckSpec.model_json_schema(), ensure_ascii=False, indent=2))
        return 0

    try:
        deck = validate_file(args.path)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
        print(
            json.dumps(
                {"valid": False, "path": str(args.path), "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "path": str(args.path),
                "slides": len(deck.slides),
                "assets": len(deck.assets),
                "sources": len(deck.sources),
            },
            ensure_ascii=False,
        )
    )
    return 0
