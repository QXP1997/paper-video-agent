# Agent Loop 文献综述与 QHarness 设计建议

> 文档状态：生产目标设计稿，供实现与评审使用；不代表下述能力已经实现  
> 调研与代码核对日期：2026-09-13  
> 代码基线：HEAD `8ff37fe` 及当日工作区；已实现能力以源码为准  
> 适用范围：本地优先、单进程可承载多个 Run、以单 Agent 代码任务为主要场景  
> 设计目标：在可接受的成本和交互负担下，持续、可恢复、可验证地完成真实任务，并形成可以实验验证的产品差异

阅读建议：推理主流程与示例优先看第 6 节，直接相关研究看第 3.6 节，三项改进与实验看第 15 节，后续实现看第 16—18 节。第 5、8—14 节提供模型、工具、验证与持久化等运行支撑。

实现进度（2026-09-14）：[实现计划](./Agent-Loop实现计划.md)的批次 1—6 已落地推理契约、共享角色调用、基础 Context Compiler、持久执行账本、Actor 和三层验证，并由 Planner / Executor 贯通完整任务主线。第六批在原模块上接入受证据约束的分层反馈、动态阶段选择和缺口关联/调查进展判断：局部失败保留方案、未知原因先调查、前提失效重规划；排除假设和缩小范围计为进展，重复结论和改写阶段标题不能刷进展。累计 158 项离线测试通过；三项机制有独立开关，示例 20 提供完整轨迹和八组配置对照。具体字段、启用方法和限制见[实现计划第 5.6 节](./Agent-Loop实现计划.md#56-批次-6)。这些结果验证工程行为，不证明学术首创或真实任务收益；完整恢复/Steering、上下文压缩和真实模型与 SRT 评测继续按批次 7—8 推进。后续仍遵循复用优先原则，优先扩展原模块。

## 1. 设计主张

QHarness 的 Loop 应当是一套**围绕任务验收持续推进工作的运行系统**。模型调用工具只是其中的执行机制；任务目标、执行证据、工作区版本、上下文、恢复和用户干预共同决定它能否用于生产。

本设计把以下能力作为同一个交付范围：任务与验收建模、模型—工具循环、受控调度、验证与修复、长上下文管理、预算与无进展处理、持久化与恢复、用户追加要求、可观测性和回归评测。可以按依赖顺序开发，但不能把这些能力仅做成空接口，就宣称生产 Loop 已经完成。

完整设计也不意味着每次任务都启动全部机制。解释一段代码可以轻量完成；修改代码需要绑定产物和检查证据；长任务需要计划、检查点和上下文交接；有外部副作用的任务需要更严格的执行与恢复条件。能力完整、按需启用，比固定增加几个 Planner / Reviewer Agent 更符合当前目标。

本次确定的推理主线是：**Planner 生成初步 Todo List；Executor 针对当前 Todo，结合环境与反馈滚动确定阶段目标和预期结果；内层 Agent Loop 自主调用工具；Verifier 检查阶段结果、Todo 完成度和有效进展，再决定继续、修复、调查或调整计划。所有 Todo 完成后进行任务整体验收。**

每轮 Executor 都有明确的规划决策，但可以保留上一轮仍有效的目标与方法；不要求每次推翻旧方案，也不在每次工具调用前再增加独立 Planner 调用。Planner、Executor 内的阶段规划器和行动模型可以复用同一个 ModelBackend，通过不同职责、上下文和输出契约实现。

支撑这条推理主线的数据对象包括：

- **Task Contract（任务契约）**：用户要什么、允许做什么、用什么判定完成。
- **Evidence Ledger（证据台账）**：实际做了什么、检查了什么、证据适用于哪个版本。
- **Durable Run State（持久运行状态）**：当前进度、尚未结束的动作、资源消耗，以及中断后下一步可以安全做什么。

本次聚焦的三项推理改进是：**按失败所在层级调整执行、动态选择阶段粒度、把阶段结果关联到 Todo 的实际缺口。**第 15 节给出机制和对照实验。证据版本绑定与副作用恢复继续作为生产支撑能力；它们不能替代推理策略本身。上述改进是待验证的组合设计，不预先宣称学术首创或性能收益。

## 2. 相比旧稿，需要修正什么

| 旧稿倾向或表述 | 本次修订 |
|---|---|
| 首版只完成最内层，验证、持久化、Context 都后置 | 把完整生产闭环作为目标，按依赖拆实施包，每包都明确如何融入最终闭环 |
| 无 Tool Call 的文本表示完成 | 它至多构成候选交付；必须先排除截断、拒答、协议异常，再按任务契约验收 |
| Verification 返回 RETRY，顺便决定下一轮 | 分开“检查事实”和“控制决策”：检查给出 PASS / FAIL / INCONCLUSIVE / ERROR，控制器决定修复、重测、等待或结束 |
| Verifier 完全不修改工作区 | 判定器不修代码；测试执行器可能写缓存、构建产物或数据库，必须声明执行环境与副作用 |
| 用户下一条消息一定创建新 Run | 运行中的补充约束可以是当前 Run 的 Steering Event；完成后的新任务才通常创建新 Run |
| Tool Call ID 足以避免重复执行 | ID 负责协议配对；副作用恢复还需要持久执行记录、稳定 operation_id 和结果核对 |
| 所有 None 都表示无限制 | 当前全局工具次数/并发支持 None；单工具覆盖中的 None 表示继承，不能混为一谈 |
| SWE-agent 说明不能只用 Shell | ACI 研究说明接口影响表现；mini-SWE-agent 提供了 Bash-only 的有效反例，工具方案必须结合任务和模型评测 |
| Commit 没变化说明无进展 | 阅读、定位、验证、澄清也可能有进展；反复提交无效修改则可能没有进展 |
| 只读工具并行以后再设计 | 现在明确依赖、资源冲突、快照与取消语义；安全并发属于完整调度能力 |
| 架构主要说明外部编排，推理策略不够明确 | 明确 Todo 推进、Executor 阶段循环、Agent 工具循环三个逻辑层次 |
| Planner 一次性输出固定执行计划 | 初步 Todo 提供方向，阶段目标与方法由 Executor 结合实际环境滚动生成 |
| 阶段验证通过就退出 Executor | 阶段目标达成、Todo 完成和 Task 完成分别判断 |
| 每次失败都重新规划整个任务 | 按问题所在层级选择局部修复、调查、阶段重规划或 Todo 调整 |

## 3. 研究证据：哪些结论可靠，哪些仍需验证

### 3.1 阅读与引用口径

本文采用针对设计问题的文献与官方实现综述，不是穷尽检索的系统综述。主要覆盖：行动循环、反馈修复、长期上下文、真实任务评测和可恢复运行。优先使用论文原文入口、作者项目页和官方文档。

证据分三类：

- **研究结果**：只在对应模型、任务、反馈条件和预算下成立，不外推成所有 Agent 的规律。
- **工程经验**：说明一个团队如何解决问题，可借鉴，但通常不构成严格对照实验。
- **本项目提案**：基于上述材料和仓库约束作出的设计，需要 QHarness 自己验证。

不比较不同文章的榜单百分比来证明某种架构更强，因为模型、预算、工具、数据与评测版本可能不同。动态文档以本次访问为准；正式实现时还应在 ADR / 依赖清单中固定引用的版本或 Commit。

### 3.2 研究与设计映射

| 研究 | 核心观察 | 能支持的设计 | 不能推出的结论 |
|---|---|---|---|
| [ReAct，ICLR 2023](https://arxiv.org/abs/2210.03629) | 推理与行动交错，环境观察影响后续决策 | 模型—动作—观察循环；结果必须进入后续上下文 | 标准循环必须有三层；论文已经解决并发与恢复 |
| [SWE-agent，2024](https://arxiv.org/abs/2405.15793) | Agent-Computer Interface 的设计会影响代码任务表现 | 把工具描述、编辑反馈、搜索输出当作可评测组件 | 工具越多越好；结构化工具永远优于 Shell |
| [Self-Refine，NeurIPS 2023](https://arxiv.org/abs/2303.17651) | 生成—反馈—改进在所测任务中能够提升结果 | 按任务启用候选结果修订 | 任意任务都值得固定反思若干轮 |
| [Reflexion，NeurIPS 2023](https://arxiv.org/abs/2303.11366) | 把反馈转成文字经验，供后续尝试使用 | 保留可追溯的失败经验与修复约束 | 反思内容一定正确；必须启动多个 Agent |
| [CRITIC，ICLR 2024](https://arxiv.org/abs/2305.11738) | 借助工具检查并修正初始回答 | 让外部证据参与验证与修复 | 任意工具反馈都是可靠 Oracle |
| [Cannot Self-Correct，ICLR 2024](https://arxiv.org/abs/2310.01798)；[自纠错综述，TACL 2024](https://aclanthology.org/2024.tacl-1.78/) | 无外部反馈的内在纠错在部分推理设置中不稳定，可能退化；不同纠错条件须区分 | 验证优先使用独立证据，保留未能判断状态 | LLM 永远不能自纠错；任何 LLM Judge 都无效 |
| [LATS，ICML 2024](https://proceedings.mlr.press/v235/zhou24r.html) | 将搜索、行动与反馈结合，探索多条候选轨迹 | 将分支搜索放在可替换策略和隔离环境中 | 默认树搜索符合所有任务的成本目标 |
| [AgentBench，ICLR 2024](https://arxiv.org/abs/2308.03688) | 跨交互环境评估，暴露长期决策与指令遵循问题 | 评测轨迹与任务行为，不能只评分最终回答 | 单一代码基准能够覆盖所有生产问题 |
| [SWE-bench，ICLR 2024](https://arxiv.org/abs/2310.06770) | 使用真实仓库 Issue 与测试评价修复 | 固定环境、检查失败用例修复和原有行为保留 | 测试通过等同完整用户验收 |
| [τ-bench，2024 预印本 / ICLR 2025](https://arxiv.org/abs/2406.12045) | 同时考察用户交互、领域规则与环境终态，并关注多次运行的一致性 | 引入重复试验、规则遵循和终态核验 | 一次跑通就证明可以稳定上线 |
| [ACE，ICLR 2026](https://arxiv.org/abs/2510.04618) | 将上下文视为可增量演化的经验手册，缓解反复重写造成的信息丢失 | 结构化、增量、带出处的工作记忆 | 可把未经验证的经验直接写成生产策略 |

### 3.3 对代码任务最有价值的推论

**外部证据优先，但证据也要验真。** 编译成功、退出码为零、测试全绿分别只证明有限事实。错误的测试选择、零测试收集、被删掉的断言、过期的运行结果，都可能制造假成功。QHarness 应记录检查覆盖的验收项和运行前提，不能把所有成功退出统一提升为“任务完成”。这是从反馈研究到生产验证的工程推论。

**短期循环和长期任务需要不同状态。** ReAct 的动作历史能够驱动下一步，但长期任务还需要知道哪些要求尚未完成、哪些决定仍有效、哪些文件后来变了。不能只把完整聊天记录不断追加到 Context。

**一次尝试的能力与多次运行的可靠性应分开评测。** 内部修复轮次属于一次完整 Run 的成本；重复独立运行用于测可靠性；候选搜索的多次尝试则用于提高成功机会。这三种“多跑几次”不能混成一个指标。

### 3.4 2025—2026 年长期 Harness 的工程进展

Anthropic 的 [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)（2025）用初始化、功能清单、逐项推进、进度记录和版本历史支持跨上下文窗口工作。这说明“下一个执行阶段如何知道真实进度”是运行时问题；不要求 QHarness 照搬两个 Agent。可将其落实为 Task Contract、结构化进度和恢复时的环境核对。

其 [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)（2026）进一步使用 Planner、Generator、Evaluator，以及阶段验收约定和实际应用检查。文章也呈现了成本增加、评估偏好及模型变化对 Harness 设计的影响。对 QHarness 最有价值的是**提前确定验收、实际操作产物、按缺陷反馈迭代**；多角色配置和特定实验的质量提升不能直接推广为默认架构。

[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)（2025）讨论了按需检索、压缩与结构化笔记。QHarness 可组合这些机制，但应把运行事实保留在数据库和 Artifact 中，摘要只是模型输入视图。压缩不是删除历史，也不能承担副作用恢复的事实来源。

### 3.5 新兴 Harness 综述的使用边界

[Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents](https://arxiv.org/abs/2609.00006) 属于 2026 年源码比较预印本；它对自研循环、检索和扩展机制的观察限于作者采样的系统与版本，不证明通用框架或向量检索普遍无效。本次页面的编号与显示提交月份存在不一致，不用其时间线或采样数量支撑关键决策，正式引用这些细节前须进一步核对版本。

[Agent Harness Engineering: A Survey 的作者项目页](https://picrew.github.io/LLM-Harness/) 给出 Execution、Tooling、Context、Lifecycle、Observability、Verification、Governance 七个维度，适合检查职责遗漏。本次 [OpenReview 原文入口](https://openreview.net/pdf?id=eONq7FdiHa) 触发浏览器验证，因此仅采用可访问项目页上的分类，不延续旧稿未经本次确认的“TMLR 审稿状态”断言。


### 3.6 直接支撑本次推理流程的研究与实现

前面的 ReAct、反馈修复与长期上下文工作提供公共基础。下面几项更直接对应本次确定的“初步 Todo → 动态阶段规划 → 行动 → 验证 → 再规划”。它们支持组成机制，尚不能证明 QHarness 这一完整组合的效果。

#### ADaPT：结合执行能力按需分解

[ADaPT: As-Needed Decomposition and Planning with Language Models](https://aclanthology.org/2024.findings-naacl.264/)（Findings of NAACL 2024）先让 Executor 尝试任务，无法完成时才调用 Planner 分解，并递归处理子任务。其方法采用短而抽象的计划，避免在环境未知时预先确定大量细节；实验覆盖 ALFWorld、WebShop 和 TextCraft。

对 QHarness 的支持是：分解粒度应结合实际执行反馈。区别是 ADaPT 主要在执行失败后进一步拆分，而本设计在每个 Executor 阶段根据上下文形成目标，允许补信息、保留方案或改变方法，不只递归拆任务。ADaPT 的中间成功判定使用模型启发式，不能直接作为生产代码验收保证。[方法第 3 节](https://arxiv.org/html/2311.05772v2)

#### AdaPlanner：比较预期与实际反馈，修订计划

[AdaPlanner: Adaptive Planning from Feedback with Language Models](https://proceedings.neurips.cc/paper_files/paper/2023/hash/b5c8c1c117618267944b2617add0a766-Abstract.html)（NeurIPS 2023）以环境反馈驱动闭环规划，区分计划内的信息补充和计划外修订，并在子目标检查点判断结果。其代码式计划使用断言检查子目标，实验覆盖 ALFWorld 与 MiniWoB++。

它支持阶段预期、检查点和反馈调整。QHarness 采用粗 Todo 与结构化 StagePlan，行动细节由内层 Agent Loop 在线决定，不照搬完整代码式计划。论文中的两种 refinement 也不等于本文的五类控制路由，后者是进一步提出的实现规则。[论文方法](https://proceedings.neurips.cc/paper_files/paper/2023/file/b5c8c1c117618267944b2617add0a766-Paper-Conference.pdf)

#### RestGPT：根据已执行结果在线确定下一子任务

[RestGPT: Connecting Large Language Models with Real-World RESTful APIs](https://arxiv.org/html/2306.06624)（2023 年起发布的工作）将自然语言子任务、API 选择和调用执行分开。Planner 根据用户目标、历史计划与执行结果生成下一子任务；当前子任务未完成时可以发出 continue 指令。

它支持不同粒度的决策分工和在线规划。QHarness 的内层是通用工具循环，并增加阶段与 Todo 两层验收，而 RestGPT 主要面向 REST API 调用，在 TMDB、Spotify 场景评测，不能直接推出代码修复收益。

#### Voyager：任务执行、环境反馈与 Critic 形成迭代闭环

[Voyager: An Open-Ended Embodied Agent with Large Language Models](https://arxiv.org/abs/2305.16291)（2023）根据环境状态提出任务，生成程序，并结合执行错误、环境反馈和自验证迭代改进。其开放式探索与技能积累目标不同于 QHarness 的用户任务，但执行后检查、失败后带反馈再尝试的结构可借鉴。[作者项目页](https://voyager.minedojo.org/)

[官方 voyager.py](https://github.com/MineDojo/Voyager/blob/main/voyager/voyager.py) 的 step / rollout 路径包含环境执行、check_task_success、critique 回填与成功/次数终止。这是具体代码参考，不是“多个通用工具循环与本设计完全相同”的证据；Critic 使用模型判断，也不等同确定性 Oracle。

#### Anthropic：阶段约定与代码交付，也有减少阶段编排的经验

[Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps) 的早期配置让 Generator 与 Evaluator 在 Sprint 前约定交付与验收。文章后续换用更强模型时取消固定 Sprint，并减少中间评审，说明应评测阶段粒度与检查强度。它是工程案例，不能外推成固定分层一定有益或一定多余。

QHarness 保留阶段开始的目标约定和结束的验证边界，但允许阶段大小变化、复用有效检查结果；进入 Verifier 不等于每次都增加一个独立 LLM Judge。

#### 研究映射与差异定位

| 本设计环节 | 直接参考 | 采用的机制 | 本项目需要验证的部分 |
|---|---|---|---|
| 初步 Todo、按环境继续细化 | ADaPT | 短计划、反馈驱动分解 | 何时需要拆分，何时继续直接执行 |
| 当前上下文决定下一阶段 | RestGPT | 在线子任务规划、从粗到细行动 | 通用代码工具下阶段目标的质量 |
| 阶段预期与反馈修订 | AdaPlanner | 子目标检查、按反馈调整计划 | 分层路由是否减少错误回退与重复规划 |
| 执行—检查—再尝试 | Voyager | 环境反馈与 Critic 反馈回填 | 验证可靠性、明确的阶段/Todo 完成语义 |
| 代码阶段约定与应用检查 | Anthropic 工程案例 | 交付前明确结果与检查方法 | 阶段粒度和检查强度如何随任务变化 |

本设计不把“分层”“反馈”“子目标”本身称为新算法。研究价值在于将第 15 节规则具体化，并在相同模型、环境与总预算下验证其收益与代价。

## 4. 官方实现对照：采用机制，而不是照搬框架

| 系统 / 资料 | 本次确认的机制 | QHarness 的采用方式 |
|---|---|---|
| [OpenAI Agents SDK Runner](https://openai.github.io/openai-agents-python/running_agents/) | 模型调用、工具回填、最终输出、Handoff 与 Turn 限额 | 复用响应分类思路；QHarness 的任务完成另由验收决定 |
| [LangChain The Art of Loop Engineering](https://www.langchain.com/blog/the-art-of-loop-engineering) | Agent、Verification、Event-driven、Hill-climbing 四类循环 | 按时间尺度和状态所有权拆分，避免嵌套大循环 |
| [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | Checkpoint、长期 Store、暂停后显式恢复；恢复可能重进节点开头 | 状态落盘与外部执行分开；恢复前检查节点内副作用，不能认为保存了状态就获得 exactly-once |
| [OpenHands Conversation](https://docs.openhands.dev/sdk/arch/conversation) 与 [Persistence](https://docs.openhands.dev/sdk/guides/convo-persistence) | 会话入口管理生命周期，事件日志与基础状态分离 | 运行事件、状态投影和上下文消息使用不同模型；客户端能够重连重建视图 |
| [mini-SWE-agent](https://mini-swe-agent.com/latest/) | 简洁循环、线性消息历史、Bash-only 动作接口 | 保留简单单轨迹作为对照基线；结构化工具的价值用错误率、成本和恢复能力证明 |
| [AutoGen Termination](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/termination.html) | 可组合的停止条件 | 使用明确的控制决策和持久计数，不按某段自然语言匹配“完成” |
| [Anthropic Agent Evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | 区分结果、轨迹、代码判定器、模型判定器，以及能力与回归评测 | 同时检查产物正确性和运行行为，验证 Judge 本身的误判 |

上述比较基于官方文档，不是对这些项目所有源码的审计。QHarness 不需要引入图运行时来获得这些能力；当前 Python 异步模型、ToolExecutor 和 SQLAlchemy 足以支撑一个职责明确的实现。

## 5. 与当前仓库对齐：已有基础与实际缺口

本节的文件是现有代码；后文新增的类名、表名和接口均为提案。相关事实背景见 [后端推理引擎设计方案](./QHarness-后端推理引擎设计方案.md)。

| 当前基础 | Loop 可复用部分 | 必须补齐或修正 |
|---|---|---|
| [model/models.py](../src/qharness/model/models.py) | ChatMessage、ToolCall、ChatResponse、Usage、流式事件 | 持久消息 ID、响应归一化、Provider 扩展状态、完整性和终止原因判断 |
| [backends/openai_compatible.py](../src/qharness/backends/openai_compatible.py) | complete / stream、原始响应、DeepSeek reasoning_content | 请求尝试账本；中断流状态；SDK 重试与 Loop 重试统一配置 |
| [tools/executor.py](../src/qharness/tools/executor.py) | 参数校验、计数、超时、取消、Hook、结果限制 | 持久执行生命周期、审批暂停而非直接拒绝、恢复去重、结构化结果保存 |
| [tools/base.py](../src/qharness/tools/base.py) | Tool、ToolExecutionRequest / Result、Run 级计数 | Tool Effect 元数据、逻辑调用与执行尝试分离、结果可知性和业务状态 |
| [tools/hooks.py](../src/qharness/tools/hooks.py) | 执行前 Allow / Reject、后置观察 | 当前没有 ASK；需要显式挂起协议，不能靠 Hook 内等待用户维持协程 |
| [run/context.py](../src/qharness/run/context.py) | 租户、工作区、Run、沙箱、取消事件 | RunContext 是重建的运行依赖，不是可序列化 RunState；取消事件与计数要从持久状态恢复 |
| [workspace/mutation.py](../src/qharness/workspace/mutation.py) | 操作台账、私有版本、外部变更检查点和冲突回滚 | 把持久 Tool Execution 与 operation_id 绑定；跨库/文件系统崩溃核对；验证版本绑定 |
| [tools/builtin/run_command.py](../src/qharness/tools/builtin/run_command.py) | 沙箱命令、退出码、进程取消、命令后文件变化记录 | 稳定命令身份、恢复探测、结构化结果传递；不能把 Handler 正常返回当作命令成功 |
| [persistence/](../src/qharness/persistence/) | 应用级数据库、SessionFactory、Alembic | Session / Task / Run / Event / Approval / Evidence 等业务表与仓储 |

当前特别需要处理的四个接缝：

1. **两层成功语义。** ToolExecutionResult.success 表示 Handler 是否正常执行返回；run_command 内容中的 succeeded / exit_code 才表示命令结果。任务完成还要第三层验收。Loop 不应解析中文文案，也不应仅依赖最外层 success。
2. **恢复与计数。** 当前执行器每次进入都会预留工具次数。审批后重新调用或结果重放不能再次消耗同一个逻辑 Tool Call 的配额；真正新发起的错误调用仍计数。应把 admission 与 execution attempt 分离。
3. **结果截断。** 当前执行器先序列化再按字符限制结果，可能丢掉供判定使用的结构。应先保存结构化字段与有限大小的完整输出 Artifact，再编译模型可见摘要。超过采集上限的日志须标明不完整，不能声称完整留存。
4. **配置语义。** 全局 max_total_calls / max_concurrency 默认 None 表示无全局上限；当前单工具默认 max_calls=10，覆盖项 None 表示继承。新的 Run Budget 与 Profile 必须展示合并后的实际限制，不能悄悄改变现有含义。

## 6. Agent 推理主流程：Todo、动态阶段与工具循环

### 6.1 已确定的三层逻辑

本节描述推理怎样推进任务。持久化、可观测性和生命周期是执行支撑，见第 6.6 节及后续章节。这里的 Planner、Executor 与 Verifier 是职责划分，不要求对应三个独立 Agent。

~~~text
用户目标与完成条件
          │
          ▼
Planner：生成初步 Todo List
          │
          ▼
任务推进器：选择依赖满足的当前 Todo
          │
          ▼
┌──────────────── Executor Loop ─────────────────┐
│ 根据 Todo、当前环境和上轮反馈作阶段规划决策       │
│ 明确目标、预期结果、要解决的缺口和交回条件         │
│                      │                         │
│                      ▼                         │
│  ┌────────────── Agent Loop ────────────────┐   │
│  │ 上下文 → 模型决策 → 工具执行 → 环境观察    │   │
│  │            ↑_____________________│      │   │
│  │ 阶段候选 / 前提失效 / 阻塞 / 停滞时交回   │   │
│  └───────────────────┬─────────────────────┘   │
│                      ▼                         │
│ Verifier：检查阶段结果、Todo 完成度与有效进展     │
│                      │                         │
│ Todo 未完成 → 分类反馈 → 下一轮阶段规划决策 ─────┤
│ Todo 完成   → 退出当前 Executor                 │
└──────────────────────┬─────────────────────────┘
                       ▼
更新 Todo 状态，推进下一项
                       │
所有 Todo 都通过，且任务要求没有遗漏
                       ▼
Task 整体验收
  ├─ 通过 → 完成交付
  └─ 遗漏 / 集成问题 / 回归 → 重开相关 Todo 或补充修复项
~~~

核心过程是：**粗粒度 Todo 提供方向，Executor 滚动决定本轮要得到什么，Agent Loop 自主决定具体动作，验证反馈决定下一轮如何推进。**

每个 Executor 轮次都有阶段规划决策。对于目标与方法仍有效的局部错误，决策可以是保留 StagePlan、补入修复反馈；它不必每次生成一份全新计划，也不必额外调用模型。创建新阶段、调查或改变方向时，再使用模型生成并校验新的 StagePlan。

这三个逻辑层次共享同一 Task 目标和总预算。工具调用轮次、阶段轮次与 Todo 数量互不等同。

### 6.2 各角色的职责和模型调用边界

| 角色 | 输入与输出 | 决策范围 |
|---|---|---|
| Global Planner | 用户目标、初始上下文 → TodoPlan | 生成初步 Todo、依赖和完成条件映射；遇到全局前提变化时修订 |
| Todo 推进器 | Todo 状态、依赖、任务缺口 → 当前 Todo | 按规则选择可执行项；没有可执行项时查阻塞或依赖问题，不忙循环 |
| Executor / Stage Planner | 当前 Todo、观察、有效发现、上轮反馈 → StagePlan 或保留计划 | 决定本轮目标、预期结果、阶段粒度、执行思路和交回条件 |
| Action Agent Loop | 当前 StagePlan、最新上下文 → 工具动作或 StageOutcome | 决定读哪里、查什么、如何修改和验证，处理普通工具错误 |
| Verifier | 阶段约定、Todo 完成条件、实际结果 → StageVerdict | 核验预期与实际差异、缺口变化；不直接修改计划或自行降低要求 |
| Feedback Router | StageVerdict、有效假设、执行状态 → FeedbackDecision | 选择保留、修复、调查、阶段重规划、Todo 重规划或交回等待 |
| Task Verifier | 原始任务要求、当前产物、各 Todo 证据 → TaskVerdict | 检查整体要求、集成行为和回归，决定是否真正完成 |

Global Planner 只给足够启动执行的 Todo List，不要求列出全部文件编辑与工具调用顺序。信息不足时可先形成调查 Todo，或让当前 Todo 的第一阶段调查；不为猜测的实现细节构造一长串固定依赖。

Stage Planner 与 Action Agent 可以复用同一个模型。前者侧重“本轮解决哪个问题、得到什么结果”，后者侧重“下一次具体做什么”。使用不同输入投影和结构化输出，不强制多模型、子 Agent 或独立进程。

Verifier 尽量复用已有有效工具结果，缺少证据时才申请 Check Runner；需要语义判断时才调用受限 Judge。阶段验证是必经的逻辑判断，不意味着阶段结束必定重新跑全部测试或新增一次 LLM 调用。

### 6.3 Todo、StagePlan 和结果的约定

**Todo** 是具有持续完成条件的工作项；**Stage** 是为推进该 Todo 而动态选择的一段工作。将“步骤”明确成这两个概念，避免与一次工具 Step 混用。

~~~python
Todo(
    id="T1",
    objective="修复分页漏记录",
    acceptance_refs=["C1"],
    dependencies=[],
    done_when=["末页、整页和空页的相关行为符合要求"],
)

StagePlan(
    stage_id="S1",
    todo_id="T1",
    plan_version=1,
    kind="INVESTIGATE",
    objective="确认遗漏发生在查询层还是结果组装层",
    addresses=["Q1"],
    assumptions=["当前环境能够复现用户描述的问题"],
    expected_results=[
        "得到可重复的失败输入",
        "定位首次出现数据缺失的处理环节",
    ],
    approach="对比查询原始结果与接口返回结果",
    stop_when="已有足够证据确定下一步调查或修复方向",
    replan_when=[
        "复现前提不成立",
        "发现问题来自当前调查范围之外",
    ],
)
~~~

StagePlan 的 objective / approach 可以随新发现变化；Todo 的 done_when 必须仍忠实于用户要求。换方法不等于改变完成标准。不得把“修好问题”自动降级为“找到原因”后结束 Todo。

addresses 必须引用一个待满足的验收项或一个真实的未决问题；新增问题要说明它为何影响当前 Todo，不能随意创建容易完成的支线。expected_results 应有可观察结果与检查方法；“充分思考”“深入理解”不构成独立验收条件。数据结构校验只证明字段合法，不证明目标在语义上合理。

stop_when / replan_when 是经校验的条件描述或注册的检查引用，不是模型提供的宿主可执行代码。Action Agent 可以提议条件已满足，但状态变化由控制器依据结果确认。

内层 Agent Loop 交回：

~~~text
StageOutcome:
  stage_id, plan_version
  status: CANDIDATE | NEEDS_REPLAN | BLOCKED | STALLED
  summary, outputs, observation_refs
  reported_assumption_changes, remaining_questions
~~~

Verifier 返回：

~~~text
StageVerdict:
  stage_id, plan_version, todo_id
  stage_status: PASS | FAIL | INCONCLUSIVE | ERROR
  todo_status: PASS | FAIL | INCONCLUSIVE | ERROR
  expected_vs_observed[]
  resolved_questions[], satisfied_criteria[], regressed_criteria[]
  remaining_gaps[], evidence_refs[]
  diagnosis_hints[]               可选假设，不冒充已证明原因
~~~

stage_goal_met 与 todo_done 可以作为派生布尔字段，但底层要保留四种状态；没有足够证据时是 INCONCLUSIVE，不能把 false 全都解释为实现失败。阶段结果良好也不能自动把整个 Todo 标为 PASS。

### 6.4 Executor 每轮怎样推进

一次 Executor 轮次按下面顺序执行：

1. **整理当前任务状态。** 读取 Todo 完成条件、未决问题、已有有效发现、最新环境与反馈，不要求重新阅读全部历史。
2. **作阶段规划决策。** 选择继续、局部修正、调查或新阶段；明确 addresses、预期结果与交回条件。
3. **运行内层 Agent Loop。** 模型每轮根据新观察选择工具，不在每个工具调用前再运行独立 Planner。
4. **在边界交回。** 得到阶段候选、发现前提失效、阻塞或停滞时返回 StageOutcome。普通可修复参数错误可在内层解决。
5. **验证与更新判断。** 核验阶段结果、Todo 完成度和缺口变化；验证时前提失效、未执行或错误的检查均不能当作 PASS。
6. **选择下一轮。** Todo PASS 才退出 Executor；阶段 PASS 但 Todo 未完成则继续；失败按第 15.1 节路由，不默认重做整个任务。

内层在 NEEDS_REPLAN / BLOCKED / STALLED 时仍经验证与分类入口，但该入口可以只核对已有结果和阻塞事实，不强制先执行一遍完整验收套件。用户取消、授权撤销与未知副作用由生命周期控制优先处理，不交给规划模型猜测是否继续。

局部修复返回同一 stage_id / plan_version 的新执行尝试；阶段目标或方法实质变化则产生新版本或新阶段并记录替代关系。已得到的有效发现继续使用，只有被证据否定或已过期的部分失效。

### 6.5 示例：同一个 Todo 的动态阶段轨迹

以下是设计示例，不指当前仓库存在该缺陷。用户要求“修复分页遗漏最后一条记录，保持原有响应格式”。

初步 Todo 可包括 T1“修复遗漏”和 T2“检查调用方兼容与回归”。Task 验收条件包括 C1“分页数据完整”、C2“原有行为不回归”、C3“响应格式兼容”。Todo 可以共享相关检查，但所有条件最终都要在任务层核对。

| Executor 轮次 | 当前判断与阶段目标 | 内层行动 | 验证反馈与下一步 |
|---|---|---|---|
| T1 / 第 1 轮 | 原因未知；解决 Q1“查询还是组装环节丢数据” | 搜索、读调用链、构造边界输入、比较中间结果 | 查询完整而组装后遗漏，Q1 关闭；阶段通过，T1 未完成 |
| T1 / 第 2 轮 | 修复结果组装边界，并检查相关输入 | 修改条件、运行末页和整页测试 | 整页仍失败；定位为具体分支遗漏，选择 REPAIR |
| T1 / 第 3 轮 | 保留修复目标，补齐已定位分支 | 修正分支，复用有效输入并补测 | 相关分页行为通过，T1 完成；不是重做第 1 轮调查 |
| T2 / 第 1 轮 | 验证兼容性与原有调用行为 | 检查响应结构、运行相关回归 | C2、C3 得到有效证据；T2 完成 |
| Task 验收 | 核对全部要求与最终产物 | 复用仍有效证据，补充缺失的集成检查 | 全部满足才交付；发现遗漏则重开相关 Todo |

如果第 1 轮发现数据在上游已经缺失，则重新选择调查目标；如果发现需要先修复另一依赖，则调整 Todo。判断位置取决于实际反馈，不是固定执行“调查一次、修改一次、验证一次”。

如果验证后代码又发生变化，相关证据失效并重新检查；如果执行中断，恢复到相应 Todo / Stage / Attempt 后继续。它们支撑同一推理过程，不改变用户目标或自动重置预算。

### 6.6 推理流程怎样接入运行时

上述三个循环是逻辑层次。实现可由一个可恢复状态机逐步调度，避免把多个长期等待的 Python while / 协程堆在一起。

~~~text
RunService + Lifecycle Controller
  输入、暂停、取消、恢复
                │
                ▼
Loop Controller：唯一状态推进者
  TodoPlan → StagePlan → Action Agent Loop → StageVerdict
                │                              │
                └──────── FeedbackDecision ◄───┘
                │
   ┌────────────┼────────────────────┐
   ▼            ▼                    ▼
Context      ModelBackend      Check Runner / Verifier
Compiler        │                    │
   ▲            ▼                    ▼
   │      Action Scheduler       Evidence
   │      Policy + Budget            │
   │            ▼                    │
   │       ToolExecutor              │
   │       Workspace / Sandbox       │
   └──── Observation / Version / Artifact
~~~

Global Planner、Stage Planner、Action Agent 与模型 Judge 的调用均通过同一模型服务计量与记录；工具和检查统一经过受控执行边界。逻辑角色可以实现为小函数、策略对象和 Prompt 模板，不要求引入通用多 Agent 框架。

Event-driven 入口负责提交或恢复任务，Cron、文件监听等是接入器，不参与每一步推理。Policy Gate 判断授权，Verifier 判断结果，Context Compiler 组织可见信息，Feedback Router 判断调整位置；控制器校验并提交最终状态转换。

### 6.7 统一实体与计数

| 实体 | 含义与所有权 |
|---|---|
| Session | 用户可见对话容器，可含多个 Task 和 Run |
| Task / Contract | 持续目标和验收要求，可跨多个有关联的 Run |
| Todo / TodoPlan | 初步工作分解、依赖、完成条件映射；全局 Planner 可有据修订 |
| Stage / StagePlan | Executor 动态选择的一段调查或实施工作及其结果约定 |
| Run | 一次有独立资源账本的运行；暂停恢复保持 run_id |
| Attempt | 同一 Stage 的执行或修复尝试；与检查重试、模型网络重试分别计数 |
| Turn | 一次逻辑模型决策及其工具批次；记录角色，不能与 Executor 阶段轮次混用 |
| Step | 可独立记录与恢复的动作，如模型请求、工具或检查执行 |
| Candidate | 带 scope=STAGE / TODO / TASK 的候选结果，绑定相应对象、契约及版本 |
| Checkpoint | 可重建状态的快照，包含当前 Todo / Stage / Attempt，不等于代码 Commit |
| Context Epoch | 一段模型上下文；压缩和重新开窗不创建新任务或补充预算 |

### 6.8 Run 状态与完成语义

~~~text
CREATED → PREPARING → RUNNING
                       │
                       ├─ 阶段候选 → VERIFYING
                       │                ├─ Todo 未完成 → RUNNING（继续阶段循环）
                       │                ├─ Todo 通过 → RUNNING（下一 Todo / Task 验收）
                       │                └─ Task 验收通过 → COMPLETED
                       ├─ 审批 → WAITING_APPROVAL → RUNNING
                       ├─ 必要信息缺失 → WAITING_INPUT → RUNNING
                       ├─ 暂停 → PAUSED → RUNNING
                       ├─ 结果未知 / 崩溃 → RECOVERING → RUNNING / WAITING_INPUT / FAILED
                       ├─ 预算不足 / 无进展 → WAITING_INPUT / EXHAUSTED
                       └─ 取消 → CANCELLING → CANCELLED
~~~

RUNNING 内区分 PLANNING_TODOS、PLANNING_STAGE、ACTING、ROUTING_FEEDBACK 等推理阶段；VERIFYING 带 scope，不能因一次阶段 PASS 就完成 Run。终态是 COMPLETED / CANCELLED / EXHAUSTED / FAILED；等待和暂停可恢复，函数返回 RunSnapshot 不表示任务已完成。

COMPLETED 表示满足当前 Task 契约，结果同时暴露 assurance=VERIFIED 或 UNVERIFIED。只有契约明确允许且没有未处理失败项时，才允许 UNVERIFIED 交付；生产代码修改 Profile 默认要求 VERIFIED。终态后继续工作创建有关联的新 Run，Task 层可汇总开销。

### 6.9 核心不变量

1. StagePlan 必须关联当前 Todo 的验收项或有依据的未决问题；规划不能自行降低用户要求。
2. 阶段完成、Todo 完成与 Task 完成分别检查；本轮目标已达成不能直接结束任务。
3. 路由只调整被反馈影响的层级；原因未明先补证据，不能无依据重写全局计划。
4. 工具只能来自完整、校验并持久化的模型响应；半截响应不触发动作。
5. 下一次行动模型决策前，上一批工具必须已有终结观察或明确停在等待/恢复状态。
6. 逻辑工具调用准入一次，实际执行尝试分别计量；规划、压缩、验证与修复共享总预算。
7. 副作用先记录意图后记录结果；未知结果进入核对，不自动等价为安全重试。
8. 完成判定只使用匹配当前契约、计划和产物版本的证据。
9. 模型与环境文本不能直接写入已批准、已验证或生产策略字段。
10. 数据库事务不跨外部调用，锁和信号量不跨用户等待；推理循环通过状态重建恢复。

## 7. 任务契约、计划与用户协作

### 7.1 Task Contract 是验收依据，不是模型自己扩写的需求清单

建议最少包含：

~~~text
task_id, contract_version, original_request_ref
goal                       用户目标
deliverables               文件、补丁、回答或其他产物
acceptance_criteria[]       稳定 ID、说明、检查方法、是否必需
scope_constraints          允许的目录、变更范围、兼容性等
authorization_refs         已有授权的引用；不能从普通工具内容生成
verification_policy        所需证据、允许的降级、修复策略
assumptions[]               暂定假设及其来源
~~~

用户不必手填 JSON。调用方可提供契约；否则由模型提议、确定性代码校验并持久化。简单请求可以直接产生一个验收项，不必额外调用 Planner。只有影响目标、范围或授权的实质歧义才请求用户输入；普通实现选择由 Agent 自主处理。

验收项可以在探索后细化，但不能通过删掉失败要求或降低标准“获得通过”。修改既有验收语义必须形成新 contract_version，记录依据；超出已有用户意图的变更需要用户决定。单纯细化检查命令、补充佐证可以自动完成，保留修订记录即可。

### 7.2 初步 Todo 与滚动阶段计划

TodoPlan 保存稳定 Todo ID、目标、依赖、验收映射和状态。状态建议为 PLANNED / ACTIVE / CANDIDATE / VERIFIED / BLOCKED / SUPERSEDED。SUPERSEDED 只表示计划重组中的替代关系，不能消除原 Task 要求。

初次 Planner 形成足够启动的工作分解；只有简单且已明确的任务才可由调用方直接提供一个 Todo。两种入口都进入相同 Executor 语义，不必为了产生一条显而易见的 Todo 再支付一次模型调用。

StagePlan 属于当前 Todo 的执行策略，随调查与反馈滚动形成，不能混写成全局 Todo 的固定工具清单。Todo 依赖图需要校验不存在环、悬空引用和无法解释的跳过项；全部 Todo 通过也要做 Task 级要求覆盖检查。

模型可提议计划、CANDIDATE 和 ProgressDelta；VERIFIED 由验证聚合器写入。进度同时保留阶段状态、验收项、未决问题与证据变化，具体规则见第 15.3 节。

REPLAN_TODO 应输出带原因的最小计划变更：增加前置项、调整依赖、拆分受阻项或替换错误方案。保留仍有效的完成项与发现，显式标记受影响检查的失效；不默认删掉整张列表再生成。实现分解可以自动调整，涉及用户目标、范围或授权的变化按第 7.1 节处理。

### 7.3 运行中消息的语义

客户端发送带 event_id 和 expected_run_version 的输入事件：

- **STEER**：补充当前目标或约束，在安全边界应用，必要时提升契约版本。
- **ANSWER / APPROVAL**：必须关联待处理问题或审批 ID，不能消费到错误的 Run。
- **PAUSE / CANCEL**：暂停允许继续；取消结束本次 Run。
- **NEW_TASK**：独立任务排队或新建 Run，不与当前工具批次交错。

新要求在模型调用期间到达时，响应按旧 context_version 标记。持久化响应后，先处理输入并重新检查尚未执行的动作；已失效的动作返回 SKIPPED 观察，再基于新上下文规划。不能在新约束已经生效后，仍盲目执行旧模型回复中的写操作。

普通聊天内容不能被恢复器猜成“允许重放未知付款/发布操作”。审批决定必须有可审计的明确来源；已经有效的授权可按范围复用，不因每个新 Turn 重复询问。

## 8. Agent Step 与工具调度

### 8.1 模型响应的明确分类

| 响应 | 处理 |
|---|---|
| 完整、合法 Tool Calls，可伴随说明文本 | 保存 assistant 响应及工具批次，进入调度；说明文本不是最终交付 |
| 合法停止、无工具调用且输出满足预期格式 | 按调用角色分类：Planner 返回计划，Action Agent 返回阶段候选，Judge 返回判定建议；不能统一视为 Task 完成 |
| length / 截断、仅部分工具参数、连接中断、结束原因缺失 | 记录不完整响应；不执行其中的工具，不判完成；按 Adapter 能力选择重新请求或受控续写 |
| 有完整调用 ID，但工具不存在或参数不合法 | 产生结构化错误观察，允许模型修正；消耗一次逻辑调用配额 |
| 缺失/冲突的调用 ID、无法安全配对的响应 | MODEL_PROTOCOL_ERROR；由 Adapter 判定可否修复，不能随便补一个 ID 后执行 |
| Provider 明确拒绝或过滤 | 记录单独原因，按任务策略交付说明或等待输入，不当作成功解决任务 |
| 空白响应或不支持的输出类型 | 有界协议修复或失败，不能进入无成本的无限重试 |

Action Agent 应能输出第 6.3 节 StageOutcome。兼容仅返回自然语言的 Provider 时，使用受校验的结果提取或显式阶段提交协议；缺失完成字段只能进入候选判定，不能因一段普通说明结束 Todo。Planner 与 Judge 的输出也必须经过各自 Schema 和语义检查，不能当作执行工具的授权。

Provider 兼容是能力矩阵，不是“名字包含 OpenAI-compatible 就全部通用”。应显式声明工具批次、流式工具参数、结构化输出、推理字段和续接协议。当前 ChatMessage 已保留 DeepSeek reasoning_content；持久化与投影要保留服务已返回且协议需要的字段，不伪造、索取或依赖未暴露的隐藏推理。

### 8.2 流式输出与执行边界

TEXT_DELTA / TOOL_CALL_DELTA 可供界面实时展示，但都属于 provisional 数据。只有完整响应聚合、结束语义校验、响应和工具意图持久化完成后，工具才能开始执行。

流中断时保存接收状态与已知 Usage；不把未知 Token 使用量记成零。模型重新请求使用新的 request_attempt_id，旧半截响应不拼接为伪造的 assistant 消息。若模型服务没有可靠续接能力，就从最后的完整上下文重新请求。

客户端断连不等于取消。运行与订阅解耦：UI 可重连读取持久事件，慢消费者只影响显示；不能因为浏览器关闭就丢掉工具结果。

### 8.3 Tool Effect 与调度元数据

现有 Tool 尚未包含这些声明，需要新增独立描述或字段：

~~~text
effect: READ_ONLY | WORKSPACE_WRITE | EXTERNAL_WRITE | UNKNOWN
resources: read_set / write_set / workspace_exclusive
replay_policy: SAFE_REISSUE | IDEMPOTENCY_KEY | RECONCILE | NEVER_AUTO
timeout_behavior: COOPERATIVE | PROCESS_TREE | MAY_CONTINUE
output_schema, tool_version, capability_requirements
~~~

这些声明由可信工具实现提供，不能由模型临时决定。run_command 默认 UNKNOWN 或 workspace_exclusive，不能因为命令以 pytest / cat 开头就自动当成只读。测试可能写文件，Shell 也可能隐藏网络或外部副作用。

安全并发的条件是：已知无依赖、资源不冲突、观察一致性有定义，并且全局与单工具资源许可均可用。同批 read_file / search_text 可以组成只读并行段；遇到写操作或未知作用域就设置屏障。写后读取必须看到写后的版本；同一工作区的任意 Shell 与写工具不能交错。

不同 Run 可以在不同工作区并行；同一物理根目录即使使用不同逻辑 workspace_id，也必须映射到同一资源锁。进程锁只约束 QHarness，不能阻止 IDE 或外部进程修改，因此还要记录读取指纹、修改前置条件和运行后核对。

### 8.4 批次结果和失败处理

工具可以乱序完成，持久事件记录实际顺序；模型可见观察按原始调用索引稳定排列，每个结果保持原 tool_call_id。

- 某工具业务失败后，可继续执行明确独立且安全的只读调用。
- 依赖该工具的后续动作产生 SKIPPED(reason, dependency_id)，不能凭空使用缺失结果。
- 审批等待保留未执行调用；未知副作用阻止冲突操作继续。
- 已完成结果不可因同批另一个工具失败而丢失。
- 无法推断调用间依赖时，以模型给定顺序串行处理。
- 同一逻辑批次闭合后再发下一次模型请求；暂停恢复可继续未完成部分。

### 8.5 结果模型需要表达三个维度

建议把工具结果扩展成 ObservationEnvelope：

~~~text
execution_id, model_call_id, tool_call_id, tool_name
execution_status: SUCCEEDED | FAILED | DENIED | SKIPPED | CANCELLED
outcome_certainty: KNOWN | UNKNOWN
domain_result: typed payload，例如 exit_code / tests_collected / matches
effect_refs: operation_id / commit_id / external_receipt
artifact_refs, output_complete, error_code, warnings, elapsed, usage
~~~

execution_status、业务 payload 和验收状态互不替代。timeout / cancelled 也可能已经产生已知的部分文件变化；MAY_CONTINUE 的同步 Handler 在取消等待后可能仍在运行，这时 certainty 必须体现不确定性，并保留资源隔离直到确认停止。

后置日志 Hook 失败只追加 warning，不把已成功执行的写操作改成可重试失败。当前 ToolExecutor 已有这个保护，应保留并扩展到持久结果路径。

## 9. Verification 与修复：让“完成”有可检查含义

### 9.1 把采集证据、判定与控制分开

建议拆成三个角色，角色不等于三个 LLM：

- **Check Runner**：运行测试、类型检查、命令、Schema 检查或应用操作，产生 Evidence。
- **Verifier**：按当前 StagePlan、Todo 和 Task Contract 核对证据，给出局部与整体结果及差异。
- **Verification Controller**：按 scope 聚合状态、证据与 ProgressDelta，再交 Feedback Router / Loop Controller 决定推进、修复、重测或等待。

Check Runner 复用同一套沙箱、权限、取消、资源锁和预算基础设施。它可以是确定性程序，也可以是受限的应用检查 Agent；调用来源标为 verifier，不能隐藏额外工具与模型开销。

Verifier 不直接修复源代码。测试和应用操作可能有副作用，应在隔离 Fixture、产物目录或受控工作区执行，并记录影响；对于不能隔离的检查，契约必须说明允许的范围。

### 9.2 Evidence 必须带版本和适用范围

~~~text
evidence_id, kind, criterion_ids
producer: tool/check/model_review/human
run_id, step_id, invocation_ref
contract_version
workspace_fingerprint:
  private_commit_id, tracked_tree_digest, live_dirty_digest
  relevant_file_digests, ignore_policy_version
environment_fingerprint:
  runtime_version, dependency_lock_digest, config_digest, fixture_id
check_definition_digest, started_at, finished_at
status, exit_code, collected_count, summary
artifact_refs, completeness, invalidation_reason
~~~

不能仅绑定私有 HEAD：测试之后用户可能直接编辑文件，HEAD 还没变；被忽略的依赖、配置和数据库也可能影响结果。需要按检查种类声明输入范围，不能声明时采用保守失效策略。

证据复用条件是相关代码、检查定义、契约要求和环境输入仍匹配。任何相关变化都会使旧结果 STALE；数据库、网络服务等无法固定的状态还应设置有效期或明确不允许缓存。**过期证据留在历史中，但不能参加当前完成判定。**

增量复用是一种优化：最初可以保守地使工作区相关证据整体失效；只有依赖分析经过评测，才缩小失效范围。依赖图不完整时，不能为了省一次测试接受旧结果。

### 9.3 Verification Result 与聚合

每个验收项返回：

~~~text
scope: STAGE | TODO | TASK
subject_id, subject_version, criterion_id
status: PASS | FAIL | INCONCLUSIVE | ERROR
evidence_refs
finding: 实际观察与期望差异
repair_hint: 可选建议，不是事实
~~~

其中 FAIL 表示明确违反要求；INCONCLUSIVE 表示缺少证据、覆盖不足或结果不稳定；ERROR 表示检查器或环境本身出错。Verifier 不返回“再来一次就好”这种混合控制结果。

完成聚合规则：

1. 各 scope 独立聚合：阶段条件 PASS 只标记 Stage；Todo 的全部条件 PASS 才结束当前 Executor；Task 的全部必需项和整体验收 PASS、证据有效且没有未知副作用，才允许 Run VERIFIED 完成。全局要求未覆盖时，不能因 Todo 列表全勾选而放行。
2. 对 FAIL / INCONCLUSIVE 生成包含预期差异与缺口变化的 StageVerdict，按第 15.1 节分层路由。已定位的局部错误建立同一 Stage 下的新 Attempt；新假设或目标需要新的 StagePlan；不会把所有失败都当成同一种重试。
3. 环境 ERROR 可以重试检查或修复环境，但必须遵守动作授权；不自动要求模型乱改业务代码。
4. INCONCLUSIVE 先补采证据；若需要权限、信息或超出自动处理范围，进入 WAITING_INPUT。
5. 预算不足或修复不再有效时，输出已完成部分、剩余问题和证据，以 EXHAUSTED 或可恢复等待结束本次推进。
6. 用户允许降低验收要求时形成新的契约版本；不回写旧证据为 PASS。

### 9.4 检查强度按任务与变更风险决定

| 场景 | 默认检查组合 | 升级触发 |
|---|---|---|
| 解释代码 / 回答问题 | 引用定位、目标覆盖、答案格式；必要时人工判断 | 结论依赖运行行为或存在不确定事实 |
| 修复局部缺陷 | 可复现失败、针对性回归、相关原有测试、Diff 范围 | 修改公共接口、依赖或基础配置 |
| 增加跨模块功能 | 验收项到测试映射、集成检查、兼容性检查 | 关键路径、迁移、权限与并发变更 |
| UI / 可运行应用 | 构建加实际用户流程检查，保留截图/操作证据 | 交互失败、主观质量要求或浏览器兼容性 |
| 外部操作 | 动作权限、请求回执、外部状态核对 | 结果未知、不可逆或状态前提变化 |

简单任务不必支付独立 LLM Judge 的成本；主观质量检查可以使用独立上下文的 Judge，但应提供明确 Rubric、固定版本和人工校准集。多个同源 Judge 的一致不能视为独立证据，也不能覆盖确定性 FAIL。

### 9.5 防止“测绿”替代完成任务

- 在修改前建立可行的基线；区分原有失败、引入失败与环境故障。
- 检查命令是否真正启动、是否收集了预期测试、是否跳过关键用例，退出码为零不够。
- 验收定义和受保护检查位于 Agent 无法任意改写的控制数据中。
- 测试代码可以是正常交付内容；测试被修改要显式记录并重新判断其独立性，不能一律禁止，也不能自动信任。
- Flaky 测试按预先规定的重测规则处理，保存所有结果，不能只保留最后一次通过。
- 最终检查与交付建立同一个产物指纹；检查后继续编辑，必须重新验证受影响项。
- 本地沙箱不能证明网络服务、浏览器或部署环境中的行为；未执行的检查应清楚展示。

### 9.6 Failure Bundle 与有目的的修复

反馈给模型的材料应包含：当前 Todo / StagePlan、失败验收项、预期与实际差异、原始日志引用、当前文件版本、已尝试的方法及结果、仍有效与被否定的假设、禁止破坏的已通过行为、剩余预算。该材料用于第 15.1 节的分层路由，不直接要求重做全局计划。

修复策略由具体失败选择：

~~~text
参数错误 → 重新调用正确工具
代码缺陷 → 定位并修改相关代码
环境失败 → 检查运行时/依赖/启动条件
旧上下文 → 重新读取最新文件
相同方案反复失败 → 补证据或重新规划
不确定副作用 → 转入 Recovery，先核对结果
~~~

不默认每次 FAIL 都回滚整个工作区。当前安全回滚按操作和文件摘要校验，适合撤销明确的 Agent 变更；它不等于任意环境快照。修复产生新修改后，即使曾有更好的候选，也必须确认能安全恢复、且验证仍有效，才能选用它。

## 10. Context Compiler 与工作记忆

### 10.1 三种数据，各有事实来源

| 数据 | 用途 | 写入与可信度 |
|---|---|---|
| 原始事件、消息、工具结果、Artifact | 审计、恢复、重新检索 | Harness 记录的事实；内容本身仍可能来自非可信环境 |
| Task / Evidence / Progress 结构化状态 | 目标、有效进度、验收与控制 | 控制器校验写入；区分模型提议和已核验字段 |
| 编译后的模型 Context 与摘要 | 帮助下一步决策 | 可丢弃重建的输入投影；不作为审批或恢复事实来源 |

### 10.2 编译流程

1. 固定系统策略、工具版本和当前 Provider 能力。
2. 加入当前任务契约、最新用户约束和剩余预算摘要。
3. 根据角色加入当前 Todo、StagePlan、未决问题、ProgressDelta 和反馈：Global Planner 看总体覆盖与依赖；Stage Planner 看当前缺口与有效发现；Actor 看本阶段目标及交回条件；Verifier 看约定、产物与检查证据。
4. 保留最近完整的模型—工具交互组。
5. 按任务需要检索文件片段、历史观察与 Artifact。
6. 必要时进行结构化压缩，再投影到 Provider 请求格式。
7. 校验工具调用配对、Token 容量和不可丢失字段，记录 ContextManifest。

ContextManifest 保存输入来源 ID、文件版本、摘要版本、工具 Schema 版本、模型能力版本和 Token 估算，便于解释“这一步模型看见了什么”。

### 10.3 预算与压缩规则

输入可用空间应满足：

~~~text
instructions + tools + task_state + recent_messages + retrieved_evidence
    <= context_capacity - reserved_output_tokens - safety_margin
~~~

上下文窗口能力和 Token 估算由模型配置与 Adapter 提供；不知道准确 Tokenizer 时记录估算方式，并为溢出处理留出余量。压缩触发阈值是可配置策略，不是所有模型统一 80% 的定律。

优先移除可重新读取的大块日志和过期片段，其次压缩已完成阶段，最后才减少仍活跃的任务细节。不能拆开 assistant Tool Calls 与对应 Tool Results；无法安全保持协议时，在完整工具批次结束后建立新的 Context Epoch，用结构化交接重建请求。

压缩后必须保留用户约束、未完成目标、错误原因、有效证据引用、版本前提、待定决策和下一步。重复对上一版摘要做全文重写容易积累丢失，可采用 ACE 启发的增量条目，并定期回到原始记录核对。该做法是本项目方案，不等于已经复现 ACE。

没有足够空间保留必要信息时，返回明确的 CONTEXT_BLOCKED 控制原因，不能无限压缩或默默删掉任务要求。压缩调用同样计入模型、Token 和时间预算。

### 10.4 记忆、检索与信任边界

工作记忆条目带 source_refs、scope、created_at、适用版本和失效条件。项目经验可以跨 Run 复用，但不能跨租户泄露，也不能把一次偶然测试通过升级为永久事实。

代码、文档、工具输出和外部网页作为带来源的数据进入上下文，不自动提升为系统指令；权限由 Policy Gate 执行。仅靠提示词不能保证阻断注入，但可以避免把环境文本直接解释成控制事件。

知识性笔记可以在当前任务内自动更新；全局 Prompt、权限策略、工具实现与跨任务经验规则的变更进入离线评测和版本发布流程。它们不能被当前执行中的 Agent 静默自修改。

## 11. 生命周期、持久化和副作用恢复

### 11.1 Checkpoint 不等于 exactly-once

模型、数据库、文件系统、沙箱进程和外部服务不共享一个事务。QHarness 可以保证本地状态更新与事件写入的原子性；不能仅凭 SQLite 记录就保证任意外部动作恰好发生一次。

建议持久执行记录：

~~~text
logical_call_id                  QHarness 内部稳定身份
provider_call_key               model_call_id + 原始 tool_call_id
execution_id                    单次执行尝试
operation_id / idempotency_key   外部副作用关联身份
original_arguments, effective_arguments, arguments_digest
tool_version, authorization_ref, workspace_precondition
state: PREPARED | DISPATCHED | SUCCEEDED | FAILED | UNKNOWN
result_ref, effect_refs, reconciliation_ref
~~~

Provider Tool Call ID 只在相应模型响应范围内使用，不能拿它作为全局唯一业务键。同 ID 内容变化必须报冲突；“工具名和参数一样”也不意味着重复，因为合法的轮询和重复测试可能完全相同。

### 11.2 工具执行事务边界

~~~text
短事务 A：准入计数 + 执行意图 + 关联 operation_id + 事件
    ↓
重新核对授权、取消、资源与工作区前置条件
    ↓
短事务 B：标记 DISPATCHED
    ↓
数据库事务外执行工具
    ↓
保存 Artifact / 核对 Workspace 操作
    ↓
短事务 C：持久结果 + 账本结算 + 完成事件 + 可恢复状态
~~~

实际派发与标记 DISPATCHED 之间仍有故障窗口，所以后续可能只能判为 UNKNOWN。设计的价值是明确处理这个窗口，而不是声称把它消除了。

现有文件工具和 run_command 在 Handler 内建立 operation_id。需要改造为可接受内部可信执行上下文中的稳定 ID，并让 Loop、Workspace、Sandbox 共同关联；不能把幂等键作为模型随意填写的普通参数。数据库、Dulwich 和磁盘不一致时，恢复器核对台账、Blob、当前文件及进程回执，不能只看一张表。

### 11.3 恢复决策表

| 最后已知状态 | 恢复动作 |
|---|---|
| 模型请求未完整返回，尚无工具派发 | 保留不完整记录，计量已知/估计成本，重新发起模型请求 |
| 工具结果已经持久化 | 重建观察和上下文，不重新执行、不重复准入计数 |
| PREPARED 且可信记录能证明未派发 | 重新校验权限、版本和预算后继续派发 |
| 只读工具 DISPATCHED，结果丢失 | 可以重新采集，但记录新时间与新环境版本，不冒充原观察 |
| 支持幂等键的外部操作 | 使用同一业务键核对/重试，遵守服务的有效期与幂等范围 |
| 文件操作或 Shell 结果未知 | 先对账 Workspace 台账、磁盘和可用进程状态；无法证明时进入 WAITING_INPUT |
| 同步线程可能仍执行 / 子进程是否退出不明 | 保持相关工作区不可继续写，探测或隔离；不能立刻以 CANCELLED 宣称全部副作用停止 |
| 待审批状态 | 校验审批是否过期、工具参数与工作区是否变化；继续等待或重建审批 |
| 目录分页游标已失效 | 重新建立查询并去除重复读取；当前内存游标不能随 Run Checkpoint 自动恢复 |

审计重放只重建状态与可见轨迹；重新执行是新的真实动作；Fork 是基于历史材料创建新 Run。三者必须使用不同 API，不能让“查看历史”触发 Shell。

### 11.4 审批、暂停与取消

ApprovalRequest 应绑定调用身份、有效参数摘要、工具版本、工作区前提、授权范围、有效期和 policy_version。用户改参数时保存原值与新值，重新校验 Schema 和 Policy；旧签名不能放行新参数。用户拒绝后产生 DENIED 观察，模型可选择已有权限内的替代方案。

审批挂起时不写一个假失败结果让模型继续尝试相同动作；它是明确的等待状态。审批决定作为一次性可消费事件落库，重复提交不重复执行。等待期间发生无关变更可以继续；与动作前提相关的变化必须重新核对。

PAUSE 默认在可恢复边界停止派发并保存状态；CANCEL 尽快停止新动作并终止可终止的请求/进程树，等待清理和结果核对。取消不会自动回滚所有文件，已经发生的变更与未验证产物要交给用户查看。崩溃后取消状态仍应生效，不能重启就继续执行。

取消不能只等控制器从长工具调用返回后才读取收件箱。RunService 先持久化取消请求，再通知当前 RunContext 的取消信号；执行器观察该信号执行中断，控制器随后写入状态变化与清理结果。通知丢失时由持久收件箱补偿，不能把内存 Event 当作唯一事实。

### 11.5 同一工作区与多个 Run

本地目标部署中至少需要 Run 单写者所有权和规范化物理根目录资源锁。若只支持一个应用实例，应使用应用实例锁阻止第二个进程同时写同库同工作区，并明确产品边界。

若开放多进程 Worker，必须增加租约与 fencing，且写入方真正校验所有权；只有一个数据库 lease 字段不能阻止旧进程继续写文件或执行 Shell。此能力属于部署模式扩展，不能在单进程测试后宣称支持分布式恢复。

## 12. 预算、进展与停止策略

### 12.1 一个总账，多个明确的子预算

Run 总账覆盖所有模型与工具活动，包括计划、压缩、检查、Judge 和修复；可为每类活动设置子预算，但总上限始终有效。

| 预算维度 | 语义 |
|---|---|
| model_requests | 逻辑请求与实际网络尝试分别记录，SDK 内部重试需要暴露统计或统一关闭后由上层管理 |
| tool_calls | 每个新逻辑调用准入计数；参数错误计数；恢复读取旧结果不重复计数 |
| tokens / cost | 保留 Provider 实测与估算来源；缺失 Usage 为未知；费用依赖固定价格配置，无法精确计价时显示 Token |
| active_time | 实际执行与请求等待消耗的 Run 时间，进程重启后累计 |
| elapsed_deadline | 从任务启动计算的真实截止时间，可以包含用户等待 |
| verification_attempts | 检查重试与代码修复分别计数，不能通过重新包装 Attempt 清零 |
| storage / output | 日志、Artifact 和事件有明确采集与保留限额，超限可观测 |

调用前预留输出与执行额度，完成后结算；并发调用通过同一事务或锁准入，避免同时看到剩余额度而一起超支。预留不是对 Provider 实际计费的精确承诺，超限时应记录实际值和原因。

执行、检查和终态整理需要合理分配预算。生产 Profile 应预留验证和交付空间，避免模型把全部 Token 用在修改上，最后没有能力检查或解释结果；清理和记录已发生的副作用不能因 Token 耗尽而被跳过。

资源限制使用明确合并规则：部署硬约束、Run 请求和 Profile 取允许范围内的有效值，工具策略再施加局部约束。展示 resolved_policy 和来源。新增可选上限可定义 omitted=继承、显式 unlimited=无该层上限、0=禁止或非法，但必须逐字段声明；TOML 没有 null，不能设计无法表达的配置。

### 12.2 进展证据优于单一重复阈值

ProgressMonitor 以第 15.3 节的 addresses / ProgressDelta 为语义主线，并收集一段窗口内的可解释信号：

- 新定位的相关文件、错误复现、排除的假设与有效用户信息。
- 必需验收项从失败/未知变为通过。
- 工作区变更与验收目标的关联，而不是只统计新增 Commit。
- 规范化工具调用、观察摘要和环境版本是否反复相同。
- 相同错误是否在同一前提下重复，修复是否在两个版本之间来回振荡。

模型说“取得了进展”只是提议，需关联可观察事件；同样，长时间无输出的构建不一定卡住，进程状态和工具超时应单独处理。

建议干预梯度：

~~~text
疑似停滞
  → 提示具体重复事实和当前未解决项
  → 补采证据 / 换工具 / 刷新上下文
  → 针对性重新规划，记录不同于旧方案的行动
  → 仍停滞则请求必要信息或 EXHAUSTED
~~~

每次干预必须有唯一触发记录和预算，不能让“重新规划”变成另一种无限循环。分页读取、合法轮询和失败—修复—测试序列应成为误报反例。

不建议一开始依赖 LLM 给出“进展分数”。先实现可解释规则，再在离线样本上标注误报和漏报；只在有足够证据后学习或调参。

### 12.3 控制决策与冲突优先级

StopPolicy 更适合扩展为 ControlPolicy：

~~~text
action: PLAN_TODOS | PLAN_STAGE | ACT | VERIFY | ROUTE_FEEDBACK | COMPACT | WAIT | TERMINATE
reason_code, user_summary, evidence_refs
resume_condition, policy_version, budget_snapshot
~~~

该 action 是运行时下一阶段，不等于第 15.1 节的语义路由。FeedbackDecision.route=REPAIR 可转换为保留计划后 ACT；INVESTIGATE / REPLAN_STAGE 转成 PLAN_STAGE；REPLAN_TODO 转成 PLAN_TODOS。终态成功必须来自 Task 级验收聚合。

同一时刻出现多种信号时，按确定顺序处理：取消/授权撤销阻止新副作用；未知执行结果先核对；新的用户约束和工作区前置条件优先于旧计划；预算约束限制进一步执行；其后才是常规验证与进展策略。已确定的结果与清理事件仍需落库。

## 13. Hook 和扩展边界

保留已有 ToolExecutionHook，同时引入职责有限的 Loop Hook：

~~~text
before_run / after_run
before_context_compile / after_context_compile
before_model / after_model
before_todo_plan / after_todo_plan
before_stage_plan / after_stage_plan
before_feedback_route / after_feedback_route
before_tool_batch / after_tool_batch
before_verification / after_verification
on_checkpoint / on_resume / on_error
~~~

Hook 不直接写 RunState；返回只读观察、限定字段的变换或控制建议，由控制器校验。Hook 配置与顺序固定到 Run 的版本快照，变更必须可追踪。

工具派发的顺序应明确为：加载定义 → 幂等准入与参数校验 → 受控参数变换 → Policy / Approval → 获取资源许可 → 再核对取消与前置条件 → 派发 → 结果持久化 → 后置观察。任何影响参数的变换必须在审批前完成；若审批后再改参数，就必须失效原审批。

核心安全检查、执行账本和完成判定不能只做成可随意禁用的日志 Hook。前置策略 Hook 异常阻止该动作并记录；后置观察 Hook 异常仅降级观测，不改写已经发生的业务事实。Hook 禁止递归发起不计量的模型/工具调用。

扩展的典型边界：

| 扩展 | 可做什么 | 必须遵守什么 |
|---|---|---|
| Model Adapter | 映射请求与响应、Provider 状态 | 消息配对、响应完整性、可观测重试 |
| Tool Provider | 注册工具和 Effect 声明 | 参数验证、资源范围、执行与恢复契约 |
| Verifier | 读取证据或申请受控检查 | 契约版本、来源、检查预算、不能自行授予权限 |
| Context Policy | 调整检索与压缩 | 保留不可丢失字段和协议完整性 |
| Strategy | 提议计划、修复或候选分支 | 不绕过共享预算、工作区隔离和验收 |
| Trigger Adapter | 提交任务或恢复事件 | event_id 去重、授权上下文、忙碌时排队 |
| Observer | 日志、指标、UI、Trace 导出 | 脱敏、租户范围、不改变控制事实 |

## 14. 持久模型、事件与可观测性

### 14.1 建议的数据模型

继续使用应用级 SQLAlchemy 与 Alembic，不单独建立 Loop 数据库。建议的逻辑表组：

| 数据组 | 主要内容 |
|---|---|
| sessions / tasks / task_contract_versions | 会话、持续目标、不可变契约版本 |
| runs / run_checkpoints | RunState 投影、状态版本、所有权、恢复点 |
| input_events / run_events | 外部输入去重、顺序事件与关联关系 |
| messages / model_calls / model_request_attempts | 原始完整消息、请求快照、实际尝试与 Usage |
| tool_calls / tool_executions / approvals | 逻辑调用、执行尝试、审批及 operation 关联 |
| todo_plans / todos / stages / stage_attempts | Todo 依赖、阶段目标与版本、执行尝试及替代关系 |
| questions / progress_deltas / feedback_decisions | 未决问题、核实进展、反馈路由与理由 |
| candidates / verification_runs / evidence | 带 STAGE / TODO / TASK 范围的候选、检查结果、证据与失效 |
| budget_entries / artifacts / context_manifests | 配额流水、产物元数据、模型输入来源 |

表名是建议，最终可合并部分低基数字段；独立身份、唯一约束和状态转换不能省略。所有查询按 tenant / task / run 权限过滤；工作区逻辑标识不是鉴权系统。

RunEvent 至少有 event_id、run_id、seq、schema_version、type、timestamp、causation_id、correlation_id、payload_ref。运行状态更新和事件写入同一个短事务；以 run_id + seq 保证顺序，以 expected_version 防止两个推进者覆盖状态。

UI 通知在提交后发送；客户端按最后 seq 补读。通知允许重复，消费端去重。可以直接从本地事件表拉取；若跨进程推送则使用 Outbox 等机制，不能让“通知已发出但状态未落库”成为恢复事实。

### 14.2 事实、投影与存储边界

事件用于审计和状态重建，RunState 是快速查询的投影，Checkpoint 缩短重放时间。Provider 原始响应保留必要字段和版本；不序列化 Python 协程、数据库 Session、锁、沙箱实例或完整运行对象。

文件内容继续由 Dulwich / Artifact 存储负责，数据库保存引用与摘要。Artifact 先写临时文件、校验后原子发布，再提交数据库引用；失败产生的孤儿文件由 GC 清理。反向顺序也需要明确补偿，不能留一个看似存在但实际缺失的证据引用。

定义保留期、活跃 Run 的引用保护、磁盘不足处理、日志脱敏和凭据排除。记录 Prompt/模型配置时不复制 API Key。删除历史应处理证据、Artifact 与模型消息之间的引用，不让恢复点指向已经回收的数据。

### 14.3 用户应该看见什么

用户视图应包含当前目标、正在做的动作、真实进度、文件变更、已执行检查、剩余阻塞、资源消耗与可用操作。长步骤显示实际工具状态或等待原因；没有新事实时不靠重复生成“正在努力”刷屏。

最终 RunResult 建议包含：

~~~text
status, assurance, stop_reason
task_id, run_id, contract_version
summary, deliverables, workspace_changes
todo_results, criterion_results, evidence_refs, unresolved_items
usage: actual / estimated / unknown
recovery_or_followup_options
~~~

隐藏推理不是可观测性的要求。应记录决策所依据的可见输入、动作和证据，不把模型内部推理当作恢复协议或用户必须阅读的日志。

## 15. 三项推理改进：机制、已有基础与实验

本次创新候选集中在推理控制，而非持久化、日志或数据库设计。三项机制共同回答：**本轮为何做这件事、做到哪里交回、反馈后调整哪一层。**先实现明确规则和结构化输出，再在真实任务上改进；不要求训练专门的调度模型，也不把额外角色数量当作创新。

### 15.1 改进一：按失败所在层级反馈与调整

**已有基础：** AdaPlanner 已有反馈驱动的计划修订，ADaPT 已有执行失败后的进一步分解。QHarness 的提案是把失败差异转换成可审查的分层路由，尽量保留仍成立的目标、方法和发现。

#### Verifier 给事实，Router 决定调整位置

Verifier 先报告预期、实际、未满足条件和来源。diagnosis_hints 是候选解释；未得到验证的原因不能直接当作重新规划依据。Feedback Router 先用明确规则处理已知情况；遇到语义归因可以调用模型提出分类，但必须关联观察，并允许 UNKNOWN 归因。

| 路由 | 适用条件 | 保留与调整 |
|---|---|---|
| REPAIR | 当前目标和关键假设仍有效，已定位具体执行/实现错误 | 保留 StagePlan，生成带具体反馈的新 Attempt，内层继续修复 |
| INVESTIGATE | 缺少关键事实，无法区分几个失败原因 | 保留 Todo，生成用于区分原因的调查阶段 |
| REPLAN_STAGE | 当前阶段目标、方法或关键假设已被证据否定 | 保留 Todo，替换当前阶段方案；保留未失效发现 |
| REPLAN_TODO | Todo 的前提、依赖或工作分解不再成立 | Global Planner 修订受影响 Todo 与依赖，保留有效完成项 |
| ADVANCE | 当前结果足以进入下一阶段或下一 Todo | 更新缺口与完成状态；按具体完成层级推进，不自动结束 Task |

另外有 RETRY_CHECK / WAIT / TERMINATE 等运行控制决定：检查器 ERROR 先处理检查；必要输入缺失就等待；取消、预算退出和未知副作用优先遵守运行控制。它们不应被强行分类成“代码修复”。

路由并非一条必须逐级尝试的升级阶梯。若新证据已经明确推翻 Todo 的前提，可以直接 REPLAN_TODO；若只是一个变量名错误，则直接 REPAIR。只有在原因不明确时才 INVESTIGATE，不用固定失败次数替代归因。

~~~python
FeedbackDecision(
    route="REPAIR",
    reason="整页分支遗漏了相同边界处理，原定位和目标仍有效",
    evidence_refs=["E7", "E8"],
    preserve=["todo:T1", "stage:S2:v1", "finding:F1"],
    invalidate=[],
    focus=["补齐整页分支并验证对应输入"],
)
~~~

preserve / invalidate 表达证据影响范围，控制器校验目标 ID 与状态。规划模型不能通过 invalidate 删除用户要求或抹掉失败历史；不能通过重新规划重置预算。

#### 建议规则与反例

- 参数或已定位代码细节错误：局部修复。
- 检查没有真正启动、测试收集为空：先核对检查配置，不能据此宣称业务逻辑失败。
- 原始数据完整而结果组装后丢失：调查范围转向组装层，不再反复改查询。
- 同一方案多次失败且没有新解释：要求新的区分性证据，不自动无限重试。
- 发现缺少前置接口或任务顺序不成立：调整相应 Todo 依赖。
- 工具权限拒绝：寻找已有授权内的实现，或等待必要授权；重新规划不是绕过授权。

**待验证假设：** 相比统一“重新规划”或统一“再修一次”，分层反馈能减少错误回退和重复劳动。

**实验：** 固定失败样本，包括实现疏漏、假设错误、环境错误和依赖错误；比较统一重试、统一阶段重规划与分层路由。测路由正确性、有效修复率、全局重规划次数、无效重复动作、总成本和误完成。归因样本应由独立检查或人工标注，不能用 Router 自己的分类给自己评分。

### 15.2 改进二：围绕关键不确定性动态选择阶段粒度

**已有基础：** ADaPT 支持按能力和复杂度调整分解，RestGPT 支持在线子任务规划；Anthropic 的工程案例说明固定阶段可能随模型变化产生额外开销。QHarness 的提案是根据当前证据确定阶段跨度和交回条件。

一个阶段应产出一组能够检查、并决定后续行动的结果。不能只按“五次工具调用”定义阶段，也不能把“完成整个复杂功能”当作对所有情况都合适的默认粒度。

| 当前情况 | 阶段选择 | 交回依据 |
|---|---|---|
| 原因未知，存在会影响修复方案的关键问题 | INVESTIGATE：围绕该问题调查 | 得到足以区分候选原因的证据，或确认现有前提不成立 |
| 原因明确、修改关联紧密、影响范围清楚 | IMPLEMENT：把修改与针对性检查放在同一阶段 | 产物达到阶段预期，或新发现使方案不成立 |
| 主要缺口是已有产物尚未检查 | VALIDATE：补充必要运行或集成观察 | 所需检查得到可解释结果，再交外层 Verifier 聚合 |
| 多个修改依赖同一尚未确认的事实 | 先解决该前提，再规划依赖它的实现 | 避免跨越未知前提积累推测性修改 |

**默认规则：不要让阶段计划依赖尚未确认的关键前提。**这不是禁止探索性实现或原型；探索动作应被标为用于获取信息的实验，不能当作已经确认方案下的正式交付。

StagePlan 明确 stop_when 和 replan_when。条件达到时内层交回；工具配对、清理等已开始的必要动作先完成。前提被否定可以提前返回 NEEDS_REPLAN，不必把全部原动作做完再验证。简单任务可以一阶段完成，复杂任务可以逐步细化，阶段数量不固定。

目标与验收边界一旦在本次 Attempt 开始前确定，Action Agent 不能随意扩张。执行中发现可顺手完成的新工作，应交回后由 Executor 在下一轮更新 StagePlan；不为省一次交接而让阶段约定失去意义。局部工具和实现步骤则由内层自主调整，不要求逐条审批。

#### 如何作阶段规划决策

输入只要求当前 Todo、有效发现、未决问题、上轮反馈和必要环境片段。模型提议目标、addresses、预期结果与条件；控制器校验引用、范围、权限和预算。不能用模型自报的“置信度 90%”作为唯一分段依据。

每轮都保留阶段规划决策：REPAIR 可以沿用计划；ADVANCE 选择下一个缺口；INVESTIGATE / REPLAN_STAGE 生成新阶段。对没有新信息且目标仍有效的继续动作，不强制额外 LLM 规划调用。

**待验证假设：** 适配任务不确定性的阶段大小，比固定细分或固定大阶段更能兼顾任务成功和规划开销。

**实验：** 对比固定小阶段、固定大阶段和动态阶段。任务同时包含原因明确的局部修改与需要调查的跨模块问题。记录验收率、额外规划调用、过早交回、越过关键前提导致的返工，以及总延迟。阶段数少本身不算胜利，必须连同结果质量比较。

### 15.3 改进三：把阶段结果关联到 Todo 的实际缺口

**已有基础：** 子目标、验收与进度判断在分层规划和工程任务管理中已有广泛使用。本项目提案是让每个 Stage 显式解释其对当前 Todo 的作用，并让后续规划消费已经核实的缺口变化。

维护两类明确状态：

~~~text
完成条件 Criteria：
  C1 分页数据完整
  C2 原有行为不回归
  C3 响应格式兼容

未决问题 Questions：
  Q1 遗漏发生在哪个处理环节？
  Q2 空页和整页边界是否受影响？
~~~

StagePlan.addresses 至少关联一个 Criterion 或与 Todo 有依据关联的 Question。未决问题记录 why_it_matters、候选解释和证据引用；可以新建、解决或重新打开，但不能靠制造无关问题刷进度。

Verifier 分别检查：

1. **阶段预期是否达成。** 实际输出是否满足本轮约定？
2. **任务缺口是否变化。** 对应问题得到解决、候选原因被有据排除，或验收项得到有效证据？
3. **是否出现回归。** 新修改是否破坏已满足条件，旧结论是否被新证据否定？

阶段 PASS 只证明局部约定成立。Todo PASS 需要其所有完成条件满足；Task PASS 还要原始要求完整覆盖和必要集成检查。不能通过把阶段目标缩成“生成一段解释”获得 Todo 完成。

#### 有效调查与无效重复的区别

确认“查询结果完整”虽然没有修改代码，但排除了一个原因、改变了下一步方向，属于有效进展。若新信息仍不足以解决 Q1，可以记录 eliminated_hypotheses 或 narrowed_scope，不强求每次调查都关闭整个问题。

只统计读文件数、Token、Commit 或自报进度分数不够。新增事实也不是越多越好：需要说明它对当前缺口有什么影响。不确定的进展先保留为 proposed，不直接授予完成状态。

若一轮没有得到影响后续决策的新事实，也没有推动完成条件，应让下一轮说明调查方法将如何改变，例如选择不同输入、观察另一个处理边界或排除另一个候选原因。合法分页、必要轮询和长工具运行不能因此被错误终止。

~~~text
ProgressDelta:
  resolved_questions
  narrowed_questions / eliminated_hypotheses
  satisfied_criteria
  regressed_criteria
  new_relevant_questions
  supporting_evidence
~~~

ProgressDelta 是可解释的结果集合，不要求把它们强行加权成统一分数。优先用规则和独立证据验证；语义相关性难以确定时可用模型建议并抽样人工检查。

**待验证假设：** 显式关联缺口并区分三层完成度，可以减少“不断做容易的阶段但任务未完成”和过早结束。

**实验：** 对比无进展结构、仅 Todo 勾选、addresses + ProgressDelta。构造重复调查、解决原因但未修复、局部修复破坏旧行为和合理排除假设等任务。测错误完成率、与目标无关的阶段、无效调查轮次、误报停滞和任务验收率。

### 15.4 三项机制怎样组合，如何避免虚假创新

建议一次性确定三个接口：StagePlan、StageVerdict / ProgressDelta、FeedbackDecision。规划产生可检查的约定，行动提供实际观察，验证形成差异，路由决定保留或调整的层级。

完整组合的实验假设是：**根据验证反馈定位问题所在层级，动态选择阶段目标和跨度，并以缺口变化约束推进，可以在相同总预算下减少无效尝试，提高任务验收率。**

总预算必须包含 Planner、Stage Planner、Action Agent、Judge、压缩与检查成本。实验分别开启分层路由、动态粒度、缺口关联，再测试组合；不能只拿多花模型调用的完整系统和少预算基线比较。

这些构件都有相关工作，本设计不声明概念首创。可形成贡献的是具体的判定规则、结果表示、交回与反馈协议，以及在代码任务上的可复现收益。若某项增加成本却无稳定收益，可以调整触发条件；这不改变生产闭环必须完整的要求。

### 15.5 与生产基础、可选扩展的关系

证据与代码版本绑定、增量检查、副作用核对和持久恢复继续按第 9—14 节实现。它们是推理能稳定落地的基础，不替代本节三项推理机制。评测中保持这些基础一致，再比较推理策略；恢复与证据缓存另做专项故障和正确性实验。

默认使用单轨迹。Planner / Stage Planner / Actor / Judge 是逻辑角色，可复用模型；不强制多 Agent。分支搜索如需引入，必须有独立工作区/沙箱、基线版本、共享总预算、分支证据与回收规则；选择补丁后合并并重新验证。当前文件回滚不能冒充完整环境分支，外部副作用不得默认进入试探分支。

## 16. 建议模块、推理接口与控制流程

### 16.1 代码布局与复用边界

~~~text
qharness/
  loop/
    controller.py       唯一推进器，调度 Todo / Stage / Actor / Verify
    models.py           TodoPlan、StagePlan、StageOutcome、FeedbackDecision
    planner.py          初步 Todo 生成与按反馈修订
    executor.py         当前 Todo 的阶段循环、完成与交回
    stage_planner.py    根据缺口规划阶段，或保留有效计划
    actor.py            内层模型—工具循环与阶段交回
    feedback.py         分层归因与路由规则
    progress.py         Questions、addresses 与 ProgressDelta 校验
    transitions.py      状态转换、scope 与版本校验
    scheduler.py        工具批次、依赖、资源屏障
    policies.py         总预算、停止与干预策略
    recovery.py         模型/工具/工作区恢复核对
    prompts/            Todo / Stage / Actor / Judge 角色模板
  context/
    compiler.py         按角色编译上下文、Token 预算、Provider 投影
    memory.py           有来源的发现、摘要与工作记忆
  verification/
    contracts.py        Stage / Todo / Task 完成条件与检查定义
    runner.py           受控采集证据
    controller.py       分 scope 聚合，产生 StageVerdict / TaskVerdict
    evidence.py         来源、版本绑定、失效与复用
  run/                  复用 RunContext，新增 RunService
  persistence/          复用数据库基础设施，新增业务仓储与迁移
  tools/                复用 ToolExecutor，补 Effect、稳定身份与持久结果
  workspace/            复用版本事实，补执行关联与恢复核对
~~~

这是职责布局，不要求每个文件都引入一组抽象基类。Global Planner、Stage Planner 和 Actor 可通过现有 ModelBackend 调用同一个模型；它们不各自创建工具运行时、预算或工作区。

### 16.2 对外运行接口

~~~python
class RunService:
    async def start(self, request: AgentRunRequest) -> RunHandle: ...
    async def send_input(self, run_id: str, event: InputEvent) -> RunSnapshot: ...
    async def pause(self, run_id: str) -> RunSnapshot: ...
    async def cancel(self, run_id: str) -> RunSnapshot: ...
    async def resume(self, run_id: str, request: ResumeRequest) -> RunHandle: ...
    async def get(self, run_id: str) -> RunSnapshot: ...
    def events(self, run_id: str, after_seq: int) -> AsyncIterator[RunEvent]: ...
~~~

请求 ID 与 event_id 去重；RunHandle 可以返回等待状态，不把用户审批转换成未捕获异常。恢复需要带回当前 Todo、StagePlan、Attempt 和未处理的验证/路由结果，不能只恢复聊天记录。

### 16.3 推理核心接口

以下是设计接口，不是当前已存在的 Python API：

~~~python
class GlobalPlanner:
    async def create(self, task, context) -> TodoPlan: ...
    async def revise(self, task, plan, feedback, context) -> TodoPlanPatch: ...

class StagePlanner:
    async def next(
        self, todo, context, previous_plan, feedback
    ) -> StagePlan:
        # REPAIR 可沿用有效计划；需要新目标时再请求模型。
        ...

class ActionAgent:
    async def run(self, stage_plan, context, feedback) -> StageOutcome: ...

class Verifier:
    async def verify_stage(
        self, task, todo, stage_plan, outcome, context
    ) -> StageVerdict: ...

    async def verify_task(self, task, context) -> TaskVerdict: ...

class FeedbackRouter:
    async def decide(
        self, todo, stage_plan, outcome, verdict, context
    ) -> FeedbackDecision: ...
~~~

TodoPlanPatch 必须表达增加、替代、依赖修改及原因，不能仅返回一张新列表使旧状态失踪。StagePlanner.next 不得修改 Task 完成标准。ActionAgent.run 只产生候选与观察，不直接设置 Todo 完成。Verifier.verify_stage 同时聚合当前阶段和 Todo；verify_task 检查原始任务覆盖与集成问题。

建议把模型调用角色标为 TODO_PLANNER / STAGE_PLANNER / ACTOR / JUDGE，统一通过角色适配服务执行 Schema 校验、上下文编译、Usage 与错误处理。模型自然语言输出中出现“已完成”不触发其他角色的状态变化。

### 16.4 Executor 业务伪代码

下面表达业务逻辑，不是直接复制运行的代码。控制输入、预算、清理和已发生副作用的处理由统一运行时提供；真实实现将每个分支转换为可恢复状态。

~~~python
async def execute_todo(task, todo, context):
    stage_plan = None
    outcome = None
    feedback = None

    while runtime.can_advance(context):
        context = await runtime.apply_inputs(context)

        if runtime.must_yield(context):
            return runtime.yield_result(context)

        if feedback is None or feedback.route != RETRY_CHECK:
            stage_plan = await stage_planner.next(
                todo, context, stage_plan, feedback
            )
            validate_stage_plan(task, todo, stage_plan, context)

            outcome = await actor.run(stage_plan, context, feedback)

        # RETRY_CHECK 保留已有行动结果，只补检查，不重复执行修改。
        verdict = await verifier.verify_stage(
            task, todo, stage_plan, outcome, context
        )
        context = accept_verified_results(context, verdict)

        if verdict.todo_status == PASS:
            return TodoOutcome.completed(todo, verdict)

        feedback = await router.decide(
            todo, stage_plan, outcome, verdict, context
        )

        if feedback.route in {REPLAN_TODO, WAIT, TERMINATE}:
            return TodoOutcome.yield_control(todo, feedback)

        # ADVANCE / REPAIR / INVESTIGATE / REPLAN_STAGE：
        # 回到下一轮阶段规划决策；有效目标和发现可以继续使用。
        # RETRY_CHECK：下一轮仅重试检查。

    return runtime.exhausted_result(context)
~~~

RETRY_CHECK 是当前阶段的验证重试，不是一个新增的推理阶段；伪代码借用同一 while 表达调度，统计时不增加阶段规划轮次。它有独立次数与总预算约束；重新检查时仍要核对原产物与条件是否有效，环境或工作区已变化则不能盲目复用旧 outcome。actor 返回阻塞或前提失效时，Verifier 可核对既有事实后返回 INCONCLUSIVE，而非强制运行全部检查。

用户输入导致当前 Todo / StagePlan 失效时，apply_inputs 必须清除不再有效的待重测分支，并转到重新规划或等待；不能在变更后继续执行旧 plan。未知副作用、取消与审批挂起通过运行时交回，优先于语义路由。

### 16.5 外层 Todo 推进与内层行动的衔接

外层任务推进按以下规则：

1. 创建初步 TodoPlan，或接受调用方提供且通过校验的 Todo。
2. 选择依赖已满足的 Todo，运行其 Executor。
3. Todo 完成则更新状态；REPLAN_TODO 则应用有据的 TodoPlanPatch；必要输入或运行控制则挂起。
4. 没有可执行项时，区分依赖错误、真实阻塞与列表已完成，不能循环空选。
5. 所有 Todo 通过后，调用 Task Verifier。
6. Task 未通过时将具体缺口映射到原 Todo，重开受影响项或生成必要修复项，再推进；没有修复条件就如实交回。
7. Task 通过后才形成最终 RunResult。

内层 ActionAgent.run：

~~~text
编译 Actor Context
  → 模型生成动作或交回结果
  → 完整响应校验
      ├─ Tool Calls → 调度执行 → Observation → 下一次模型决策
      ├─ CANDIDATE → 返回阶段候选
      ├─ NEEDS_REPLAN / BLOCKED / STALLED → 返回事实与原因
      └─ 协议异常 / 运行控制 → 受控纠错或交回
~~~

每次工具批次结束检查阶段交回条件、最新输入与预算。模型可以在内层根据普通工具错误调整动作，但不得无限停留在内层绕过阶段反馈，也不能自行修改 StagePlan 完成标准。ToolExecutor 的 success 仍不能替代业务结果和阶段判定。

### 16.6 用一个状态推进器落实逻辑循环

建议状态主线：

~~~text
PLAN_TODOS → SELECT_TODO → PLAN_STAGE → ACT
                                ↑        │
                                │        ▼
                                └── ROUTE_FEEDBACK ← VERIFY_STAGE
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
                SELECT_TODO          PLAN_TODOS           WAIT / STOP

全部 Todo 通过 → VERIFY_TASK → COMPLETE / 重开相关 Todo
~~~

REPAIR 路径也经过 PLAN_STAGE 决策，但可沿用同一目标和方法；RETRY_CHECK 从反馈回到 VERIFY_STAGE；需要更多工具时在 ACT 内继续。实现无需依赖持久的嵌套调用栈，恢复器根据状态、scope 和对象版本选择下一个微步骤。

相应事件可包括 todo_plan.created/revised、stage.planned、stage.outcome、stage.verified、feedback.routed、todo.completed/reopened、task.verified。事件只有在作用对象版本匹配时才能应用；阶段 PASS 事件不能触发任务终态。

## 17. 后续实施任务：按依赖交付完整推理闭环

具体编码顺序、可勾选任务、各批验收出口与第一批文件清单见[Agent Loop 实现计划](./Agent-Loop实现计划.md)。本节说明设计能力包，实施计划将其细化为八个有依赖关系的编码批次。

本次交付是设计文档；以下模块仍需编码实现。实施顺序围绕已确定的推理流程组织，同时接入现有模型、工具、工作区与数据库基础。各实施包属于同一生产目标，不以空接口或单次跑通作为完整交付。

| 包 | 具体要做的内容 | 依赖 | 可检查交付 |
|---|---|---|---|
| A：推理领域与状态契约 | TodoPlan、Todo、StagePlan、StageOutcome、StageVerdict、ProgressDelta、FeedbackDecision；三层完成语义、版本、依赖校验与状态转换；定义对应持久模型 | 现有 run / persistence / model | 固定样本能走通每种状态转换；阶段 PASS 不会完成 Task；计划不能删掉用户要求 |
| B：内层 Action Agent Loop | 角色上下文、完整响应分类、工具回填、阶段候选与提前交回；准入/尝试分离、稳定 operation_id、安全调度 | A + 现有 Backend / ToolExecutor / Workspace | 按给定 StagePlan 自主读—改—测；前提失效会交回；半截工具响应不执行 |
| C：Todo Planner 与 Executor 主闭环 | 初步 Todo、依赖选择、Stage Planner、Executor 循环；Stage/Todo/Task Verifier、Check Runner、Failure Bundle、证据有效性 | A、B | 动态规划—执行—验证—反馈形成完整闭环；完成 Todo 后整体验收；局部失败可继续修复 |
| D：三项改进的具体策略 | 分层路由、阶段粒度与交回条件、addresses / Questions / ProgressDelta；模型建议和确定性约束结合 | C | 能解释为什么修复/调查/改计划；明确问题可以直接实施，未知关键前提先调查；避免无效调查与误完成 |
| E：长期运行与生产整合 | Context Compiler、Artifact、总预算、审批、Steering、取消、恢复；Checkpoint 保存当前 Todo/Stage/Attempt；UI/Trace 视图与保留策略 | A—D；基础状态形状在 A 确定 | 跨窗口、用户追加要求和重启后继续同一推理过程；不重复副作用、不丢失任务条件 |
| F：回归与对照评测 | 固定任务、假模型、故障注入、真实模型运行、分项消融与 Profile 标定 | A—E | 提交实际验收率、失败轨迹、成本和回归结果，达到第 18 节发布门槛 |

实现 C 时即采用明确的三层结果和基础反馈路由；D 完成路由细化与动态策略，不能用“以后再做创新”把 Executor 留成无限重试。稳定执行 ID、结构化工具结果和计数恢复是 B 的前提，不等 E 才返工。

### 17.1 编码前先固定的推理样例

准备少量无需真实模型即可检查的样例，覆盖：

- 只需一个 Todo、一个阶段就能完成的明确任务。
- 调查阶段 PASS，但 Todo 仍未完成。
- 实现细节错误，经 REPAIR 保留计划继续。
- 原因未知，需要 INVESTIGATE 而不是盲改。
- 前提失效，REPLAN_STAGE；依赖变化，REPLAN_TODO。
- 检查 ERROR，仅重跑检查，不重复代码修改。
- 全部 Todo 已通过，但 Task 集成验收失败。

每个样例明确输入上下文、模型提议、工具观察、期望路由和终态。样例用于确定协议，不要求真实模型逐字输出固定文本。

### 17.2 模型角色与 Prompt 的交付内容

- Todo Planner：只生成足够启动的分解、依赖和验收映射；记录假设，不固定全部工具动作。
- Stage Planner：根据缺口选择阶段目标，输出预期、addresses 与交回条件；能保留仍有效的旧方案。
- Actor：根据实时观察自主选工具；知道何时提交候选、何时报告前提变化。
- Judge：根据约定与证据输出差异和判断；区分事实、推测、检查错误和缺证据。
- Router：优先规则；语义分类调用模型时保留 UNKNOWN，并要求证据关联。

每个角色同时交付输出 Schema、正反例、失败修复规则、上下文选择规则和调用计量。不能只写几段 Prompt，把所有状态判断留给自然语言解析。

### 17.3 Profile 与策略默认值

建议提供 interactive、coding、long_running 三种 Profile。它们共享同一推理和正确性契约，调整阶段粒度、检查强度、上下文组织与资源分配；不通过关闭阶段完成判定来“提速”。

默认采用有依据的分层路由、关键前提优先的阶段划分和可解释 ProgressDelta，不依赖模型自评总分。简单 Todo 可以一阶段完成，阶段验证可直接复用有效证据；复杂 Todo 再拆多阶段。不预设每个 Todo 必须经过固定数量的反思或评审模型调用。

模型/Token/阶段次数与延迟阈值通过试跑确定，配置需展示来源与有效值。没有实测时标为 provisional，不宣称最优；所有 Planner、Actor、Judge 和检查调用都计入同一任务成本。

## 18. 验收与评测方案

### 18.1 确定性协议与故障验收

| 类别 | 场景 | 必须观察到的结果 |
|---|---|---|
| 直接输出 | 合法停止、无工具 | 按角色解析：Planner 产生计划、Actor 产生阶段候选、Judge 产生判定；只有 Task 验收通过才能完成 Run |
| 输出完整性 | length、空响应、半截工具 JSON | 不误判完成，不执行半截动作 |
| 工具配对 | 多调用乱序完成 | 按原 ID/索引回填，持久事件保留实际完成顺序 |
| 参数错误 | 无效 JSON、未知工具、Schema 不符 | 返回可修复观察；不绕过调用限额 |
| 命令失败 | Handler 正常返回，exit_code 非零 | Loop 看见业务失败，任务不会因外层 success 被放行 |
| 调度 | 并行读取、写后读、Shell 与写工具 | 合法读取可并发；冲突动作被屏障隔离 |
| 批次失败 | 前一动作失败、后一动作依赖它 | 后续明确 SKIPPED，已完成结果不丢失 |
| 审批 | 等待、批准、拒绝、重复提交、参数编辑 | 持久挂起；有效决定只消费一次；旧审批不放行新参数 |
| 用户追加 | 模型等待中收到范围变化 | 新约束生效后不再派发冲突旧动作 |
| 取消 | 模型、进程树、同步线程分别取消 | 阻止新动作；已发生变化可见；未确认停止不会伪称干净退出 |
| 请求重试 | SDK 限流重试与上层重试同时配置 | 实际尝试可计量、上限明确，不出现重试乘法失控 |
| 崩溃 | 工具开始后、结果落库前退出 | 核对后恢复或明确 UNKNOWN；不盲目重放写操作 |
| 数据一致性 | 文件/Dulwich/DB 在不同边界失败 | 差异可检出，存在明确补偿/核对路径 |
| 计数恢复 | 审批恢复、进程重启、Context 重建 | 逻辑计数不重复，实际尝试不漏计 |
| 上下文 | 大日志、压缩、多工具消息、游标失效 | 关键约束与消息配对保留；原证据可取回；游标重新查询 |
| 假验证 | 零测试、删断言、跳过关键用例 | 不仅凭退出码判 PASS，保留未满足的验收项 |
| 证据失效 | 测试后修改代码、配置或依赖 | 旧证据 STALE，重新执行相关检查 |
| 检查故障 | Flaky、环境 ERROR、缺少权限 | 区分失败与无法判断，不通过无限重试筛选绿灯 |
| 预算 | 并发预留、压缩、验证、Usage 缺失 | 所有开销入账，未知不等于零，保留清理能力 |
| 无进展 | 错误重复与编辑振荡 | 触发有界干预并能解释依据 |
| 无进展反例 | 分页、轮询、长构建、正常修复 | 不因工具名重复而错误结束 |
| 客户端 | 断连、重复事件、慢消费者 | Run 按既定策略继续，重连按 seq 补读 |
| 所有权 | 重复 resume、同物理目录不同 ID | 不出现两个推进者或冲突写入 |
| 隔离与保留 | 越租户引用、日志凭据、磁盘满、GC | 拒绝越权读取，不丢活跃恢复点；存储失败明确可见 |

使用 Fake Model Backend、可控工具和固定 Workspace Fixture 实现协议检查；在沙箱/文件系统边界做故障注入。真实模型成功率不能代替这些确定性正确性检查。

### 18.2 真实任务集

建立能覆盖日常使用的固定任务集合：局部修复、跨文件功能、已有测试失败、没有现成测试的修改、依赖/运行环境问题、用户中途改变要求、需要应用操作的交付，以及跨上下文的长期任务。

每个任务保存初始工作区、依赖与运行时版本、用户输入、允许的变更、公开验收和独立终态检查。日常 coding Profile 用于主基线；其他 Profile 另报结果，不能混在一起平均。

数据分开发集、回归集和保留测试集。策略根据开发 Trace 调整后，在保留集检查泛化；不能把保留集失败答案写入记忆再宣称性能提升。

### 18.3 指标定义

- **任务验收率**：一次完整 Run 在预算内通过独立验收的比例，内部修复仍算同一次 Run。
- **错误完成率**：声明 VERIFIED 完成的 Run 中，被独立检查判失败的比例；同时报告占全部 Run 的比例与样本数。
- **运行一致性 pass^k**：同一任务 k 次独立试验全部成功的概率估计；区别于至少一次成功的 pass@k。可按每任务 n 次试验中 s 次成功，用组合数 C(s,k)/C(n,k) 估计，再按任务汇总。不能直接把全体平均成功率取 k 次方来替代异质任务的结果。
- **有效修复率**：有修复机会的失败候选中，最终通过的比例，同时报告新增回归和平均修复成本。
- **恢复正确性**：无重复副作用、无错误继续的比例；另报自动恢复率与人工核对率，避免奖励冒险恢复。
- **执行效率**：每个成功任务的总成本、成功/失败分别的耗时分布、P50/P95 延迟和模型/工具尝试数。
- **交互负担**：每任务必要与不必要的人工介入次数、被动等待时间。
- **Context 质量**：压缩后约束保留、文件版本引用正确率、因旧上下文导致的返工。
- **干预质量**：无进展检测的误报/漏报、触发后的验收收益与额外开销。
- **推理策略质量**：分层路由正确性、错误升级到全局 Planner 的比例、阶段提前交回率、越过未知前提造成的返工，以及规划/Judge 占总调用和成本的比例。
- **目标关联质量**：阶段与实际 Todo 缺口的相关性、无效调查轮次、局部通过被错误提升为整体完成的次数。参考标签由独立检查或人工给出，不能使用同一模型自评作唯一真值。

所有对照固定模型版本、参数、任务环境和预算，重复运行并报告区间与样本数。模型升级后重跑，不假设原有 Harness 策略继续最优。

### 18.4 发布门槛与创新实验分开

生产发布首先要求第 18.1、18.5 节的强制用例全部通过，尤其不能出现重复副作用、审批失配或过期证据被当成已验证。有限测试中的零事件不等于数学保证，故障覆盖必须随真实案例扩充。

任务验收率、成本与延迟门槛由目标任务集的基线数据确定；在没有实测前不编造“成功率 95%”之类数字。发布必须附实际测量、已知限制和回归差异。

第 15 节的实验使用逐项消融：在相同生产运行基础上比较基础推理流程，再分别启用分层路由、动态阶段粒度和缺口关联，最后测组合。安全性不允许为了消融而在真实用户工作区关闭；较弱基线只在受控 Fixture 中运行。


### 18.5 推理流程与三项改进的专项验收

| 场景 | 预期行为 |
|---|---|
| 初始项目未知 | Planner 可以生成调查 Todo 或粗 Todo，不编造完整文件级执行计划 |
| Todo 依赖有环或引用不存在 | 计划校验失败并受控修订，不进入空转 |
| 清楚的局部修改 | 单阶段完成，允许复用证据；不会强制调查与多轮反思 |
| 阶段定位成功但尚未修复 | stage_status=PASS，todo_status 不为 PASS，继续实施 |
| 已定位的实现错误 | REPAIR 保留有效 StagePlan 和发现，不重做全局计划 |
| 失败原因无法确定 | INVESTIGATE 产生区分原因的目标，不将猜测当事实 |
| 阶段前提被观察否定 | Agent 提前交回，选择 REPLAN_STAGE |
| Todo 依赖确实失效 | REPLAN_TODO 只修订受影响项，保留有效完成项 |
| 检查环境错误 | RETRY_CHECK 或环境调查，不重复未知副作用或误改业务代码 |
| 模型缩小预期以通过 | 用户/Todo 完成条件不随之降低，不能误完成 |
| 调查排除了一个原因 | 记录有效 ProgressDelta，即使没有改文件也不误判停滞 |
| 多轮相同调查无新发现 | 要求改变获取信息的方法，干预有界并说明原因 |
| Todo 全勾选但遗漏用户要求 | Task Verifier 拦截，重开相关项或新增必要修复项 |
| 新阶段修改破坏旧结果 | 失效相关证据、记录 regressed_criteria 并继续修复 |
| 重规划时用户追加约束 | 新 StagePlan 基于最新契约，旧参数/候选不得继续放行 |
| Planner、Actor、Judge 复用模型 | 角色输出按各自协议解释，Judge 文本或 Todo 计划不被误执行为工具动作 |
| 恢复发生在验证后、路由前 | 复用已保存判定，按版本只应用一次路由，不重做已完成行动 |

固定输出测试验证协议与路由约束；真实模型任务验证目标选择和语义归因质量。不能只用会严格按脚本输出的 Fake Backend 证明动态推理有效。

### 18.6 对照组与消融设置

| 对照组 | 推理配置 | 回答的问题 |
|---|---|---|
| B0 | 普通 ReAct 工具循环，加统一任务终态检查 | 基本行动能力是什么 |
| B1 | 初始固定 Todo / 计划，顺序执行，固定错误处理 | 固定规划相对普通循环的收益与局限 |
| B2 | 本文三层结构，但每轮完整重新规划、固定阶段规则、只检查局部完成 | 额外规划成本能否自然带来收益 |
| Q0 | 三层主流程，明确 Stage/Todo/Task 判定，基础规则 | 完整闭环的可用基线 |
| Q1 / Q2 / Q3 | 在 Q0 上分别增强分层反馈、动态阶段粒度、缺口关联 | 每项改进独立的效果 |
| Q-all | 三项共同启用 | 组合收益与机制之间的干扰 |

较弱基线的局部结果不能直接作为评测成功，所有组都由同一独立终态检查评分。对比只在受控环境进行；公共权限、恢复、工具和有效证据检查保持一致，避免把基础设施差异误认为推理收益。

固定模型与参数、初始环境、任务输入、工具集合和总预算；所有规划与评审调用计入成本。多次独立运行，报告质量、成本和延迟的分布与区间。必要时增加模型能力和任务复杂度分组，检查策略是否只对某类任务有效。

## 19. 设计评审时优先确认的决定

实现者可以据本文开始细化接口和迁移。以下决定应在相应模块合并前形成 ADR，并用样例运行结果说明取舍：

1. 初步 Todo 的粒度、StagePlan / StageOutcome / StageVerdict / FeedbackDecision 契约，以及 Stage/Todo/Task 完成判定。
2. 分层路由规则、UNKNOWN 归因处理、阶段交回条件与 ProgressDelta 的核验方式。
3. Task 与 Run 的持续关系、契约修订权限，以及运行中消息何时生效。
4. ToolExecutor 的准入/执行拆分方案、稳定 operation_id 的注入点和旧工具兼容方式。
5. 结构化结果与 Artifact 的保存上限，以及 Wrapper success 与业务结果的映射。
6. 检查输入范围、环境指纹、证据失效策略和最终交付快照。
7. 单进程实例锁与工作区所有权边界；哪些未知结果允许自动核对。
8. Context 压缩的协议边界、不可丢失字段与 Provider 能力矩阵。
9. coding / long_running 的实际预算默认值和目标回归任务集。

这些是需要落实的工程决定，不是要求用户在每个普通任务前逐项审批。QHarness 的生产价值取决于它是否能持续推进、如实验证、正确恢复，并让用户理解结果。

## 20. 主要参考资料与核对状态

正文各处已就近链接来源。以下为便于维护的索引；学术论文的结论限于各自实验设置，官方工程资料不视为学术因果证据。

### 20.1 论文与基准

1. Yao et al. [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)，ICLR 2023。
2. Yang et al. [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://arxiv.org/abs/2405.15793)，2024。
3. Madaan et al. [Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/abs/2303.17651)，NeurIPS 2023。
4. Shinn et al. [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)，NeurIPS 2023。
5. Gou et al. [CRITIC: Large Language Models Can Self-Correct with Tool-Interactive Critiquing](https://arxiv.org/abs/2305.11738)，ICLR 2024。
6. Huang et al. [Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/abs/2310.01798)，ICLR 2024。
7. Kamoi et al. [When Can LLMs Actually Correct Their Own Mistakes? A Critical Survey of Self-Correction of LLMs](https://aclanthology.org/2024.tacl-1.78/)，TACL 2024。
8. Zhou et al. [Language Agent Tree Search Unifies Reasoning, Acting, and Planning in Language Models](https://proceedings.mlr.press/v235/zhou24r.html)，ICML 2024。
9. Liu et al. [AgentBench: Evaluating LLMs as Agents](https://arxiv.org/abs/2308.03688)，ICLR 2024。
10. Jimenez et al. [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770)，ICLR 2024。
11. Yao et al. [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045)，2024；[ICLR 2025 论文](https://openreview.net/pdf?id=roNSXZpUDN)。
12. Zhang et al. [Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models](https://arxiv.org/abs/2510.04618)，初稿 2025，当前页面标注 ICLR 2026、v3。
13. Barbaste et al. [Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents](https://arxiv.org/abs/2609.00006)，2026 预印本；仅作背景，元数据问题见第 3.5 节。
14. Li et al. [Agent Harness Engineering: A Survey 作者项目页](https://picrew.github.io/LLM-Harness/)，2026；本次原文访问受限，采用范围见第 3.5 节。


15. Prasad et al. [ADaPT: As-Needed Decomposition and Planning with Language Models](https://aclanthology.org/2024.findings-naacl.264/)，Findings of NAACL 2024；[方法全文](https://arxiv.org/html/2311.05772v2)。
16. Sun et al. [AdaPlanner: Adaptive Planning from Feedback with Language Models](https://proceedings.neurips.cc/paper_files/paper/2023/hash/b5c8c1c117618267944b2617add0a766-Abstract.html)，NeurIPS 2023；[论文全文](https://proceedings.neurips.cc/paper_files/paper/2023/file/b5c8c1c117618267944b2617add0a766-Paper-Conference.pdf)。
17. Song et al. [RestGPT: Connecting Large Language Models with Real-World RESTful APIs](https://arxiv.org/html/2306.06624)，初稿 2023，重点参照第 3.2 节在线规划。
18. Wang et al. [Voyager: An Open-Ended Embodied Agent with Large Language Models](https://arxiv.org/abs/2305.16291)，2023；[作者项目页](https://voyager.minedojo.org/)。

### 20.2 官方工程资料

19. LangChain. [The Art of Loop Engineering](https://www.langchain.com/blog/the-art-of-loop-engineering)。
20. LangGraph. [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
21. LangGraph. [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。
22. OpenAI Agents SDK. [Running agents](https://openai.github.io/openai-agents-python/running_agents/)。
23. OpenHands. [Conversation Architecture](https://docs.openhands.dev/sdk/arch/conversation)。
24. OpenHands. [Conversation Persistence](https://docs.openhands.dev/sdk/guides/convo-persistence)。
25. SWE-agent Team. [mini-SWE-agent Documentation](https://mini-swe-agent.com/latest/)。
26. Microsoft AutoGen. [Termination](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/termination.html)。
27. Anthropic. [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)，2025。
28. Anthropic. [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)，2026。
29. Anthropic. [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)，2025。
30. Anthropic. [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)，2026。
31. MineDojo. [Voyager 官方执行与 Critic 反馈循环源码](https://github.com/MineDojo/Voyager/blob/main/voyager/voyager.py)，重点参照 step / rollout；动态 main 分支，正式复现时固定 Commit。
