# QHarness Agent Loop 实现计划

> 状态：待实现；本计划不表示相关代码已完成  
> 制定日期：2026-09-13  
> 代码基线：HEAD `8ff37fe` 及当日工作区  
> 设计依据：[Agent Loop 文献综述与 QHarness 设计建议](./Agent-Loop文献综述与QHarness设计建议.md)  
> 目标：实现初步 Todo → 动态阶段规划 → Agent 工具循环 → 验证与反馈 → Todo / Task 完成的完整生产闭环

## 1. 实施原则与交付边界

采用八个有明确验收出口的实施批次。每批交付实际行为、对应检查和可运行示例，完成全部批次后才进行生产验收。开发顺序不改变完整目标，也不把验证、恢复、上下文和三项推理改进留成空接口。

先在固定任务和 Fake Backend 上验证状态机，再用真实模型检查推理效果。真实模型偶然跑通不能替代协议与故障检查，Fake Backend 的脚本通过也不能证明真实推理有效。

Planner、Stage Planner、Actor 和 Judge 默认复用一个 ModelBackend，通过角色上下文、Prompt 和输出 Schema 区分。任务规划不要求一次性列出完整执行步骤；阶段完成、Todo 完成和 Task 完成始终分别判定。

持久状态、逻辑调用 ID、预算和结果结构在前期一起确定，后续再完成恢复决策、交互和长期任务联调。不会先用不可恢复的大循环串起来，再事后补执行身份。

本计划中的新增文件路径均为拟定位置；现有文件以当前仓库为准。实施时允许合并过薄的模块，但职责、接口与验收条件保持明确。

## 2. 当前基础与复用位置

| 已有部分 | 直接复用 | 本次需要扩展 |
|---|---|---|
| [模型接口](../src/qharness/backends/base.py) 与 [协议实现](../src/qharness/backends/openai_compatible.py) | complete / stream、统一响应、Provider 字段 | 角色调用、输出校验、请求尝试记录、重试预算归属 |
| [模型领域对象](../src/qharness/model/models.py) | ChatMessage、ToolCall、ChatResponse、Usage | 持久身份和角色关联，不再平行定义一套消息协议 |
| [工具执行器](../src/qharness/tools/executor.py) | 参数校验、限制、取消、Hook | 准入与执行分离、稳定执行身份、结构化结果、审批挂起衔接 |
| [RunContext](../src/qharness/run/context.py) 与 [装配入口](../src/qharness/run/factory.py) | 工作区、沙箱、计数和取消依赖 | 从持久状态重建依赖，装配 RunService |
| [工作区修改服务](../src/qharness/workspace/mutation.py) | 操作历史、私有 Commit、冲突保护 | 从可信执行上下文接收稳定 operation_id，支持恢复核对 |
| [应用数据库](../src/qharness/persistence/) | SQLAlchemy、SessionFactory、Alembic | 随功能增加 Run、模型/工具执行、Todo/Stage、验证等迁移 |

当前没有统一 tests 目录或已配置的测试运行器。建议新增基于标准库 unittest 的行为测试，异步部分使用 IsolatedAsyncioTestCase，避免为了启动协议检查额外引入运行依赖。真实模型和系统沙箱检查单独标识，普通测试默认不访问模型服务，也不执行系统初始化。

## 3. 八个实施批次

### 批次 1：推理对象、状态转换与固定样例

**目的：** 先让核心对象和控制语义在代码中成立，为后续模型调用提供明确契约。

- [ ] 定义 TaskContract、TodoPlan、Todo、StagePlan、StageOutcome、StageVerdict、ProgressDelta、FeedbackDecision。
- [ ] 定义 RunState、Stage/Todo/Task 三层 scope、阶段/执行尝试身份与版本。
- [ ] 校验 Todo 依赖、验收项引用、StagePlan.addresses、目标修订和状态转换。
- [ ] 实现纯状态 reducer：阶段通过、Todo 通过、任务通过、重开、等待等事件各自推进对应状态。
- [ ] 建立固定分页任务 Fixture、ScriptedModelBackend 和可控工具的测试支撑。
- [ ] 明确模型输出边界使用 Pydantic 校验；运行依赖继续由 RunContext 提供，不序列化锁或协程。

主要位置：`loop/models.py`、`loop/transitions.py`、`tests/support/`、`tests/loop/test_transitions.py`。

验收出口：调查阶段 PASS 后仍停在当前 Todo；只有 Task 验收事件可以完成 Run；非法依赖、悬空引用、过期版本和目标降级能够被拒绝。测试断言行为和不变量，不仅测试字段能否序列化。

### 批次 2：共享调用边界、基础上下文与执行账本

**目的：** 让所有推理角色通过同一受控调用路径运行，工具身份与计数从开始就可恢复。

- [ ] 实现角色调用服务，复用 ModelBackend，区分 TODO_PLANNER / STAGE_PLANNER / ACTOR / JUDGE。
- [ ] 实现基础 Context Compiler：按角色注入任务、Todo、StagePlan、观察和反馈；校验消息配对与输入容量。
- [ ] 定义 loop 配置、resolved_policy 和 Prompt 版本；保留当前工具配置的继承与 None 语义。
- [ ] 建立 Run、消息、模型请求尝试、工具逻辑调用/执行和预算的仓储及迁移。
- [ ] 拆分工具准入与实际执行：同一逻辑调用计数一次，网络与执行重试分别计量。
- [ ] 将稳定 operation_id 从可信执行上下文传给文件修改和 run_command，关联 Loop、Workspace 与 Sandbox。
- [ ] 保存结构化工具结果和输出 Artifact；模型可见摘要与业务判定字段分离。
- [ ] 固定模型重试归属：Loop 管理的调用统一配置请求重试，避免与 SDK 内部重试相乘；保留独立 Backend 调用的兼容方式。
- [ ] 新增 Alembic revision，验证现有 workspace 历史数据升级后仍可使用。

主要位置：`loop/model_service.py`、`loop/repository.py`、`loop/config.py`、`loop/budget.py`、`context/compiler.py`，以及现有 tools、workspace、persistence。

验收出口：角色输出可以校验与追踪；重复提交同一逻辑 Tool Call 不重复准入；恢复读旧结果不执行工具；run_command 外层 success 不会覆盖非零退出码；大输出不会丢掉必需业务字段。尚未完成全套恢复核对的 DISPATCHED 操作必须明确返回 UNKNOWN，不能自动重放。

### 批次 3：单阶段 Action Agent Loop

**目的：** 给定 StagePlan 后，模型能自主连续行动，并在合适边界交回结果。

- [ ] 实现模型响应分类、完整流聚合、工具批次回填和下一轮 Context。
- [ ] 支持 CANDIDATE / NEEDS_REPLAN / BLOCKED / STALLED 的结构化阶段交回。
- [ ] 校验 stop_when / replan_when；工具错误可以在内层修复，关键前提失效可以提前交回。
- [ ] 支持 Tool Effect、只读并行段与写屏障；无法证明独立时保留顺序。
- [ ] 接入共享预算、取消、协议纠错和明确错误观察。
- [ ] 提供“给定阶段目标，自动读—改—测”的独立示例。

主要位置：`loop/actor.py`、`loop/scheduler.py`、`loop/prompts/actor.*`、`tests/loop/test_actor.py`。

验收出口：阶段内多轮工具调用正确配对；模型半截工具参数不执行；工具失败后能够修正；前提失效会交回；Actor 不直接设置 Todo 或 Run 完成。此批交付单阶段执行能力，不代表已经形成完整任务执行器。

### 批次 4：Stage / Todo / Task 验证与证据有效性

**目的：** 用明确的检查结果驱动阶段和任务完成，供下一批 Executor 使用。

- [ ] 实现 Check Runner，复用沙箱、工具策略、取消和预算。
- [ ] 实现 PASS / FAIL / INCONCLUSIVE / ERROR 四态结果。
- [ ] 实现 StageVerdict，同时包含阶段结果、Todo 状态、预期/实际差异和 ProgressDelta。
- [ ] 实现 Task Verifier，核对原始要求覆盖、最终产物与必要集成行为。
- [ ] 将证据绑定代码、检查定义和环境输入，相关变化后失效；未知依赖使用保守失效。
- [ ] 处理零测试收集、原有失败、Flaky、检查环境错误和日志不完整。
- [ ] 形成 Failure Bundle；缺证据先补采，检查错误不自动要求改业务代码。
- [ ] 接入可选语义 Judge，保留 UNKNOWN / INCONCLUSIVE，确定性失败不可被模型评分覆盖。

主要位置：`verification/contracts.py`、`verification/runner.py`、`verification/controller.py`、`verification/evidence.py`、`tests/verification/`。

验收出口：找到原因不等于修复完成；阶段通过不等于 Todo 通过；全部 Todo 勾选不能掩盖任务遗漏；测试后相关文件变化使旧结果失效；检查 ERROR 不被误判为代码 FAIL。

### 批次 5：Planner + Executor，贯通整条推理主线

**目的：** 从用户任务开始，经过动态阶段执行，最终由 Task 验收结束。

- [ ] Global Planner 生成初步 TodoPlan，支持有据的 TodoPlanPatch。
- [ ] Todo 推进器选择依赖满足的工作项，识别真实阻塞和依赖错误。
- [ ] Stage Planner 根据当前 Todo、未决问题与反馈生成阶段目标、预期结果、addresses 和交回条件。
- [ ] Executor 连接阶段规划、Actor、Verifier 与基础 Feedback Router。
- [ ] 实现 ADVANCE / REPAIR / INVESTIGATE / REPLAN_STAGE / REPLAN_TODO，以及 RETRY_CHECK / WAIT / TERMINATE。
- [ ] REPAIR 保留有效计划；RETRY_CHECK 只重试检查；其他路由明确调整范围。
- [ ] Todo 完成后推进下一项，全部完成后整体验收；遗漏或回归重开相应 Todo。
- [ ] 将上述逻辑落到统一状态机，提供一次调用启动任务的完整示例。

主要位置：`loop/planner.py`、`loop/stage_planner.py`、`loop/executor.py`、`loop/feedback.py`、`loop/controller.py`、`tests/loop/test_task_flow.py`。

验收出口：固定任务能够经历“调查通过但 Todo 未完成 → 实施失败 → 保留方案修复 → Todo 通过 → Task 验收通过”。另一个样例验证 Todo 全通过但集成失败时会继续工作。至此推理功能主线贯通，生产交付仍需完成后续联调与评测。

### 批次 6：落实三项推理改进

**目的：** 在已贯通流程上完成具体策略，而不是只保留枚举和几个 Prompt。

- [ ] 分层反馈：根据证据判断错误所在层级，输出 preserve / invalidate / focus；未明确原因先调查。
- [ ] 动态阶段粒度：围绕关键不确定性选择 INVESTIGATE / IMPLEMENT / VALIDATE，明确交回边界。
- [ ] 缺口关联：维护 Criteria、Questions、addresses 和 ProgressDelta，校验每个阶段对 Todo 的实际作用。
- [ ] 保留有效调查成果：排除原因和缩小范围也算进展；读文件数或自评分不能直接当进度。
- [ ] 对反复无新信息的调查要求改变获取信息的方法，同时保护合法分页、轮询和长工具运行。
- [ ] 为三项机制增加独立配置与对照开关，测试模式下支持消融；核心权限和执行正确性不随开关关闭。

主要位置：`loop/feedback.py`、`loop/progress.py`、`loop/stage_planner.py` 和对应测试/配置。

验收出口：局部错误不会无故重做全局规划；前提错误不会继续盲修；明确任务可一阶段完成；调查阶段不能通过不断降低目标“刷完成”；有效排除假设不会误判为停滞。

### 批次 7：长期任务、用户交互与恢复联调

**目的：** 让同一推理过程跨上下文窗口、用户追加要求和进程重启继续。

- [ ] 完成 Context Compiler 的检索、结构化工作记忆、压缩与 ContextManifest。
- [ ] 保留 Task / Todo / Stage 条件、未决问题与有效发现，压缩不清零预算或破坏工具配对。
- [ ] 实现 RunService、输入收件箱、Steering、审批、暂停、取消和恢复。
- [ ] 审批绑定参数、版本与前置条件；重复决定不重复执行，参数变化重新校验。
- [ ] 实现稳定执行身份下的恢复核对：已完成、未派发、部分结果、未知副作用分别处理。
- [ ] 处理同步 Handler 可能继续运行、沙箱进程树清理、工作区变更与证据失效。
- [ ] 完成 Run 单写者、同物理根目录互斥、客户端断连和事件补读。
- [ ] 联调 Artifact 保留、磁盘不足、引用保护、日志脱敏和配置快照。
- [ ] 在派发、落盘、Commit、结果确认等边界做故障注入。

主要位置：`run/service.py`、`loop/recovery.py`、`context/`、`loop/repository.py`、现有 sandbox / workspace、`tests/recovery/`。

验收出口：恢复时回到正确 Todo / Stage / Attempt，不重做已完成行动；新用户约束阻止冲突旧动作；旧审批不放行新参数；取消不会伪称所有未知效果已经消失；UI 断连不丢运行结果。

### 批次 8：真实任务评测、Profile 与完整交付

**目的：** 判断这套结构是否在目标任务上好用，并形成可复现发布依据。

- [ ] 建立开发集、回归集和保留测试集，固定任务环境和独立终态检查。
- [ ] 跑通明确修改、调查修复、跨模块功能、环境故障、动态约束和长期任务。
- [ ] 对比普通 ReAct、固定 Todo、每轮完整重规划、三层基础流程和三项改进组合。
- [ ] 固定模型、工具和总预算，规划/Judge/检查开销全部计入；多次运行报告分布。
- [ ] 统计验收率、误完成、路由质量、无效调查、返工、人工介入、成本和耗时。
- [ ] 根据测量确定 interactive / coding / long_running Profile 的实际默认值。
- [ ] 完成使用示例、配置模板、数据库迁移说明和已知边界。
- [ ] 对照设计文档第 18.1、18.5 节逐项验收，输出检查结果与未解决问题。

主要位置：`evals/`、`examples/`、`config/loop.example.toml`、`README.md` 和相关文档。

验收出口：全部必需协议、推理和故障用例通过；有真实任务结果支持选定配置。成功率与延迟门槛基于测量设定，不先编造数值；有限试验中的零故障不作为绝对可靠性保证。

## 4. 依赖顺序与进度检查点

~~~text
1 推理契约与样例
        ↓
2 共享调用、基础 Context、执行账本
        ↓
3 单阶段 Actor
        ↓
4 三层 Verifier
        ↓
5 Planner + Executor 主闭环
        ↓
6 三项推理策略
        ↓
7 长期任务与生产联调
        ↓
8 真实评测与完整交付
~~~

这是一条便于顺序实施和评审的主路径。验证器的纯判定规则可以在 Actor 开发时提前准备；生产恢复规则在前期就应纳入执行身份设计。任务实际执行时不要求并行开发或启动多个 Agent。

三个重要检查点：

- 批次 3 完成：能按一个明确阶段目标自主行动。
- 批次 5 完成：从用户目标到 Task 验收的推理主线已贯通。
- 批次 8 完成：完整生产目标通过联调、故障检查与真实任务评测。

## 5. 下一次编码直接从哪里开始

建议下一次实现批次 1，具体交付以下内容：

| 拟新增位置 | 交付内容 |
|---|---|
| `src/qharness/loop/__init__.py` | 导出明确的领域类型 |
| `src/qharness/loop/models.py` | Task/Todo/Stage、结果、反馈与进度模型 |
| `src/qharness/loop/transitions.py` | 纯状态转换、版本与完成 scope 校验 |
| `tests/support/scripted_backend.py` | 可按脚本返回合法/异常模型响应的 Backend |
| `tests/support/workspaces.py` | 独立临时工作区 Fixture |
| `tests/loop/test_models.py` | 非法依赖、错误引用、契约约束等行为检查 |
| `tests/loop/test_transitions.py` | 三层完成、重新规划、重开与过期事件检查 |

测试包补充必要的 `__init__.py`，保证标准库发现方式可用。拟使用入口：

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
~~~

首批至少覆盖七条轨迹：

1. 阶段 PASS，Todo 尚未完成。
2. Todo PASS，Task 尚有其他要求。
3. Task 全部验收通过，Run 才完成。
4. REPAIR 保留有效阶段目标。
5. REPLAN_STAGE 替换阶段但保持 Todo 要求。
6. REPLAN_TODO 修订依赖并保留有效完成项。
7. 旧版本结果或计划降级被拒绝。

这些检查以固定事实为输入，不依赖模型 API Key，不运行真实用户工作区命令。完成首批后，后续组件就能围绕已经明确的契约和行为实现。

## 6. 进度维护规则

每个批次完成后更新勾选项，并补充实际文件、验证命令、结果和遗留限制。局部完成可以逐项勾选，批次出口未满足时不能标记整批完成。

如实现中发现设计冲突，优先保持用户目标、三层完成语义和执行正确性，更新对应设计说明及受影响用例。接口调整应解释原因，不通过跳过验收或压缩任务范围解决困难。
