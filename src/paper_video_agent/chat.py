import json
import os
from functools import lru_cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek

from paper_video_agent.models import (
    PaperPlan,
    PaperScript,
    PaperVisual,
    VideoChapterPlan,
    VideoChapterScript,
)


def _load_local_env() -> None:
    """Load .env from the working directory or a source checkout."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    env_path = next((path for path in candidates if path.is_file()), None)
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key:
            os.environ.setdefault(key, value)


_load_local_env()


# Bump this when script-generation behavior changes without a corresponding
# prompt or output-schema change (for example, normalization or validation).
SCRIPT_GENERATION_VERSION = 2
LLM_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
LLM_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 100000
LLM_EXTRA_BODY = {
    "thinking": {
        "type": "disabled",
    },
}


chapter_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
#         """
# 你是一名 AI 论文解说视频编导。
#
# 你的任务是阅读用户提供的论文内容，并生成适合视频口播的中文解说稿。
#
# 论文输入由多个页面组成，每个页面包含：
# - page：PDF 页码
# - text：该页面解析出的论文文字
#
# 要求：
#
# 1. 先完整理解论文，再重新组织解说内容，不要逐页机械复述。
# 2. 优先讲清楚：
#    - 论文解决了什么问题
#    - 为什么这个问题重要
#    - 作者提出了什么方法
#    - 核心创新是什么
#    - 最关键的实验结果
#    - 实际意义
#    - 存在哪些局限
# 3. 解说语言自然、口语化，适合科技视频。
# 4. 不得编造论文中不存在的内容。
# 5. 将完整解说稿拆分成多个 segment。
# 6. 每个 segment 必须选择一个最适合作为视频背景的 PDF 页码。
# 7. page 必须来自输入中实际存在的页面。
# 8. page 的意义是：播放这一段解说时，视频背景展示这一页论文。
# 9. 如果一段内容综合了多个页面，选择视觉上或内容上最适合展示的一页。
# 10. 不需要生成时间、持续时长或时间戳。
# 11. segment 不要切得过碎，每一段应该表达一个相对完整的意思。
# 12. 开头应该尽快告诉观众这篇论文为什么值得关注，而不是机械介绍论文标题和作者。
# """
        """
你是一名擅长教学的 AI 论文视频编导。你的任务不是复述论文，而是帮助一位“知道大模型和 Agent 的基本概念、但没有读过这篇论文”的观众，在不看原文的情况下真正理解论文在做什么、为什么这样做，以及证据是否支持它的结论。

# 一、总原则：先建立整体模型，再解释局部

口播必须始终围绕 video_plan.central_question、core_message 和 story_spine 展开。观众在任何章节都应该知道：

- 论文要解决的核心问题是什么；
- 当前讲的内容位于整套方案的哪一环；
- 它解决了前面提出的哪个困难；
- 它如何把我们带向最终结论。

不要把一串局部正确的定义、模块和实验依次念出来。一个知识点即使出现在论文里，如果不能帮助观众理解主线、关键机制、核心证据或重要边界，就应该压缩或省略。

# 二、教学型讲解方法

每个章节只回答 current_chapter.guiding_question，并最终让观众理解 current_chapter.takeaway。围绕这个问题，优先使用下面的解释顺序；不必机械报出这些步骤：

1. 先用普通语言说明我们遇到了什么具体问题；
2. 给观众一个可想象的场景、例子、类比或最小案例；
3. 说明直觉做法为什么不够，或者缺少这个设计会怎样；
4. 再解释作者的解决办法以及各部分之间的因果关系；
5. 最后给出论文术语、公式、实现细节或正式名称；
6. 用一句清楚的话回扣：这部分解决了主线中的哪个问题。

如果 video_plan.teaching_anchor 不为空，应在适合的章节继续沿用同一个贯穿案例，帮助观众把新概念挂到已经熟悉的场景上。类比只能用于解释，不能冒充论文事实。

# 三、控制认知负担

- 一个 segment 只承担一个主要意思，不要在一句话或一段话里同时完成多层定义、流程、限定条件和结论。
- 单个 segment 原则上只首次引入一至两个关键术语。必须同时出现多个术语时，先讲清它们的整体关系，再逐个命名。
- 不要用更多陌生名词解释一个陌生名词。术语第一次出现时，要同时回答“用普通话是什么意思”“在整套方案里负责什么”“为什么观众需要知道它”。
- 先给地图，再讲零件。复杂系统、算法或训练流程必须先用两三句话讲清整体输入、关键过程和输出，再展开模块。
- 每连续解释两至四个重要概念后，用自然的一句话提供认知停靠点，例如“到这里先记住……”或“前面这些设计其实都在解决同一个问题……”。不要机械重复相同句式。
- 使用适合口播的短句和自然连接。避免论文式长句、名词堆砌和多层括号。
- 精讲不等于把细节全部讲完。篇幅优先用于解释、例子、因果关系和必要回顾，而不是增加术语数量。

# 四、开头与章节连续性

第一章需要尽快让观众获得一张最小但完整的地图：论文面对什么问题、作者的核心办法是什么、结果大致如何。可以从现实问题、技术矛盾、反直觉发现或关键结果切入，但不要在观众尚不了解评测含义时连续堆叠模型名、数据集名和分数。

previous_chapters 是已经录制、不可修改的全部前文：

1. 不要重复已经详细解释过的内容，但可以用一句通俗总结把当前问题挂回 story_spine。
2. 当前章节开头要让观众知道“我们已经理解了什么，现在还缺哪一块”，不能只说“接下来进入某某部分”。
3. 当前章节内部的所有 segments 必须一次生成，形成“提出问题—解释机制—得出结论”的完整小叙事。
4. 章节结尾应明确 current_chapter.takeaway，并按照 transition_goal 自然制造下一个需要回答的问题。
5. previous_chapters 只用于保持教学和语言连续性；论文事实仍必须由 paper_content 支持。

# 五、术语、公式与技术细节

重要术语可以保留，但先讲作用，再给名称。例如不要只给 episode、trajectory、harness 等词下定义；应先用一个具体任务说明为什么必须区分这些层级，再把名称贴到观众已经理解的概念上。

公式应先讲它想衡量或优化什么，再讲变量和计算方式，最后解释结果变大或变小意味着什么。只有当公式结构本身是核心贡献时，才逐项解释；否则保留直觉和结论即可。

系统架构、数据流水线、训练阶段、状态管理、轨迹、上下文处理、重放、奖励和超参数是否展开，取决于它们是否直接支撑 core_message。不得因为论文写了就逐项复述。

# 六、实验与结论

实验讲解使用“论文主张—对应证据—证据意味着什么—证据不能说明什么”的结构。不要朗读整张表格或连续罗列 benchmark：

- 每组结论只选择最有代表性的一至两个数字；
- 先解释指标衡量什么，再说数字；
- 明确比较对象和比较条件；
- 正常说明负面结果、适用边界和局限；
- 保留 suggest、may、indicate 等原文的不确定性，不把相关性改写成因果性。

# 七、页面与口播内容的关系

输入由多个 PDF 页面组成，每页包含 page、text 和 visuals。visuals 是 MinerU 识别出的图、表、公式或带标题代码块，包含稳定 id、类型、caption、图片路径和页面位置。每个 segment 的 page 是这一段播放时的主要背景画面，不是口播事实的边界。

- page 应选择最能承载当前主要内容的页面，例如核心示意图、流程图、表格、公式、实验图或相关正文所在页。
- 当某个视觉元素能明显帮助解释当前主要内容时，将它的 id 原样填入 segment.visual_id，并让 segment.page 与该视觉元素的 page 一致；没有明确视觉焦点时 visual_id 必须为 null。
- 不得编造 visual_id，也不要仅因为页面存在图表就强行引用。引用图表时应讲清它帮助说明的关系或证据，不要只念 caption 或整张表。
- 口播可以综合 paper_content 中其他任意真实页面的信息，用来补充前因后果、定义、比较或证据；不要求每一句话都来自当前显示页。
- 如果一个结论需要跨页理解，应在口播中自然整合，画面选择其中视觉上最能代表当前主要内容的一页。
- 当讲解重点明显转移到另一页的图、表、公式或内容时，拆成新的 segment 并切换 page。
- 不要为了匹配页面而按 PDF 页序复述，也不要让背景页频繁进行没有教学价值的跳转。
- page 必须是输入中真实存在的 PDF 页码。

# 八、准确性

必须严格忠实于论文，不得编造方法、数据、实验结果、作者动机、因果关系、观点或局限。应明确区分论文事实、作者解释和为了教学使用的类比。无法由 paper_content 支持的内容不要写。

# 九、输出

当前调用只输出一个完整的 VideoChapterScript：

- chapter_id 和 title 必须与 current_chapter 完全一致；
- segments 按播放顺序包含 page、完整中文口播 text，以及可选的 visual_id；
- 只生成当前视频章节，不重写前文，不生成其他章节。

结构化输出必须是严格合法、可直接解析的 JSON 参数。JSON 的字段名和字符串边界仍使用英文双引号；但在 title、text 等字符串正文中引用词语、概念或句子时，禁止直接使用未转义的英文双引号，统一改用中文引号“……”。如果正文确实必须包含英文双引号，必须写成转义形式 \\\"。输出前检查所有字符串正文中的英文双引号均已正确转义。

        """
    ),
    (
        "human",
        """
请根据下面的信息生成当前视频章节的全部口播 segments。

整期视频规划：

{video_plan}

当前视频章节规划：

{current_chapter}

此前已生成的全部视频章节（第一章时为空数组）：

{previous_chapters}

下一个视频章节规划（最后一章时为 null）：

{next_chapter}

论文内容：

{paper_content}
"""
    ),
])


planning_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
你是一名擅长教学的 AI 论文精讲视频总编导。请先完整理解论文，再为“知道大模型和 Agent 的基本概念、但不了解这篇论文”的观众设计一条能够真正学懂论文的叙事路径。

你规划的是教学顺序，不是论文目录。不要照抄 Introduction、Related Work、Method、Experiments、Conclusion，也不要先罗列论文包含什么，再逐项分配章节。先确定观众最后应该建立怎样的整体理解，再决定信息的出现顺序。

# 一、先提炼整篇论文的教学骨架

1. central_question：用非论文式语言提出整篇论文真正想回答的一个问题。观众听完问题后，应当立即明白这项研究为什么存在。
2. core_message：用一段清楚的话概括作者做了什么、它为什么可能有效、主要证据和必要边界。不要写成口号。
3. story_spine：用 3 至 7 个有因果关系的步骤串起全文，例如“目标是什么 → 真正困难在哪里 → 作者的核心办法 → 关键机制如何解决困难 → 怎样验证 → 能说明什么与不能说明什么”。每一步都必须推动解释，不能只是论文模块名称。
4. teaching_anchor：如果适合，选择一个能贯穿多个章节的具体场景、最小例子、类比或思想实验；不适合则输出 null。它只帮助理解，不能冒充论文事实。

# 二、章节规划

1. 根据论文实际复杂度动态规划 4 至 8 个视频章节，不必用满 8 个。
2. 每章围绕一个 guiding_question 展开，只承担一个清晰的认知任务，并给出一句观众能复述的 takeaway。
3. key_points 只保留回答 guiding_question 必需的 1 至 4 点。不要把论文出现过的所有术语和模块塞进计划。
4. 第一章应尽快建立最小完整地图：现实问题、论文核心方案和结果轮廓。可以用关键结果做开场，但不能在指标尚未解释时堆叠模型名、数据集名和数字。
5. 中间章节按照 story_spine 推进。每一章都应解决上一章留下的具体问题，并说明自己在整体方案中的位置。
6. 方法、数据、训练、基础设施、公式和实现细节是否单独成章，取决于它们是否是理解核心贡献所必需，而不是取决于论文给了它们多少篇幅。
7. 实验章节应围绕核心主张组织证据，不按表格顺序报数；规划中同时保留最有价值的正面证据、负面结果和限制。
8. 最后一章应收束“作者做成了什么、没有证明什么、对实践或研究有什么意义”，不要机械重复摘要。
9. 各章节不得重复详细讲解同一概念。需要承接时，只规划一句回顾，再继续推进。
10. transition_goal 要描述观众在本章结束后自然产生的下一个疑问，不能写“下面进入下一章”。

# 三、页面规划

source_pages 是本章全部事实依据的候选页，可以跨越、合并和重新排列论文页面：

- 必须使用输入中真实存在的页码；
- 不要求按 PDF 页序，也不要求口播只使用播放时显示的页面；
- 优先纳入包含核心图、表、公式、流程或关键论证的页面；
- 同一章节可以综合多个页面的信息，实际写作时再为每个 segment 选择最能代表当前主要内容的一页作为背景。

# 四、通用性与准确性

这套规划必须适用于不同类型的 AI 论文，包括模型、算法、Agent、系统、数据、评测和应用研究。不要预设论文一定包含强化学习、轨迹、工具调用、模型合并或某一种固定技术结构。

不得编造论文没有的方法、数据、实验、动机、因果关系、结论或局限。论文使用谨慎措辞时，计划也应保留对应的不确定性。

章节标题会显示在竖屏视频顶部，使用 2 至 8 个汉字，准确、简短、有辨识度，不使用序号和标点。chapter_id 按 chapter_01、chapter_02 的格式连续编号。

# 五、结构化输出格式

你会通过工具调用返回 PaperPlan。工具参数必须是严格合法、可直接解析的 JSON：

1. JSON 的字段名和字符串边界正常使用英文双引号。
2. 在所有字符串正文中引用词语、概念或句子时，禁止直接使用未转义的英文双引号，统一使用中文引号“……”。
3. 如果字符串正文确实必须包含英文双引号，必须写成转义形式 \\\"。
4. 错误示例：`{{"question": "为什么"换个 harness"还不够？"}}`。
5. 正确示例：`{{"question": "为什么“换个 harness”还不够？"}}`。
6. 输出前检查所有字符串正文中的英文双引号均已正确转义，不要输出 Markdown 代码块或额外说明。

只输出 PaperPlan，不要提前撰写完整口播稿。
""",
    ),
    (
        "human",
        """
请为下面的论文规划一期中文精讲视频：

{paper_content}
""",
    ),
])


@lru_cache(maxsize=1)
def get_llm() -> ChatDeepSeek:
    """Create the API client only when a generation stage actually needs it."""
    return ChatDeepSeek(
        model=LLM_MODEL,
        base_url=LLM_BASE_URL,
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
        extra_body=LLM_EXTRA_BODY,
    )


@lru_cache(maxsize=1)
def _get_planning_chain():
    return planning_prompt | get_llm().with_structured_output(
        PaperPlan,
        method="function_calling",
        include_raw=True,
    )


@lru_cache(maxsize=1)
def _get_chapter_chain():
    return chapter_prompt | get_llm().with_structured_output(
        VideoChapterScript,
        method="function_calling",
        include_raw=True,
    )


def script_generation_cache_material() -> dict:
    """Return deterministic, non-secret inputs that affect PaperScript output."""

    def prompt_messages(prompt: ChatPromptTemplate) -> list[dict[str, str]]:
        return [
            {
                "role": type(message).__name__,
                "template": message.prompt.template,
            }
            for message in prompt.messages
        ]

    return {
        "generation_version": SCRIPT_GENERATION_VERSION,
        "llm": {
            "model": LLM_MODEL,
            "base_url": LLM_BASE_URL,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "extra_body": LLM_EXTRA_BODY,
        },
        "prompts": {
            "planning": prompt_messages(planning_prompt),
            "chapter": prompt_messages(chapter_prompt),
        },
        "schemas": {
            "paper_plan": PaperPlan.model_json_schema(),
            "chapter_script": VideoChapterScript.model_json_schema(),
            "paper_script": PaperScript.model_json_schema(),
        },
    }


def _truncate_chapter_title(title: str, max_length: int = 8) -> str:
    """Keep navigation titles compact without rejecting an otherwise valid plan."""
    if len(title) <= max_length:
        return title

    ellipsis = "..."
    return title[:max_length - len(ellipsis)].rstrip() + ellipsis


def invoke_structured_with_retry(
    chain,
    values: dict,
    stage: str,
    max_attempts: int = 5,
):
    """Retry transient empty/invalid structured responses with useful diagnostics."""
    diagnostics = []
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = chain.invoke(values)
        except Exception as exc:
            last_exception = exc
            diagnostics.append(
                f"第 {attempt} 次调用异常: {type(exc).__name__}: {exc}"
            )
        else:
            parsed = result.get("parsed")

            if parsed is not None:
                return parsed

            raw = result.get("raw")
            raw_content = str(getattr(raw, "content", ""))[:500]
            tool_calls = getattr(raw, "tool_calls", []) or []
            finish_reason = (
                getattr(raw, "response_metadata", {}) or {}
            ).get("finish_reason")
            diagnostics.append(
                f"第 {attempt} 次没有结构化结果: "
                f"parsing_error={result.get('parsing_error')!r}, "
                f"finish_reason={finish_reason!r}, "
                f"tool_calls={len(tool_calls)}, "
                f"content={raw_content!r}"
            )

        if attempt < max_attempts:
            print(
                f"{stage}结构化输出失败，"
                f"正在重试 {attempt + 1}/{max_attempts}..."
            )

    message = f"{stage}连续 {max_attempts} 次未返回有效结构化结果"
    details = "\n".join(diagnostics)

    if last_exception is not None:
        raise RuntimeError(f"{message}\n{details}") from last_exception

    raise RuntimeError(f"{message}\n{details}")


def generate_video_plan(
    paper_content: str,
    available_pages: set[int],
) -> PaperPlan:
    plan = invoke_structured_with_retry(
        chain=_get_planning_chain(),
        values={
            "paper_content": paper_content,
        },
        stage="视频章节规划",
    )

    normalized_chapters = []

    for index, chapter in enumerate(plan.chapters, start=1):
        source_pages = list(dict.fromkeys(
            page
            for page in chapter.source_pages
            if page in available_pages
        ))

        if not source_pages:
            raise ValueError(
                f"视频章节“{chapter.title}”没有有效的论文来源页"
            )

        normalized_chapters.append(
            chapter.model_copy(update={
                "chapter_id": f"chapter_{index:02d}",
                # Title length is a display concern. Truncate model output
                # instead of failing and regenerating the entire plan.
                "title": _truncate_chapter_title(chapter.title),
                # The prompt asks for at most four teaching points. Some
                # function-calling models occasionally return five; trim the
                # overflow instead of rejecting an otherwise valid plan.
                "key_points": chapter.key_points[:4],
                "source_pages": source_pages,
            })
        )

    return plan.model_copy(update={
        "chapters": normalized_chapters,
    })


def generate_video_chapter(
    paper_content: str,
    video_plan: PaperPlan,
    current_chapter: VideoChapterPlan,
    previous_chapters: list[VideoChapterScript],
    next_chapter: VideoChapterPlan | None,
    available_pages: set[int],
    available_visuals: dict[str, int],
) -> VideoChapterScript:
    chapter_script = invoke_structured_with_retry(
        chain=_get_chapter_chain(),
        values={
            "paper_content": paper_content,
            "video_plan": video_plan.model_dump_json(indent=2),
            "current_chapter": current_chapter.model_dump_json(indent=2),
            # Deliberately pass every previously generated segment without summarizing.
            "previous_chapters": json.dumps(
                [chapter.model_dump() for chapter in previous_chapters],
                ensure_ascii=False,
                indent=2,
            ),
            "next_chapter": (
                next_chapter.model_dump_json(indent=2)
                if next_chapter is not None
                else "null"
            ),
        },
        stage=f"视频章节“{current_chapter.title}”",
    )

    invalid_visual_ids = sorted({
        segment.visual_id
        for segment in chapter_script.segments
        if segment.visual_id is not None
        and segment.visual_id not in available_visuals
    })
    if invalid_visual_ids:
        print(
            f"视频章节“{current_chapter.title}”忽略了不存在的视觉元素: "
            f"{invalid_visual_ids}"
        )

    # A valid visual reference is more specific than the separately generated
    # background page. Correct a page mismatch deterministically instead of
    # discarding an otherwise useful chapter generation.
    normalized_segments = [
        segment.model_copy(update={
            "page": available_visuals[segment.visual_id],
        })
        if segment.visual_id is not None
        and segment.visual_id in available_visuals
        and available_visuals[segment.visual_id] != segment.page
        else segment.model_copy(update={"visual_id": None})
        if segment.visual_id is not None
        and segment.visual_id not in available_visuals
        else segment
        for segment in chapter_script.segments
    ]
    chapter_script = chapter_script.model_copy(update={
        "segments": normalized_segments,
    })

    invalid_pages = sorted({
        segment.page
        for segment in chapter_script.segments
        if segment.page not in available_pages
    })

    if invalid_pages:
        raise ValueError(
            f"视频章节“{current_chapter.title}”使用了不存在的页码: "
            f"{invalid_pages}"
        )

    return chapter_script.model_copy(update={
        "chapter_id": current_chapter.chapter_id,
        "title": current_chapter.title,
    })

def generate_paper_script(
    _pages: list[dict],
) -> PaperScript:

    if not _pages:
        raise ValueError("论文页面不能为空")

    normalized_pages = [{
        "page": int(page["page"]),
        "text": str(page.get("text", "")),
        "visuals": list(page.get("visuals", [])),
    } for page in _pages]
    paper_content = json.dumps(
        normalized_pages,
        ensure_ascii=False,
    )
    available_pages = {
        int(page["page"])
        for page in _pages
    }
    visuals = [
        PaperVisual.model_validate(visual)
        for page in normalized_pages
        for visual in page["visuals"]
    ]
    available_visuals = {visual.id: visual.page for visual in visuals}

    print("正在规划视频章节...")
    video_plan = generate_video_plan(
        paper_content=paper_content,
        available_pages=available_pages,
    )
    print(f"视频标题: {video_plan.video_title}")

    for index, chapter in enumerate(video_plan.chapters, start=1):
        print(
            f"  {index}/{len(video_plan.chapters)} "
            f"{chapter.title}"
        )

    generated_chapters: list[VideoChapterScript] = []

    for index, current_chapter in enumerate(video_plan.chapters):
        next_chapter = (
            video_plan.chapters[index + 1]
            if index + 1 < len(video_plan.chapters)
            else None
        )

        print(
            f"正在生成视频章节 "
            f"{index + 1}/{len(video_plan.chapters)}: "
            f"{current_chapter.title}"
        )

        chapter_script = generate_video_chapter(
            paper_content=paper_content,
            video_plan=video_plan,
            current_chapter=current_chapter,
            previous_chapters=generated_chapters,
            next_chapter=next_chapter,
            available_pages=available_pages,
            available_visuals=available_visuals,
        )
        generated_chapters.append(chapter_script)

        print(
            f"章节完成，共 {len(chapter_script.segments)} 个 segments"
        )

    return PaperScript(
        title=video_plan.video_title,
        plan=video_plan,
        chapters=generated_chapters,
        visuals=visuals,
    )
