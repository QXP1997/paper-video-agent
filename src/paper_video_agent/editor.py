import json
from functools import lru_cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

from paper_video_agent.chat import (
    LLM_BASE_URL,
    LLM_EXTRA_BODY,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    get_llm,
    invoke_structured_with_retry,
)
from paper_video_agent.models import (
    EditedScriptChapters,
    PaperScript,
    PaperScriptAudit,
)
from research_agent_core.artifacts import (
    canonical_sha256 as _canonical_sha256,
)
from research_agent_core.artifacts import (
    write_json_atomic as _write_json_atomic,
)
from research_agent_core.prompts import prompt_messages as _prompt_messages

EDITOR_GENERATION_VERSION = 1
EDITOR_CACHE_VERSION = 1


editor_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
你是一名资深中文科技视频编辑。你的任务是在不重新阅读论文、不引入新事实的前提下，把一份已经完成事实审核的论文解说稿编辑成更准确、清楚、紧凑、适合口播的版本。

输入包括完整 raw_script 和对应的 fact_audit。raw_script 提供全局叙事、章节和原始口播；fact_audit 是当前唯一可用的论文证据结论。不要调用外部知识，不要猜测审核没有提供的信息，也不要声称自己重新核对过论文原文。

# 一、事实审核意见的处理

对审核出的每个原子陈述按以下规则处理：

- supported：可以保留，但仍要根据叙事价值决定是否需要说；
- partially_supported：补上审核指出的关键条件、范围、比较对象或谨慎措辞，也可以在不影响主线时删除；
- unsupported：不得继续作为论文事实陈述。优先删除；只有审核提供了明确、可用的 suggested_revision 时才可按建议最小修正；
- conflicting：不要静默选择某个数字或版本。若冲突本身不影响理解则删除精确值；确有必要时只客观说明论文给出了不一致信息；
- not_verifiable：不得把它当成已确认事实。优先删除，必要时明确降低确定性，但不能用模糊措辞掩盖核心事实缺乏依据；
- derived：只有审核 evidence 写清了计算依据时才能保留，并用普通语言说明它是根据原文数据算出的结果，而不是作者直接报告的结论。

编辑稿中不得出现“审核结果显示”“审核节点认为”等流程元信息。不要为了修复一句话而新增审核记录中没有的方法、数字、原因、作者意图或结论。

# 二、全局信息取舍

先从 raw_script.plan 的 central_question、core_message、story_spine 和各章 guiding_question 判断主线，再编辑所有章节。精讲的目标是让观众理解，而不是把论文每个细节都念出来。

优先保留：

1. 论文要解决的问题及其重要性；
2. 现有做法的关键困难；
3. 作者方案的整体思路和真正重要的机制；
4. 能支撑核心主张的代表性证据；
5. 负面结果、适用条件和局限；
6. 对实践或后续研究真正有帮助的含义。

优先压缩或删除：

- 对理解核心贡献没有帮助的实现清单、超参数、数据配比和次要实验；
- 连续定义大量术语、逐条复述公式或逐行朗读表格；
- “这一章我们将……”“到这里我们已经……”等模板化开场、总结和转场；
- 在多个章节中重复解释的背景、方法、结果和结论；
- 只是换一种说法再次表达同一观点的句子；
- 为显得完整而添加、但没有推进 central_question 的内容。

跨章节去重时保留最适合首次完整解释该概念的位置。后文确实需要承接时，只用一句短提示，不要重新讲一遍。如果一个章节与前文高度重复，删除重复内容，只保留该章回答自身 guiding_question 所必需且尚未讲过的信息；整章没有独立价值时可以删除该章节。

# 三、数字与实验

- 不朗读完整表格，不连续罗列 benchmark、模型名和分数；
- 每个实验结论通常只保留一至两个最能支撑主张的数字；
- 整期视频只保留观众理解贡献或边界真正需要记住的关键数字，不机械追求固定数量；
- 没必要体现精度时可以自然地说“大约”“接近”“超过”，但不能改变方向、量级、单位或统计含义；
- 必须区分百分比、百分点、倍数、绝对提升和相对提升；
- 先说明指标或比较在回答什么问题，再给数字和它意味着什么；
- 多个结果支持同一结论时，选代表性结果并概括其余趋势。

# 四、口播表达

- 使用自然、准确的中文短句，一个 segment 聚焦一个主要意思；
- 先讲直觉和作用，再给必要术语；不要用更多陌生名词解释陌生名词；
- 合并被切得过碎、背景页相同且逻辑连续的 segments；过长且包含多个主要意思的 segment 可以拆分；
- 避免论文腔、宣传腔、空泛评价、反复设问和机械总结；
- 保持原稿的受众水平，不因压缩而省掉理解核心方法所必需的因果链；
- 保留论文原有的不确定性和边界，不把“可能”“表明”“相关”升级成确定因果；
- 不以缩短为唯一目标，没有重复或低价值的信息不应仅因篇幅被删掉。

# 五、结构和页面约束

你只输出 EditedScriptChapters：

- chapters 必须按 raw_script 中的原顺序排列；
- chapter_id 只能使用 raw_script 已有的 ID，不得新建、改名或重复；
- 可以删除完全没有独立价值的章节，但至少保留四章；
- 保留章节时，其 title 必须与 raw_script 对应章节一致；
- 每个保留章节至少包含一个 segment；
- segment.page 只能使用该章节原有 segment.page 或该章 source_pages 中的页码；
- segment.visual_id 只能保留 raw_script.visuals 中存在且与 segment.page 同页的 ID；没有明确视觉焦点时为 null；
- 页面只是画面选择。合并 segments 时选择其中最能代表当前内容的一页，不要为了切页而重复口播；
- 不输出时间戳、时长、审核说明、Markdown 或任何额外字段。

输出前从头通读编辑稿，确认章节之间没有明显重复、事实问题已按审核处理，并且删减后仍能回答 central_question、讲清核心方案、主要证据和必要边界。
""",
    ),
    (
        "human",
        """
请编辑下面的完整论文视频口播稿。

原始脚本：

{raw_script}

事实审核：

{fact_audit}

上一次输出的结构或约束错误（首次调用为空）：

{validation_feedback}
""",
    ),
])


@lru_cache(maxsize=1)
def _get_editor_chain():
    return editor_prompt | get_llm().with_structured_output(
        EditedScriptChapters,
        method="function_calling",
        include_raw=True,
    )


def editor_generation_cache_material() -> dict:
    """Return deterministic, non-secret inputs that affect edited scripts."""
    return {
        "generation_version": EDITOR_GENERATION_VERSION,
        "llm": {
            "model": LLM_MODEL,
            "base_url": LLM_BASE_URL,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "extra_body": LLM_EXTRA_BODY,
        },
        "prompts": {
            "editor": _prompt_messages(editor_prompt),
        },
        "schemas": {
            "edited_script_chapters": EditedScriptChapters.model_json_schema(),
            "paper_script": PaperScript.model_json_schema(),
        },
    }


def _normalize_edited_script(
    result: EditedScriptChapters,
    raw_script: PaperScript,
) -> PaperScript:
    raw_chapters_by_id = {
        chapter.chapter_id: chapter
        for chapter in raw_script.chapters
    }
    plans_by_id = {
        chapter.chapter_id: chapter
        for chapter in raw_script.plan.chapters
    }
    expected_order = [chapter.chapter_id for chapter in raw_script.chapters]
    actual_ids = [chapter.chapter_id for chapter in result.chapters]

    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("编辑稿包含重复 chapter_id")
    if len(actual_ids) < 4:
        raise ValueError("编辑稿至少需要保留四个章节")
    if any(chapter_id not in raw_chapters_by_id for chapter_id in actual_ids):
        raise ValueError("编辑稿包含原始脚本中不存在的 chapter_id")
    if actual_ids != [
        chapter_id for chapter_id in expected_order if chapter_id in actual_ids
    ]:
        raise ValueError("编辑稿章节顺序与原始脚本不一致")

    normalized_chapters = []
    visual_pages = {visual.id: visual.page for visual in raw_script.visuals}
    for edited_chapter in result.chapters:
        raw_chapter = raw_chapters_by_id[edited_chapter.chapter_id]
        chapter_plan = plans_by_id.get(edited_chapter.chapter_id)
        if chapter_plan is None:
            raise ValueError(
                f"编辑稿章节缺少对应规划: {edited_chapter.chapter_id}"
            )

        allowed_pages = {
            segment.page for segment in raw_chapter.segments
        } | set(chapter_plan.source_pages)
        invalid_pages = sorted({
            segment.page
            for segment in edited_chapter.segments
            if segment.page not in allowed_pages
        })
        if invalid_pages:
            raise ValueError(
                f"编辑稿章节“{raw_chapter.title}”使用了不允许的页码: "
                f"{invalid_pages}"
            )
        invalid_visual_ids = sorted({
            segment.visual_id
            for segment in edited_chapter.segments
            if segment.visual_id is not None
            and (
                segment.visual_id not in visual_pages
                or visual_pages[segment.visual_id] != segment.page
            )
        })
        if invalid_visual_ids:
            raise ValueError(
                f"编辑稿章节“{raw_chapter.title}”使用了无效视觉元素: "
                f"{invalid_visual_ids}"
            )

        normalized_chapters.append(edited_chapter.model_copy(update={
            "title": raw_chapter.title,
        }))

    kept_ids = set(actual_ids)
    edited_plan = raw_script.plan.model_copy(update={
        "chapters": [
            chapter
            for chapter in raw_script.plan.chapters
            if chapter.chapter_id in kept_ids
        ],
    })
    return PaperScript(
        title=raw_script.title,
        plan=edited_plan,
        chapters=normalized_chapters,
        visuals=raw_script.visuals,
    )


def generate_edited_script(
    raw_script: PaperScript,
    audit: PaperScriptAudit,
    max_attempts: int = 3,
) -> PaperScript:
    """Globally edit a script using its evidence-bound fact audit."""
    if audit.script_title != raw_script.title:
        raise ValueError("事实审核与原始脚本标题不匹配")
    raw_chapter_ids = [chapter.chapter_id for chapter in raw_script.chapters]
    audit_chapter_ids = [chapter.chapter_id for chapter in audit.chapters]
    if audit_chapter_ids != raw_chapter_ids:
        raise ValueError("事实审核没有按顺序完整覆盖原始脚本章节")

    values = {
        "raw_script": raw_script.model_dump_json(indent=2),
        "fact_audit": audit.model_dump_json(indent=2),
        "validation_feedback": "",
    }
    errors = []

    for attempt in range(1, max_attempts + 1):
        try:
            result = invoke_structured_with_retry(
                chain=_get_editor_chain(),
                values=values,
                stage="论文口播全局编辑",
                max_attempts=1,
            )
            return _normalize_edited_script(result, raw_script)
        except Exception as exc:
            errors.append(f"第 {attempt} 次: {type(exc).__name__}: {exc}")
            values["validation_feedback"] = (
                "请修正下列错误并重新输出完整编辑稿：\n"
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < max_attempts:
                print(
                    "论文口播编辑结果不完整，"
                    f"正在重试 {attempt + 1}/{max_attempts}..."
                )

    raise RuntimeError(
        f"论文口播连续 {max_attempts} 次编辑失败\n"
        + "\n".join(errors)
    )


def build_editor_cache_metadata(
    raw_script: PaperScript,
    audit: PaperScriptAudit,
) -> dict:
    """Fingerprint only inputs that can change the edited script."""
    inputs = {
        "paper_script_sha256": _canonical_sha256(raw_script.model_dump()),
        "script_audit_sha256": _canonical_sha256(audit.model_dump()),
        "editor_generation_sha256": _canonical_sha256(
            editor_generation_cache_material()
        ),
    }
    return {
        "version": EDITOR_CACHE_VERSION,
        "fingerprint": _canonical_sha256(inputs),
        "inputs": inputs,
    }


def load_cached_edited_script(
    script_path: str | Path,
    cache_path: str | Path,
    expected_cache: dict,
) -> PaperScript | None:
    script_path = Path(script_path)
    cache_path = Path(cache_path)
    if not script_path.is_file() or not cache_path.is_file():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cache.get("version") != EDITOR_CACHE_VERSION
            or cache.get("fingerprint") != expected_cache["fingerprint"]
        ):
            return None
        return PaperScript.model_validate_json(
            script_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_edited_script(
    script: PaperScript,
    output_path: str | Path,
    cache_path: str | Path | None = None,
    cache_metadata: dict | None = None,
) -> None:
    _write_json_atomic(Path(output_path), script.model_dump())
    if cache_path is not None and cache_metadata is not None:
        _write_json_atomic(Path(cache_path), cache_metadata)
