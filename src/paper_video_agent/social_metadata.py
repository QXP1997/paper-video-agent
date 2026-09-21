"""Generate platform-ready publishing copy from a finished PaperScript.

This is intentionally a separate post-processing step. It sends a compact
summary of the script to the LLM instead of the full narration, then writes
machine-readable JSON and a copy-friendly Markdown file beside the video
outputs.
"""

from __future__ import annotations

import argparse
import json
import os
from functools import lru_cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from paper_video_agent.chat import get_llm, invoke_structured_with_retry
from paper_video_agent.models import PaperScript


class SocialMetadata(BaseModel):
    title: str = Field(
        min_length=1,
        max_length=50,
        description="适合多个平台共用的中文视频标题",
    )
    description: str = Field(
        min_length=1,
        max_length=450,
        description="适合多个平台共用的视频简介",
    )
    tags: list[str] = Field(min_length=5, max_length=12)
    core_summary: str = Field(
        min_length=1,
        max_length=300,
        description="用通俗中文概括论文做了什么以及最主要的结论",
    )
    key_conclusion: str = Field(
        min_length=1,
        max_length=500,
        description="论文最重要的结论，必须同时说明适用边界或限制",
    )


metadata_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
你是一名负责科技内容发布的中文编辑。请根据论文精读视频脚本摘要，生成一份可以跨平台复用的标题、描述和标签。

核心要求：
1. 必须先说清楚这篇论文在解决什么问题、用了什么方法、得到什么结论，不能只罗列论文术语。
2. 只使用输入中明确出现的信息，不要补充作者、机构、数据、实验结果或应用场景。
3. 标题要准确、有吸引力，但不能夸大成“吊打所有模型”“彻底解决”等标题党。
4. 描述要适合直接发布：先用通俗语言说明论文做了什么，再概括核心方法和结果，最后说明限制或解读前提。
5. 标题控制在 30 个汉字以内，既清楚又有吸引力，不要写成平台专属风格。
6. 描述控制在 250 至 350 个汉字以内，适合抖音、小红书等短视频平台；只保留问题、方法、结果和限制，不要展开章节细节。
7. 标签使用纯文本短词，不要带 #，不要重复，不要生成“爆款”“推荐”等空泛标签。
8. 输出必须严格符合给定结构。
""",
    ),
    (
        "human",
        """
下面是从完整视频脚本中提取的摘要，不是整篇论文原文：

{script_summary}
""",
    ),
])


@lru_cache(maxsize=1)
def _get_metadata_chain():
    return metadata_prompt | get_llm().with_structured_output(
        SocialMetadata,
        method="function_calling",
        include_raw=True,
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"


def build_script_summary(script: PaperScript) -> dict:
    """Select high-signal fields without sending the full narration."""
    plan = script.plan
    chapters = []
    for chapter in script.chapters:
        segments = chapter.segments
        chapters.append({
            "title": chapter.title,
            "segment_count": len(segments),
            "sample_narration": [
                _clip(segments[0].text, 260),
                *(
                    [_clip(segments[-1].text, 260)]
                    if len(segments) > 1
                    else []
                ),
            ],
        })

    return {
        "video_title": script.title,
        "planned_title": plan.video_title,
        "core_message": _clip(plan.core_message, 700),
        "central_question": _clip(plan.central_question, 500),
        "story_spine": [_clip(item, 320) for item in plan.story_spine],
        "opening_hook": _clip(plan.opening_hook, 400),
        "chapters": chapters,
    }


def _markdown(metadata: SocialMetadata, source_path: Path) -> str:
    tags = " ".join(f"#{tag.lstrip('#')}" for tag in metadata.tags)
    return (
        f"# 发布文案：{source_path.stem}\n\n"
        f"**标题**：{metadata.title}\n\n"
        f"**描述**：\n{metadata.description}\n\n"
        f"**标签**：{tags}\n\n"
        f"**核心概括**：{metadata.core_summary}\n\n"
        f"**关键结论**：{metadata.key_conclusion}\n\n"
    )


def generate_social_metadata(
    script_path: str | Path,
    output_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    script_path = Path(script_path)
    data = json.loads(script_path.read_text(encoding="utf-8"))
    script = PaperScript.model_validate(data)
    summary = build_script_summary(script)

    metadata = invoke_structured_with_retry(
        _get_metadata_chain(),
        {"script_summary": json.dumps(summary, ensure_ascii=False, indent=2)},
        stage="发布文案",
    )

    output_dir = Path(output_dir) if output_dir else script_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "social_metadata.json"
    markdown_path = output_dir / "social_metadata.md"

    json_path.write_text(
        json.dumps(metadata.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(
        _markdown(metadata, script_path),
        encoding="utf-8",
    )
    return json_path, markdown_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="根据 paper_script.json 生成统一发布文案",
    )
    parser.add_argument(
        "--script-json",
        type=Path,
        required=True,
        help="视频生成完成后的 paper_script.json 路径",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="输出目录，默认写回 paper_script.json 所在的 output 目录",
    )
    args = parser.parse_args()

    script_path = args.script_json.expanduser().resolve()
    if not script_path.is_file():
        parser.error(f"脚本文件不存在: {script_path}")
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        parser.error("未配置 DEEPSEEK_API_KEY，请复制 .env.example 为 .env 后填写")

    json_path, markdown_path = generate_social_metadata(
        script_path,
        args.output_dir.expanduser().resolve() if args.output_dir else None,
    )
    print(f"发布文案 JSON 已生成: {json_path}")
    print(f"发布文案 Markdown 已生成: {markdown_path}")


if __name__ == "__main__":
    main()
