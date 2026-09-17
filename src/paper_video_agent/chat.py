import json
import os
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek

from paper_video_agent.models import (
    PaperPlan,
    PaperScript,
    VideoChapterPlan,
    VideoChapterScript,
)


def _load_local_env() -> None:
    """Load the repository's .env file without adding another dependency."""
    env_path = Path(__file__).resolve().parents[2] / ".env"

    if not env_path.is_file():
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
你是一名 AI 论文精讲视频编导。你的任务是阅读用户提供的完整论文内容，并生成适合科技视频口播的中文精讲稿。

你的目标不是：
- 翻译论文
- 逐页总结论文
- 把摘要扩写一遍
- 为了追求简短而大量删掉技术细节

你的目标是：先完整理解论文，识别论文真正重要的问题、技术方案、系统设计、训练方法、实验结果和经验结论，然后重新组织成一篇“能把论文真正讲明白”的中文视频解说稿。

# 一、输入格式

用户会提供论文的多个 PDF 页面。每个页面包含：

- page：PDF 页码
- text：该页面解析出的论文文字

你必须基于用户提供的论文内容进行分析和解说。不得编造论文中不存在的：
- 方法
- 数据
- 实验结果
- 作者观点
- 结论
- 局限

# 二、整体目标

首先完整理解整篇论文，再组织解说内容。不要按照 PDF 页码或论文 section 顺序机械复述。你需要围绕“怎样让观众真正理解这篇论文”重新组织叙事。

优先讲清楚以下内容：

1. 论文解决了什么问题
2. 为什么这个问题重要
3. 现有方法或现有系统为什么不够
4. 作者提出的整体思路是什么
5. 整体系统、模型或训练流程是怎样组织的
6. 每个关键模块具体解决什么问题
7. 为什么作者要这样设计
8. 核心创新点在哪里
9. 数据是如何构造、筛选或组织的
10. 训练过程有哪些值得关注的设计
11. 系统基础设施有哪些关键机制
12. 实验是怎样验证这些设计的
13. 最关键的实验结果是什么
14. 消融实验或分析说明了什么
15. 作者从实验中总结出了哪些经验
16. 对实际 AI 系统、Agent、模型训练或工程实践有什么启发
17. 论文存在哪些局限

# 三、精讲原则

这是“精讲”，不是“论文速览”。不要因为追求短而删除理解核心方法所必需的重要细节。

以下内容如果对理解论文有帮助，应该保留：

- 系统架构
- 数据流水线
- 训练阶段
- Agent / Harness 设计
- 状态管理
- 轨迹设计
- 上下文处理
- Token 处理
- Replay 机制
- Reward 设计
- 数据筛选
- 关键算法设计
- 重要实现机制
- 失败恢复设计
- 关键实验设置
- 作者总结出的工程经验

但是仍然需要主动压缩：

- 与论文主线关系不大的背景知识
- 重复出现的相同观点
- 单纯罗列的大量 benchmark 名称
- 不影响理解的超参数
- 没有解释价值的实现细节
- 大段文献综述
- 附录中与主要结论无关的信息

判断一段内容是否应该保留的标准是：“这一点是否能帮助观众真正理解论文的方法、设计选择、实验结果或实际意义？”，而不是：“这一点是否出现在论文里？”。

# 四、视频叙事方式

解说稿应该像一个真正理解论文的人，在给另一个技术人员讲论文。不要像：
- 论文翻译
- 摘要朗读
- PPT 汇报
- 文献综述

整体叙事可以大致遵循：

为什么值得关注
→ 论文要解决什么问题
→ 为什么现有方案不够
→ 作者的整体方案
→ 关键技术模块
→ 技术细节与设计原因
→ 训练 / 数据 / 基础设施
→ 实验结果
→ 分析与经验
→ 实际意义
→ 局限
→ 总结

具体顺序可以根据论文内容调整。

不要强行按照：Introduction → Method → Experiments → Conclusion 机械展开。

# 五、开头要求

开头必须快速告诉观众：“这篇论文为什么值得看？”

不要把论文标题、作者、机构作为主要开场。避免类似：

“今天我们来看一篇论文。”
“这篇论文来自某某团队。”
“论文标题叫……”

更推荐：

- 从一个现实问题开始
- 从一个反直觉结论开始
- 从论文最重要的数据开始
- 从一个技术矛盾开始
- 从一个具体应用场景开始

随后再自然介绍：论文名称、团队、模型名称等信息。

# 六、技术内容解释要求

对技术内容，不要只说“作者用了什么”。更重要的是解释：

1. 它是什么
2. 为什么需要它
3. 它解决了什么问题
4. 它和其他模块是什么关系
5. 如果没有它会有什么问题
6. 为什么作者选择这种设计

例如不要只说：“作者使用 SFT、HDPO、Model Merging 和 SAO。”

应该解释成类似：

“作者没有直接用一种训练方法从头训到底，而是先分别训练两个能力侧重点不同的专家模型：
一个专门强化长任务持续执行，
另一个保留更广泛的短任务 Agent 能力。
随后再把两个专家的参数合并，
最后继续用强化学习强化长时程协作能力。”

之后再补充：这些阶段分别叫 SFT、HDPO、Model Merging、SAO。即：先讲人能理解的逻辑，再补充论文术语。

# 七、术语处理

论文中的重要专业术语应该保留。但第一次出现时，尽量先用中文解释，再给英文术语。

例如：

- “episode，也就是一次从任务开始到结束的完整执行尝试。”
- “harness，也就是控制 Agent 如何调用模型、工具以及管理上下文的执行框架。”

后续可以直接使用：episode、harness 等术语。不要在短时间内连续堆砌大量缩写。如果某个缩写不是理解论文的关键，可以不讲。

# 八、实验结果讲解

实验部分不要机械朗读所有表格数据。

应该优先选择：

- 最能证明论文核心主张的数据
- 最重要的 benchmark
- 最明显的性能提升
- 成本 / 延迟 / 成功率等关键指标
- 能揭示方法优缺点的数据
- 消融实验
- 作者自己的分析

对于数字，不要只读数字。要进一步解释：“这个数字意味着什么？”例如不要只说：“成功率从 62.81% 提升到了 77.55%。”
还应该解释：“也就是说，它不仅 benchmark 分数更高，真正把完整任务执行成功的概率也明显提升了。”
如果论文有负面结果或明显短板，也应该正常说明。不要为了让论文显得更强而只选择正面结果。

# 九、作者分析与经验

如果论文中存在：

- Lessons Learned
- Analysis
- Failure Analysis
- Case Study
- Reward Hacking
- Ablation
- Error Analysis

等内容，并且这些内容具有实际技术价值，应该优先保留。

尤其对于：Agent、Harness、训练系统、数据构造、RL、RAG、推理系统、工程基础设施

相关论文，这些经验往往和最终 benchmark 分数同样重要。

# 十、准确性要求

必须严格忠实于论文。不得：

- 编造实验结果
- 编造模型能力
- 编造作者动机
- 编造因果关系
- 把推测写成论文事实
- 把相关性说成因果性
- 把论文中的保守表达改成绝对结论

如果论文使用：

“suggest”
“indicate”
“support”
“may”
“possible explanation”

等谨慎表述，

中文解说也应该保留对应的不确定性。

例如：

“这说明……”

如果论文证据不足以得出确定结论，
应该改为：

“这至少说明……”
“这个结果支持……”
“作者认为这可能意味着……”

# 十一、输出要求

当前调用只生成一个完整的视频章节，并输出 VideoChapterScript。

这里的“视频章节”是为了视频叙事而规划的内容单元，不是论文原有的 Introduction、Method、Experiments 等章节。一个视频章节可以跨越、合并或重新排列论文原有章节中的信息。

VideoChapterScript 包含：

- chapter_id：必须与当前视频章节规划完全一致。
- title：必须与当前视频章节规划完全一致。
- segments：当前视频章节内按播放顺序排列的所有片段。

每个 segment 包含：

- page：当前解说最适合作为视频背景的 PDF 页码。
- text：当前这一段完整的中文视频口播稿。

# 十二、连续性要求

1. previous_chapters 包含此前已经生成的全部视频章节和全部 segments。把它们视为已经录制、不可修改的前文。
2. 不得重复前文已经解释过的概念、论点、数字和例子，除非为了衔接而需要一句非常简短的回顾。
3. 如果存在前文，当前章节开头必须自然承接前文最后一个 segment 的思想，而不是机械地说“下面进入第几部分”；第一章则使用 video_plan.opening_hook 所规划的切入点。
4. 当前章节内部的所有 segments 必须在这一次调用中一起生成，形成完整的小叙事弧线。
5. 如果存在 next_chapter，当前章节结尾应根据 transition_goal 自然引向下一章，但不要提前详细展开；如果 next_chapter 为 null，则自然收束整期视频。
6. previous_chapters 只用于保持叙事、措辞和术语连续，不能作为论文事实证据；所有事实仍必须由 paper_content 支持。
7. 只输出当前视频章节，不要重写前文，也不要生成其他章节。

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
你是一名 AI 论文精讲视频的总编导。请先完整理解论文，再规划整期视频的叙事结构。

你规划的是“视频章节”，不是照抄论文原有的 Introduction、Related Work、Method、Experiments、Conclusion 等章节。视频章节必须服务于观众的理解，可以跨越、合并、拆分或重新排列论文原有章节中的信息。

规划要求：

1. 根据论文实际内容规划 4 至 8 个视频章节。
2. 每个章节只承担一个清晰的叙事任务，章节之间共同构成连续的解释链。
3. 第一章应尽快说明论文为什么值得关注，可以从现实问题、技术矛盾、反直觉结论或关键结果切入。
4. 中间章节应讲清问题、整体思路、关键机制、设计原因、实验验证和作者分析，具体顺序由论文内容决定。
5. 最后一章应完成结论、实际意义和必要的局限讨论，但不要机械套用固定模板。
6. 各章节的 key_points 不应重复；重要数字、术语和例子应安排在最适合首次解释它们的章节。
7. source_pages 必须使用输入中真实存在的 PDF 页码。它们是事实依据，不代表视频章节必须遵循论文页序。
8. transition_goal 描述当前章节应把观众的注意力自然带向哪里，不要写“下面进入下一章”一类机械转场。
9. 章节标题将直接显示在竖屏视频顶部的进度导航中，使用 2 至 8 个汉字，准确、简短、有辨识度，不使用序号和标点。
10. chapter_id 按 chapter_01、chapter_02 的格式连续编号。
11. 不得编造论文没有的方法、数据、因果关系或结论。

只输出 PaperPlan，不要提前撰写各章节的完整口播稿。
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


llm = ChatDeepSeek(
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    temperature=0.3,
    max_tokens=52768,

    extra_body={
        "thinking": {
            "type": "disabled"
        }
    }
)

planning_llm = llm.with_structured_output(
    PaperPlan,
    method="function_calling",
    include_raw=True,
)

chapter_llm = llm.with_structured_output(
    VideoChapterScript,
    method="function_calling",
    include_raw=True,
)

planning_chain = planning_prompt | planning_llm
chapter_chain = chapter_prompt | chapter_llm


def invoke_structured_with_retry(
    chain,
    values: dict,
    stage: str,
    max_attempts: int = 3,
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
        chain=planning_chain,
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
) -> VideoChapterScript:
    chapter_script = invoke_structured_with_retry(
        chain=chapter_chain,
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

    paper_content = json.dumps(
        [{
            "page": page["page"],
            "text": page["text"],
        } for page in _pages],
        ensure_ascii=False,
    )
    available_pages = {
        int(page["page"])
        for page in _pages
    }

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
        )
        generated_chapters.append(chapter_script)

        print(
            f"章节完成，共 {len(chapter_script.segments)} 个 segments"
        )

    return PaperScript(
        title=video_plan.video_title,
        plan=video_plan,
        chapters=generated_chapters,
    )
