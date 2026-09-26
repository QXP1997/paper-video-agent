"""调用受控 Node 运行时，将 SlideDeckSpec 渲染为可编辑 PPTX。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from research_presentation_core.models import SlideDeckSpec


class PptxRenderError(RuntimeError):
    """PPTX 渲染器不可用或渲染失败。"""


def render_pptx(
    spec_path: str | Path,
    output_path: str | Path,
    *,
    node_executable: str | Path | None = None,
    node_modules: str | Path | None = None,
    keep_build: bool = False,
) -> dict[str, Any]:
    """根据已校验的 Deck Spec 生成 PPTX、预览和渲染报告。

    运行时优先使用显式参数，其次使用 ``RUNTIME_NODE`` 和
    ``RUNTIME_NODE_MODULES``。如果最终输出已经存在，不会覆盖。
    """
    source = Path(spec_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if source.suffix.lower() != ".json" or not source.is_file():
        raise PptxRenderError(f"Deck Spec 不存在: {source}")
    if output.suffix.lower() != ".pptx":
        raise PptxRenderError("输出路径必须使用 .pptx 扩展名")
    if output.exists():
        raise PptxRenderError(f"PPTX 已存在，为避免覆盖已拒绝: {output}")
    preview_output = output.parent / f"{output.stem}.preview"
    if preview_output.exists():
        raise PptxRenderError(f"预览目录已存在，为避免覆盖已拒绝: {preview_output}")
    try:
        spec = json.loads(source.read_text(encoding="utf-8"))
        SlideDeckSpec.model_validate(spec)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PptxRenderError(f"Deck Spec 未通过校验: {source}") from error

    node = Path(node_executable or os.environ.get("RUNTIME_NODE", "") or shutil.which("node") or "")
    if not node.is_file():
        raise PptxRenderError("找不到 Node.js，请配置 RUNTIME_NODE 或显式传入 node_executable")
    modules = _resolve_node_modules(node, node_modules)
    if modules is None:
        raise PptxRenderError(
            "找不到 PptxGenJS，请配置 RUNTIME_NODE_MODULES/PPTXGENJS_MODULES，"
            "或将其安装到 QHarness 托管 Node 的 node_modules 目录"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="presentation-render-", dir=source.parent))
    candidate = build / "candidate.pptx"
    report = build / "render-report.json"
    preview = preview_output
    published = False
    try:
        environment = os.environ.copy()
        environment.update({
            "DECK_SPEC_PATH": str(source),
            "PPTX_CANDIDATE_PATH": str(candidate),
            "PPTX_PREVIEW_DIR": str(preview),
            "PPTX_REPORT_PATH": str(report),
            "RUNTIME_NODE_MODULES": str(modules),
        })
        script = Path(
            os.environ.get("PRESENTATION_RENDERER_SCRIPT", "")
            or Path(__file__).parent
            / "skills"
            / "create-presentation"
            / "scripts"
            / "pptx_renderer.mjs"
        ).expanduser().resolve()
        if not script.is_file():
            raise PptxRenderError(f"找不到 Skill 内的 PptxGenJS 渲染脚本: {script}")
        completed = subprocess.run(
            [str(node), str(script)],
            cwd=str(source.parent),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise PptxRenderError(f"PPTX 渲染失败: {detail[-4000:]}")
        if not candidate.is_file() or candidate.stat().st_size < 1:
            raise PptxRenderError("PPTX 渲染器没有生成有效候选文件")
        if output.exists():
            raise PptxRenderError(f"PPTX 在发布前已被其他进程创建: {output}")
        try:
            os.link(candidate, output)
        except FileExistsError as error:
            raise PptxRenderError(f"PPTX 已存在，拒绝覆盖: {output}") from error
        except OSError:
            try:
                with candidate.open("rb") as source_file, output.open("xb") as target_file:
                    shutil.copyfileobj(source_file, target_file)
            except FileExistsError as error:
                raise PptxRenderError(f"PPTX 已存在，拒绝覆盖: {output}") from error
        if report.is_file():
            result = json.loads(report.read_text(encoding="utf-8"))
        else:
            result = {}
        result.update({
            "output_path": str(output),
            "candidate_bytes": output.stat().st_size,
            "preview_dir": str(preview),
        })
        published = True
        return result
    except Exception:
        if not published:
            shutil.rmtree(preview, ignore_errors=True)
        raise
    finally:
        if not keep_build:
            shutil.rmtree(build, ignore_errors=True)


def _resolve_node_modules(
    node: Path,
    explicit_modules: str | Path | None,
) -> Path | None:
    """查找 PptxGenJS；优先使用沙箱传入的托管依赖目录。"""

    configured = [
        explicit_modules,
        os.environ.get("RUNTIME_NODE_MODULES"),
        os.environ.get("PPTXGENJS_MODULES"),
    ]
    candidates = [Path(value) for value in configured if value]
    # QHarness 可以把 npm 包安装在托管 Node 目录旁；Codex 运行时则把它放在
    # node.exe 的上两级 dependencies/node/node_modules 下。只向上查有限层级。
    parent = node.parent
    for _ in range(4):
        candidates.append(parent / "node_modules")
        parent = parent.parent
    for candidate in candidates:
        entry = candidate / "pptxgenjs" / "dist" / "pptxgen.es.js"
        if entry.is_file():
            return candidate.resolve()
    return None


def main(argv: tuple[str, ...] | None = None) -> int:
    """命令行入口：``research-pptx spec.json output.pptx``。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="将 SlideDeckSpec 渲染为 PPTX")
    parser.add_argument("spec_path")
    parser.add_argument("output_path")
    parser.add_argument("--node")
    parser.add_argument("--node-modules")
    parser.add_argument("--keep-build", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = render_pptx(
            args.spec_path,
            args.output_path,
            node_executable=args.node,
            node_modules=args.node_modules,
            keep_build=args.keep_build,
        )
    except PptxRenderError as error:
        parser.error(str(error))
    # Windows 默认控制台常使用 GBK；报告包含中文和项目符号时不能直接编码。
    # 尽量切换到 UTF-8，无法切换时也要保证 CLI 不因打印结果而误报失败。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
