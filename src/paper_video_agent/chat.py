import json
import os
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek

from push_agent.models import PaperScript


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


prompt = ChatPromptTemplate.from_messages([
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

最终输出一个 PaperScript。

PaperScript 包含：

title：
一个适合作为视频标题的中文标题。

要求：
- 能准确反映论文核心内容
- 突出最值得关注的点
- 不要纯粹直译英文论文标题
- 不要标题党
- 不得夸大论文结论


segments：按照最终视频播放顺序排列。

每个 segment 包含：

page：当前解说最适合作为视频背景的 PDF 页码。

text：当前这一段完整的中文视频口播稿。

        """
    ),
    (
        "human",
        """
请根据下面的论文内容生成视频解说稿。

论文内容：

{paper_content}
"""
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

structured_llm = llm.with_structured_output(
    PaperScript,
    method="function_calling",
    include_raw=True,
)

chain = prompt | structured_llm

def generate_paper_script(
    _pages: list[dict],
) -> PaperScript:

    paper_content = json.dumps(
        [{
            "page": page["page"],
            "text": page["text"],
        } for page in _pages],
        ensure_ascii=False,
    )

    result = chain.invoke({
        "paper_content": paper_content,
    })
    result = result["parsed"]
    print(result.title)

    for segment in result.segments:
        print(segment.page)
        print(segment.text)

    return result
