# QHarness Agent Loop 实现计划

> 状态：批次 1—6 已实现并通过离线行为检查；批次 7—8 待实现（更新于 2026-09-14）<br>
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

**复用优先（2026-09-13 用户明确要求）：** 每批先检查现有实现；可以复用就直接复用，需要扩展时优先修改原有模块，只有现有职责确实无法承载时才新增代码。文件清单是职责建议，不是必须逐个新建文件的要求。不得平行重写模型消息协议、工具执行器、工作区管理、RunContext 或数据库基础设施。

## 2. 当前基础与复用位置

| 已有部分 | 直接复用 | 本次需要扩展 |
|---|---|---|
| [模型接口](../src/qharness/backends/base.py) 与 [协议实现](../src/qharness/backends/openai_compatible.py) | complete / stream、统一响应、Provider 字段 | 角色调用、输出校验、请求尝试记录、重试预算归属 |
| [模型领域对象](../src/qharness/model/models.py) | ChatMessage、ToolCall、ChatResponse、Usage | 持久身份和角色关联，不再平行定义一套消息协议 |
| [工具执行器](../src/qharness/tools/executor.py) | 参数校验、限制、取消、Hook | 准入与执行分离、稳定执行身份、结构化结果、审批挂起衔接 |
| [RunContext](../src/qharness/run/context.py) 与 [装配入口](../src/qharness/run/factory.py) | 工作区、沙箱、计数和取消依赖 | 从持久状态重建依赖，装配 RunService |
| [工作区修改服务](../src/qharness/workspace/mutation.py) | 操作历史、私有 Commit、冲突保护 | 从可信执行上下文接收稳定 operation_id，支持恢复核对 |
| [应用数据库](../src/qharness/persistence/) | SQLAlchemy、SessionFactory、Alembic | 随功能增加 Run、模型/工具执行、Todo/Stage、验证等迁移 |

批次 1 已新增 tests 目录，使用标准库 unittest，异步部分使用 IsolatedAsyncioTestCase，没有增加测试运行依赖。真实模型和系统沙箱检查单独标识，普通测试默认不访问模型服务，也不执行系统初始化。

## 3. 八个实施批次

### 批次 1：推理对象、状态转换与固定样例

**目的：** 先让核心对象和控制语义在代码中成立，为后续模型调用提供明确契约。

- [x] 定义 TaskContract、TodoPlan、Todo、StagePlan、StageOutcome、StageVerdict、ProgressDelta、FeedbackDecision。
- [x] 定义 RunState、Stage/Todo/Task 三层 scope、阶段/执行尝试身份与版本。
- [x] 校验 Todo 依赖、验收项引用、StagePlan.addresses、目标修订和状态转换。
- [x] 实现纯状态 reducer：阶段通过、Todo 通过、任务通过、重开、等待等事件各自推进对应状态。
- [x] 建立固定分页任务 Fixture、ScriptedModelBackend 和可控工具的测试支撑。
- [x] 明确模型输出边界使用 Pydantic 校验；运行依赖继续由 RunContext 提供，不序列化锁或协程。

主要位置：`loop/models.py`、`loop/transitions.py`、`tests/support/`、`tests/loop/test_transitions.py`。

验收出口：调查阶段 PASS 后仍停在当前 Todo；只有 Task 验收事件可以完成 Run；非法依赖、悬空引用、过期版本和目标降级能够被拒绝。测试断言行为和不变量，不仅测试字段能否序列化。

### 批次 2：共享调用边界、基础上下文与执行账本

**目的：** 让所有推理角色通过同一受控调用路径运行，工具身份与计数从开始就可恢复。

- [x] 实现角色调用服务，复用 ModelBackend，区分 TODO_PLANNER / STAGE_PLANNER / ACTOR / JUDGE。
- [x] 实现基础 Context Compiler：按角色注入任务、Todo、StagePlan、观察和反馈；校验消息配对与输入容量。
- [x] 定义 loop 配置、resolved_policy 和 Prompt 版本；保留当前工具配置的继承与 None 语义。
- [x] 建立 Run、消息、模型请求尝试、工具逻辑调用/执行和预算的仓储及迁移。
- [x] 拆分工具准入与实际执行：同一逻辑调用计数一次，网络与执行重试分别计量。
- [x] 将稳定 operation_id 从可信执行上下文传给文件修改和 run_command，关联 Loop、Workspace 与 Sandbox。
- [x] 保存结构化工具结果和输出 Artifact；模型可见摘要与业务判定字段分离。
- [x] 固定模型重试归属：Loop 管理的调用统一配置请求重试，避免与 SDK 内部重试相乘；保留独立 Backend 调用的兼容方式。
- [x] 新增 Alembic revision，验证现有 workspace 历史数据升级后仍可使用。

主要位置：`loop/model_service.py`、`loop/repository.py`、`loop/config.py`、`loop/budget.py`、`context/compiler.py`，以及现有 tools、workspace、persistence。

验收出口：角色输出可以校验与追踪；重复提交同一逻辑 Tool Call 不重复准入；恢复读旧结果不执行工具；run_command 外层 success 不会覆盖非零退出码；大输出不会丢掉必需业务字段。尚未完成全套恢复核对的 DISPATCHED 操作必须明确返回 UNKNOWN，不能自动重放。

### 批次 3：单阶段 Action Agent Loop

**目的：** 给定 StagePlan 后，模型能自主连续行动，并在合适边界交回结果。

- [x] 实现模型响应分类、完整流聚合、工具批次回填和下一轮 Context。
- [x] 支持 CANDIDATE / NEEDS_REPLAN / BLOCKED / STALLED 的结构化阶段交回。
- [x] 校验 stop_when / replan_when；工具错误可以在内层修复，关键前提失效可以提前交回。
- [x] 支持 Tool Effect、只读并行段与写屏障；无法证明独立时保留顺序。
- [x] 接入共享预算、取消、协议纠错和明确错误观察。
- [x] 提供“给定阶段目标，自动读—改—测”的独立示例。

主要位置：`loop/actor.py`、`loop/scheduler.py`、`loop/prompts/actor.*`、`tests/loop/test_actor.py`。

验收出口：阶段内多轮工具调用正确配对；模型半截工具参数不执行；工具失败后能够修正；前提失效会交回；Actor 不直接设置 Todo 或 Run 完成。此批交付单阶段执行能力，不代表已经形成完整任务执行器。

### 批次 4：Stage / Todo / Task 验证与证据有效性

**目的：** 用明确的检查结果驱动阶段和任务完成，供下一批 Executor 使用。

- [x] 实现 Check Runner，复用沙箱、工具策略、取消和预算。
- [x] 实现 PASS / FAIL / INCONCLUSIVE / ERROR 四态结果。
- [x] 实现 StageVerdict，同时包含阶段结果、Todo 状态、预期/实际差异和 ProgressDelta。
- [x] 实现 Task Verifier，核对原始要求覆盖、最终产物与必要集成行为。
- [x] 将证据绑定代码、检查定义和环境输入，相关变化后失效；未知依赖使用保守失效。
- [x] 处理零测试收集、原有失败、Flaky、检查环境错误和日志不完整。
- [x] 形成 Failure Bundle；缺证据先补采，检查错误不自动要求改业务代码。
- [x] 接入可选语义 Judge，保留 UNKNOWN / INCONCLUSIVE，确定性失败不可被模型评分覆盖。

主要位置：`verification/contracts.py`、`verification/runner.py`、`verification/controller.py`、`verification/evidence.py`、`tests/verification/`。

验收出口：找到原因不等于修复完成；阶段通过不等于 Todo 通过；全部 Todo 勾选不能掩盖任务遗漏；测试后相关文件变化使旧结果失效；检查 ERROR 不被误判为代码 FAIL。

### 批次 5：Planner + Executor，贯通整条推理主线

**目的：** 从用户任务开始，经过动态阶段执行，最终由 Task 验收结束。

- [x] Global Planner 生成初步 TodoPlan，支持有据的 TodoPlanPatch。
- [x] Todo 推进器选择依赖满足的工作项，识别真实阻塞和依赖错误。
- [x] Stage Planner 根据当前 Todo、未决问题与反馈生成阶段目标、预期结果、addresses 和交回条件。
- [x] Executor 连接阶段规划、Actor、Verifier 与基础 Feedback Router。
- [x] 实现 ADVANCE / REPAIR / INVESTIGATE / REPLAN_STAGE / REPLAN_TODO，以及 RETRY_CHECK / WAIT / TERMINATE。
- [x] REPAIR 保留有效计划；RETRY_CHECK 只重试检查；其他路由明确调整范围。
- [x] Todo 完成后推进下一项，全部完成后整体验收；遗漏或回归重开相应 Todo。
- [x] 将上述逻辑落到统一状态机，提供一次调用启动任务的完整示例。

主要位置：`loop/planner.py`、`loop/stage_planner.py`、`loop/executor.py`、`loop/feedback.py`、`loop/controller.py`、`tests/loop/test_task_flow.py`。

验收出口：固定任务能够经历“调查通过但 Todo 未完成 → 实施失败 → 保留方案修复 → Todo 通过 → Task 验收通过”。另一个样例验证 Todo 全通过但集成失败时会继续工作。至此推理功能主线贯通，生产交付仍需完成后续联调与评测。

### 批次 6：落实三项推理改进

**目的：** 在已贯通流程上完成具体策略，而不是只保留枚举和几个 Prompt。

- [x] 分层反馈：根据证据判断错误所在层级，输出 preserve / invalidate / focus；未明确原因先调查。
- [x] 动态阶段粒度：围绕关键不确定性选择 INVESTIGATE / IMPLEMENT / VALIDATE，明确交回边界。
- [x] 缺口关联：维护 Criteria、Questions、addresses 和 ProgressDelta，校验每个阶段对 Todo 的实际作用。
- [x] 保留有效调查成果：排除原因和缩小范围也算进展；读文件数或自评分不能直接当进度。
- [x] 对反复无新信息的调查要求改变获取信息的方法，同时保护合法分页、轮询和长工具运行。
- [x] 为三项机制增加独立配置与对照开关，测试模式下支持消融；核心权限和执行正确性不随开关关闭。

主要位置：`loop/feedback.py`、`loop/progress.py`、原有 `loop/planner.py` 和对应测试/配置；阶段规划继续扩展 Planner，不新增 stage_planner 包装层。

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

## 5. 已完成批次与下一步

### 5.1 批次 1

批次 1 已交付以下内容：

| 实际位置 | 交付内容 |
|---|---|
| `src/qharness/loop/__init__.py` | 导出明确的领域类型 |
| `src/qharness/loop/models.py` | Task/Todo/Stage、结果、反馈与进度模型 |
| `src/qharness/loop/transitions.py` | 纯状态转换、版本与完成 scope 校验 |
| `tests/support/scripted_backend.py` | 可按脚本返回合法/异常模型响应的 Backend |
| `tests/support/workspaces.py` | 独立临时工作区 Fixture |
| `tests/loop/test_models.py` | 非法依赖、错误引用、契约约束等行为检查 |
| `tests/loop/test_transitions.py` | 三层完成、重新规划、重开与过期事件检查 |
| `tests/loop/test_support.py` | 原有模型协议、生产工具执行器与工作区接口的接入检查 |
| `examples/15_loop_state.py` | 离线展示 Stage PASS → Todo PASS → Task PASS 的不同状态 |

测试包已补充必要的 `__init__.py`，保证标准库发现方式可用。运行入口：

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe examples/15_loop_state.py
~~~

首批覆盖以下七条主轨迹，以及检查重试、等待恢复、依赖失效等边界：

1. 阶段 PASS，Todo 尚未完成。
2. Todo PASS，Task 尚有其他要求。
3. Task 全部验收通过，Run 才完成。
4. REPAIR 保留有效阶段目标。
5. REPLAN_STAGE 替换阶段但保持 Todo 要求。
6. REPLAN_TODO 修订依赖并保留有效完成项。
7. 旧版本结果或计划降级被拒绝。

这些检查以固定事实为输入，不依赖模型 API Key，不运行真实用户工作区命令。验收结果：34 项离线测试通过；示例依次输出 `planning`、`verifying_task`、`completed`。

本批复用情况：ScriptedModelBackend 继承现有 ModelBackend 并直接使用 ChatRequest / ChatResponse / ChatStreamEvent；可控工具仅提供 Handler，通过现有 Tool / ToolRegistry / ToolExecutor 校验与执行；临时工作区直接构造 WorkspaceContext；业务异常扩展已有 exception 模块。新增生产模块仅承载原仓库尚无实现的推理契约与纯状态转换，未改写现有 RunContext 或执行基础设施。

批次 1 交付时的边界（持久仓储与预算现已由批次 2 补齐）：

- reducer 校验已提交反馈的转换是否合法，不负责生成路由、规划或模型判断。真实 Planner / Actor / Verifier 仍按后续批次实施。
- `evidence_refs` 当前只校验引用及覆盖关系；真实检查、证据来源和代码/环境版本失效由批次 4 接入。未知依赖时，对 Todo 依赖及共享验收项保守重开。
- TaskContract 在本批事件中不可修改。原地修订 Todo 不能改写 objective / acceptance_refs / done_when；重新分解可使用新 Todo ID，但必须保留原 Task 验收覆盖。这是结构约束，不能证明任意自然语言描述在语义上没有弱化，最终仍需任务级验证。
- REPAIR 保留整个 StagePlan 并分配新 attempt_id；REPLAN_STAGE 保留阶段 ID、递增计划版本；INVESTIGATE / ADVANCE 的新阶段使用新 ID；RETRY_CHECK 留在当前尝试的验证阶段。等待恢复保留原阶段与尝试，终态运行不再接收推理事件。
- 状态可 JSON 往返，但尚未接入持久仓储、数据库 CAS 或预算；纯 reducer 的版本检查不能替代数据库原子提交。

### 5.2 批次 2

**交付结果：共享角色调用与持久执行账本已接通，累计 65 项离线测试通过。** 本批新增 31 项行为检查，使用真实 SQLite 迁移、真实工具执行器、工作区与 Dulwich；模型网络和 Shell 使用测试替身。

| 实际位置 | 已实现行为与复用关系 |
|---|---|
| `loop/config.py`、`config/loop.example.toml` | Loop 限额、请求重试、Prompt 版本；复用已有 TOML 读取，工具配置仍由 tools/config.py 负责 |
| `loop/context.py` | 基础 Context Compiler；直接生成 ChatRequest，注入契约、当前 Todo/Stage、状态中的反馈及观察；拒绝不完整工具配对和超容量输入 |
| `loop/model_service.py` | 四类角色共用 ModelBackend；Schema 与阶段身份校验；调用记录、重试和预算；角色响应不直接推进 RunState |
| 现有 `backends/base.py`、`openai_compatible.py`、`model/models.py` | 解析实际生效请求；Loop 请求单独设置 backend_max_retries=0，原独立 Backend 调用仍继承原有配置 |
| `loop/repository.py` | 复用 SessionFactory / OrmBase；任务状态、消息、模型与工具尝试、完整输出 Artifact、预算和 RunState 原子版本更新 |
| `loop/tool_service.py` 与原 `tools/executor.py` | 原执行器拆出 admit / execute_admitted；Loop 以持久准入替代内存准入，其余参数、Hook、并发、超时与取消继续复用 |
| 原 `run/context.py`、`run/factory.py` | 扩展 create_loop_services，在已有 RunContext 上装配共享调用边界；重建服务不清零计数、不覆盖旧状态 |
| 原文件修改工具、`run_command` 和 `workspace/mutation.py` | 将账本分配的 operation_id 通过可信执行上下文传入已有修改服务与沙箱；不新增模型可伪造的操作身份参数 |
| `persistence/alembic/versions/0002_loop_ledger.py` | 增加七张 Loop 表，保留三张已有工作区历史表；预算与 Run 行一起更新，不另建一套数据库管理器 |
| `examples/16_loop_calls.py` | 从契约调用 Planner、安装 Todo 状态、演示一次受控工具执行和重建后的旧结果读取；复用 tests/support 中的离线替身 |

本批将基础 Context Compiler 收在 `loop/context.py`，预算准入与结算收在同一仓储事务中，未为了匹配拟定目录而另建薄封装。后续职责扩大时再按实际需要拆分。

验收覆盖：

- 六个线程重复提交同一逻辑 Tool Call，仅一个取得执行权，其余得到 UNKNOWN；逻辑调用和执行尝试都只计一次。
- 确认未进入 Handler 的审批拒绝可以显式重试：逻辑计数保持 1，执行器派发尝试增加为 2。已成功、效果不明或无法证明未执行的调用不允许重放。
- 关闭并重新创建 DatabaseManager、LoopRepository 和 RunContext 后，已完成结果与计数可恢复，Handler 不再次执行。
- `run_command` 外层调用成功、退出码为 1 时，结构化数据仍明确表示业务失败；摘要长度受限，5000 字符的原始 stdout 可通过 Artifact 读取。
- 写入、替换、补丁、回滚与命令变更复用现有工作区服务；命令的 Sandbox operation_id 与工作区操作记录一致；模型传入伪造身份会被原参数校验拒绝。
- 模型重试逐次记录并计量，未知 usage 保留预留额度；无效 JSON、截断输出和错位身份不被接受；旧事件与改变参数的重复逻辑 ID 被拒绝。
- 测试数据库先建立 `0001_workspace_history` 的真实操作记录，再升级到 `0002_loop_ledger`；旧 Commit 关联仍可读取，迁移后的结构与 ORM Metadata 一致。
- 异步角色与工具调用中的账本操作通过线程执行；模拟数据库等待时，其他协程仍可推进，不被同步数据库 I/O 阻塞。

验证入口：

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -X utf8 examples/15_loop_state.py
.\.venv\Scripts\python.exe -X utf8 examples/16_loop_calls.py
~~~

当前边界与后续衔接：

- 四类角色的调用边界已经可调用真实 Backend；本批测试没有访问真实模型，尚不说明规划质量。循环调度、流式消费和多轮工具回填在批次 3，完整 Planner / Executor 主流程在批次 5。
- Context Compiler 当前保留完整输入，容量不足明确拒绝。默认 Token 估算使用 UTF-8 字节数加消息开销，可注入模型 tokenizer 计数；估算不等于 Provider 精确计费。usage 已知时按实际计量结算，未知时保留预算，避免把失败请求当免费调用。
- `DISPATCHED` 表示已交给外部执行边界；重启或外层超时后无法证明效果的调用返回 UNKNOWN，不自动恢复执行。完整恢复核对、持久审批、Steering 和进程级运行所有权仍按批次 7 实现。
- 工具逻辑准入次数与执行器派发尝试分开保存；后者包含参数校验、审批等尚未进入 Handler 的失败，不能当作成功业务操作数。Loop 的计数以账本为准，RunContext.tool_state 是恢复后的运行时投影，同一 Run 不应混用绕开账本的工具入口。
- Artifact 保存 Handler 已经返回的完整业务数据及输出摘要，数据库使用现有应用存储；沙箱自身已经截断的 stdout/stderr 不会被凭空恢复。极小摘要限额容不下 Artifact 元数据时，引用仍保存在 ToolExecutionResult.artifact_id 中，判断业务状态始终读取 data。
- 已验证 SQLite 的升级与并发行为；MySQL 大字段使用 LONGTEXT 类型适配，MySQL / PostgreSQL 的真实数据库联调仍在后续生产检查中完成。

### 5.3 批次 3

**交付结果：单阶段 Action Agent Loop 已贯通，累计 86 项离线测试通过。** 本批新增 21 项测试；固定分页任务实际经历读取源文件、原回归失败、修改文件、回归通过，再交回候选结果。

| 实际位置 | 交付内容 |
|---|---|
| `loop/actor.py` | Actor.run 接收 StagePlan / attempt_id，循环调用模型与工具；只提交 StageOutcome，状态到 VERIFYING 为止 |
| `loop/scheduler.py` | 使用已有 ToolService 执行批次；独立只读段有界并行，写入和未知效果形成顺序屏障 |
| 原 `loop/model_service.py`、`backends/openai_compatible.py` | 复用已有流聚合，只有闭合的完整响应才能交给 Actor；补充流资源关闭和阶段版本检查 |
| 原 `loop/models.py`、`loop/config.py`、`loop/context.py` | 阶段交回条件引用、行动轮数/协议纠错/批次上限、Actor Prompt v2；按既有配置快照校验 |
| 原 `tools/base.py`、`tools/registry.py` 与内置工具 | 新增可信 ToolEffect / parallel_safe 声明；读取和搜索允许并行，带游标的目录读取仍顺序执行 |
| 原 `loop/tool_service.py`、`loop/repository.py` | 工具绑定记录效果声明；拒绝基于旧阶段版本的新行动；复用既有调用身份、缓存、预算与原子状态更新 |
| 原 `run/factory.py` | create_loop_services 返回的 services.actor 可以直接执行给定阶段 |
| `examples/17_stage_actor.py` | 使用真实已配置 Backend 与 SRT 的独立分页修复示例，支持 --stream；复用原 Provider 与工作区历史服务 |

执行规则：

- 每个模型轮次生成稳定逻辑调用 ID；Provider 在不同轮次重复使用工具 ID 时，对回填历史做确定性命名隔离，保留 reasoning_content 与原始响应记录。
- 只有显式声明 READ_ONLY 且 parallel_safe 的工具可以组成并行段。写工具不能声明 parallel_safe；UNKNOWN 和未声明工具均顺序执行。
- 任一调用失败或业务退出码非零时，无法证明后续调用可独立继续的部分返回 SKIPPED，下一模型轮根据真实结果修正。并行段内已经派发的独立读取会全部收齐。
- 流未闭合、响应截断或协议不合法时，不执行其中的工具；工具参数不合法则由原 ToolExecutor 返回错误观察。协议修正有独立次数上限，同时消耗共享模型预算。
- candidate 必须引用计划中已有的停止条件并提供产出或观察依据；needs_replan 必须引用重规划条件并报告前提变化。未知条件和不存在的观察引用会被拒绝；已报告前提变化的结果不能作为 candidate 交回。
- 预算、审批、取消或未知执行效果导致 blocked；反复协议错误或达到行动轮数上限导致 stalled。Actor 不提交 Todo PASS 或 Task PASS。
- 同一尝试重入时按确定性调用身份从账本重建已完成轨迹，不重做已有工具行动，也不重置轮数。完整中断恢复、单写者所有权和 Steering 仍在批次 7 实施。

验证结果与运行入口：

~~~powershell
# 离线测试：真实临时文件、Dulwich 和数据库；模型为脚本替身。
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 真实模型与 SRT 示例：需要原有配置文件及已可用的沙箱。
.\.venv\Scripts\python.exe examples/17_stage_actor.py
.\.venv\Scripts\python.exe examples/17_stage_actor.py --stream
~~~

已验证正常及流式工具回填、半截参数不执行、SDK 增量聚合与流关闭、错误后修正、只读并行/写屏障、在途取消、状态变化后的旧调用拒绝、预算与批次上限，以及同一版本下的重入去重。真实示例已验证导入与命令入口，本批没有实际访问模型服务或启动 SRT 进行端到端评测。

边界说明：stop_when / replan_when 是自然语言契约，本批验证引用、状态和依据结构，并让模型每轮检查条件；不能由引用正确推出条件在现实中确实成立。StageOutcome 仍是候选或阻塞报告，真实成功判定由批次 4 的 Verifier 完成。示例中的“不得修改测试”同样属于待验证的任务约束，不能仅凭 Actor 声明证明遵守。

Prompt 默认版本升级为 `loop-roles-v2`，保留 v1 供旧策略读取与原角色调用使用；不把已有 Run 的策略静默改成 v2。新增配置默认值可以用于读取旧快照，但策略、请求上下文或工具效果声明不一致时仍拒绝复用同一个逻辑调用；历史结果可通过原读取接口取得。建议新的行动任务使用新 Run 和 v2 策略。

上述为批次 3 交付记录；证据验证现已由批次 4 补齐。

### 5.4 批次 4

2026-09-14 完成 Stage / Todo / Task 验证与证据有效性，累计 **115 项离线测试通过，其中本批新增 29 项专项测试**。这批交付能实际运行检查、保存证据并提交验证事件的控制器；Planner / Executor 的全任务调度由下一批串联。

复用与新增职责：

| 位置 | 本批实现 |
|---|---|
| `loop/tool_service.py` | 增加仅供检查控制器调用的 execute_check；复用参数校验、Hook、审批策略、取消、预算与防重放 |
| `loop/repository.py` | 复用 Artifact 表存储不可变检查意图、证据和报告；CAS 提交支持写锁内的文件前置条件复核 |
| `workspace/version.py` | 提取共用文件扫描，增加不推进 HEAD 的内容/权限指纹；历史原有 ignore 规则继续保留 |
| `verification/contracts.py` | CheckSpec、CheckEvidence、CheckObservation、QuestionConclusion、FailureBundle |
| `verification/evidence.py` | unittest / pytest / 明确命令断言的四态解析与结果归约 |
| `verification/runner.py` | 实际检查、稳定执行身份、重复检查、基线比较、文件/定义/环境有效性 |
| `verification/controller.py` | 三层覆盖、ProgressDelta、可选 Judge、验证事件提交与失效 Todo 重开 |
| `run/factory.py` | 原 create_loop_services 装配结果增加 verifier |
| `examples/18_verification.py` | 复用现有离线 Fixture，演示最终通过与最终集成失败两条轨迹 |

没有新建数据库表、迁移版本、Shell 执行器、模型协议或预算系统。数据库 head 仍为 `0002_loop_ledger`，原有独立工具与 Actor 调用方式保持兼容。

**三个完成边界如何判定：**

1. Stage 检查的 targets 必须指向当前 StagePlan.expected_results。每个预期都被有效 PASS 检查覆盖，阶段才通过。
2. Todo 检查独立覆盖 acceptance_refs 和 done_when；只查验收 ID 而遗漏自然语言完成条件仍为 INCONCLUSIVE。调查检查可通过受信任的 QuestionConclusion 生成“已解决 / 已缩小 / 已排除”进展，并引用真实检查凭据；不会把调查结果当成代码修复完成。
3. Task 重新执行自己的检查，覆盖原始 criteria 和 constraints，不能使用全部 Todo 的勾选代替最终检查。最终产物和集成行为由应用提供对应 CheckSpec。确认某个验收项失败会复用 reducer 重开相关 Todo 和依赖；原始约束失败会阻止整个任务完成，并保守重开 Todo。

缺少检查或日志证据时返回 INCONCLUSIVE，报告包含没有证据引用的缺口项与补采建议，不虚构一个 Evidence ID。ERROR 表示检查没有给出可用业务结论，例如沙箱不可用、收集/运行错误、取消或效果未知。Failure Bundle 带检查原因、完整输出 Artifact 引用、基线标记及建议下一步；本批不代替 Executor 选择 REPAIR / INVESTIGATE 等路由。

**调用与输入约定：**

```python
from qharness.verification import CheckSpec

# 应用根据实际运行时、依赖和外部服务生成身份；不是让 Actor 填写一个任意标签。
context.metadata["verification_environment"] = verified_environment_digest

verdict = await services.verifier.verify_stage("verify-A1-1", [
    CheckSpec(id="stage-regression", scope="stage", targets=stage_plan.expected_results,
              command="python test_pagination.py", failure_exit_codes=(1,),
              inputs=("pagination.py", "test_pagination.py")),
    CheckSpec(id="todo-regression", scope="todo",
              targets=(*todo.acceptance_refs, *todo.done_when),
              command="python test_pagination.py", failure_exit_codes=(1,),
              inputs=("pagination.py", "test_pagination.py")),
])
report = services.repository.read_artifact(services.verifier.report_ref("verify-A1-1"))
```

上例是分页任务的命令断言：脚本本身必须确实验证这些 targets，不能因为接口支持批量绑定就把任意成功命令标成覆盖全部需求。测试框架使用 `kind="unittest"` 或 `kind="pytest"`；普通 `command` 只在可信命令定义明确成功语义时使用，非零退出码默认 ERROR，显式声明的 failure_exit_codes 才表示业务断言 FAIL。

检查定义、目标映射和环境身份由应用端或受信任的检查配置提供。模型可以建议检查，但不能直接替换这些定义、削弱 targets 或用自己的文字创建 PASS。`execute_check` 不注册为模型可见工具；Judge 仍经过已有 ModelService，消耗共享模型预算。

**证据有效性与异常语义：**

- 同时记录检查前后文件指纹、检查定义/版本、运行时/环境身份、Run 版本和 Attempt。检查运行、后续检查或 Judge 等待期间改变输入，会使旧结论失效；提交前再在数据库写锁内复核文件和检查定义。工具结果完成但证据尚未写入时进程中断，重建后也不能把旧结果重新绑定到新文件。
- `inputs` 必须覆盖实现、测试、测试配置和锁文件等实际依赖；它是完整依赖声明，不只是模型最近改动的文件。未提供时保守扫描工作区普通文件，除 .git/.qharness 元数据外不采用 .gitignore，以免遗漏隐藏的代码或新增文件；链接/不可读输入无法可靠快照时不发放 PASS。扫描成本随工作区增大，已知依赖时应显式声明。
- 工作区外的解释器、包环境、服务/数据集版本需要由 `verification_environment` 身份覆盖；也可用 `CheckRunner(tools, environment=provider)` 注入动态提供器。环境身份缺失属于装配错误，不默认为一个稳定环境。共享可变文件系统没有跨进程原子快照，本批的前后快照和提交 guard 不是文件系统事务；更强单写者/恢复协调继续按批次 7 实施。
- `verifier.refresh(current_specs)` 在 PLANNING / VERIFYING_TASK 边界核对已通过 Todo 的凭据；文件、环境、检查定义改变或检查定义消失时，复用 ReopenTodos 传播失效。第五批应把它接到规划边界。独立 Task 检查始终基于当前输入重新验收。
- 零测试、全跳过、全 expected failure、未知测试摘要和不完整沙箱日志不构成 PASS。工具给模型展示的摘要截断时，可读取完整 Artifact 判定；沙箱已丢弃的日志不能通过 Artifact 凭空恢复。测试异常按 ERROR 留待诊断，避免把环境问题直接路由成改业务代码。
- `repetitions` 显式限定重复检查次数，结果、测试数量或失败签名不同则保守标记 Flaky / INCONCLUSIVE，不采用最后一次 PASS。超时/取消导致未知执行效果时立即停止重复派发，沿用原工具账本的未知结果边界。
- `baselines={check_id: evidence_ref}` 比较同定义、同环境的既有稳定失败及失败签名。相同失败可标记 pre_existing，但仍保留 FAIL；不会因为“原来就红”而跳过验收，也不把所有失败归因于本次修改。基线可在修改前的调查阶段采集。
- 对“不得修改测试”这样的约束，应提供受信任的原始测试摘要比对检查；仅把当前测试文件列入 inputs，不能证明 Actor 之前没有削弱过它。原始约束本身同样必须被 Task 检查覆盖。

**语义 Judge 的定位：**

`verify_stage(..., judge=True)` / `verify_task(..., judge=True)` 将证据和完整输出交给已有 Judge 角色。它可以确认检查的语义覆盖，或保留 ERROR / INCONCLUSIVE；不能覆盖确定性 FAIL，不能把缺证据变成 PASS，也不能将任意 evidence_refs 或 ProgressDelta 写入完成账本。确定性检查全绿但模型仍发现语义问题时保留 INCONCLUSIVE、要求补证；模型反对意见本身不升级为已证实的业务 FAIL。模型 JSON 不合法则沿用角色调用协议错误，不提交成功事件。

已验证三层完成隔离、问题进展、覆盖遗漏、约束违反、任务集成退化、文件/环境/定义失效、提交时竞态、重建防重放、日志截断、Flaky、基线失败、取消、未知执行、预算共享、租户隔离，以及 Judge 不能捏造或覆盖证据。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -X utf8 examples/18_verification.py
.\.venv\Scripts\python.exe -X utf8 examples/18_verification.py --integration-failure
```

这些是离线行为验证，检查输出使用既有可控沙箱；没有把它们描述为真实模型或 SRT 的端到端评测。真实环境与跨任务效果仍须在后续联调和批次 8 验收。

上述为批次 4 交付记录；完整任务调度现已由批次 5 串联。

### 5.5 批次 5

2026-09-14 完成 Planner + Executor 主线，累计 **133 项离线测试通过，其中本批新增 18 项任务流程测试**。应用调用一次 Executor 即可从 TaskContract 经初步 Todo、动态阶段、Actor、Verifier 和反馈路由运行至完成、等待或终止。

本批复用原 ModelService、ContextCompiler、Actor、VerificationController、RunState/reducer 和 LoopRepository。新增生产文件仅为 `loop/planner.py`、`loop/executor.py`、`loop/feedback.py`；全局规划、阶段规划及 Todo 选择在 Planner 内复用同一角色调用机制，没有为文件清单中的 stage_planner/controller 再建薄包装。受信任检查目录 CheckCatalog 扩展在已有 verification/controller.py 中，没有新增数据库表或迁移版本。

**运行入口：**

```python
from qharness.run import create_loop_services
from qharness.verification import CheckCatalog

services = create_loop_services(context, backend=backend, executor=tool_executor,
    database_manager=database, config=loop_config, contract=task_contract)
state = await services.executor.run(
    CheckCatalog(tuple(trusted_check_specs)),
    observations=(initial_workspace_summary,),
    stream=False,
    judge=False,
)
```

TaskContract 仍由应用根据用户任务建立；检查定义、输入依赖和环境身份遵循第 5.4 节。CheckCatalog 按明确 targets 选择检查，原定义同时进入 Planner 上下文，帮助模型制定可验收的阶段。它不会因为模型临时改写预期结果，就自动把一个已有成功命令绑定到新目标；未覆盖的目标仍交给 Verifier 判为 INCONCLUSIVE。模型和应用可以协作补充检查，但检查的可信授权边界不变。

**调度及完成语义：**

- 首次运行调用 Global Planner，输出经过契约覆盖、依赖图和初始版本校验的 TodoPlan 后，沿用 repository.install_state。已有 Run 不重新初始化计划或预算。
- Todo 选择按依赖是否通过确定；清单顺序只用于在多个就绪 Todo 中选择，不能跳过依赖。当前 Todo 尚未完成时，ADVANCE、修复和调查继续围绕它进行。
- Stage Planner 接收当前 Todo、未决问题、进展记录、反馈和最近的真实检查输出；输出先用原 reducer 预检，再提交 StartStage。非法输出允许有限次协议修正，每次仍计入模型预算。
- Actor 独立运行。REPAIR 不调用 Stage Planner，完整复用原 StagePlan，仅分配新的 Attempt；最近的实际失败日志会回填修复上下文，旧证据只作历史依据，不被当成新的通过凭据。
- Verifier 提交 StageVerdict 后，基础路由选 ADVANCE、REPAIR、INVESTIGATE、REPLAN_STAGE、REPLAN_TODO、RETRY_CHECK 或 WAIT。RETRY_CHECK 留在当前 Attempt 的 VERIFYING，不重复 Actor 和阶段规划。REPLAN_STAGE 保留 Stage ID 并递增计划版本；INVESTIGATE 建立当前 Todo 的新调查阶段。
- 基础策略在有明确失败、没有报告前提改变时允许局部修复；修复上限后调查，前提变化时先修订阶段，阶段修订上限后可凭当前证据重新分解 Todo。任务分解修订也有上限。该策略还不是批次 6 的精细错误分层或自适应粒度算法。
- TodoPlanPatch 包含 base_version、修订后的 plan、reason 和 evidence_refs。版本必须匹配、内容必须实际改变、证据必须来自本次反馈并能在当前 Run 读取；原 TaskContract 不变。复用 ReplaceTodoPlan 对保留/新增 Todo 和依赖失效的处理，不能原地削弱旧 Todo 的完成义务。Patch 同时保存在原 Artifact 表供核对。
- 规划边界调用现有 refresh 重开失效 Todo；全部 Todo 通过后运行独立 Task 检查。集成 FAIL 会重开受影响 Todo，再回到规划/执行；验收证据缺失会补查或等待，不把缺失证据等同于业务回归。
- WAIT 保留当前恢复位置，不会自动循环唤醒；应用可通过已有 Resume 和验证/反馈事件继续协调。TERMINATE 可由应用显式调用 executor.terminate(reason)，或由受信任的 FeedbackRouter 扩展返回终止决策。所有路由最终均受原 reducer 校验约束。

**预算与执行边界：**

`LoopConfig` 和 `config/loop.example.toml` 增加 max_stage_attempts、max_stage_repairs、max_stage_replans、max_todo_replans、max_check_retries、max_run_events；次数从已有状态和历史推导，服务重建或 Resume 不清零。旧配置通过默认值读取；原模型/工具总预算、SDK 重试控制和审批机制继续生效。

模型/工具存在已派发但未确定的调用时，Executor 进入 WAITING。同一轮某个检查效果未知时，原 ToolService 也阻止后续检查派发，不能用新的调用 ID 绕过未知效果。取消会停止新行动；已发生的效果仍以账本为准。状态过期、身份冲突和存储损坏会明确报错，不被包装成成功状态。

首次 TodoPlan 尚未建立便遇到模型错误或取消时，返回初始化异常，保留已有调用账本；不能虚构一组 Todo 以制造可等待的 RunState。初始 RunService/收件箱和完整跨进程恢复语义继续由批次 7 处理。

本批只有进程内 Executor 锁和原数据库 CAS/调用身份保护，不声明跨进程单写者接管已完成。WAIT 的外部条件解除、未知调用核对、审批回复与 Steering 仍需应用通过现有接口协调；Resume 本身不会清空重试预算或证明未知效果已消失。重入时应保持同一检查目录、grounding 和流模式，改变调用绑定会被账本拒绝。上下文继续采用现有容量边界，容量不足会等待或报错，不暗中丢弃验收要求。

**已验证轨迹：**

1. 初步 Todo → 调查通过但 Todo 失败 → 实施失败 → 完整保留 StagePlan 修复 → Todo 通过 → 独立 Task PASS。
2. Todo 全通过 → Task 集成 FAIL → 重开相关 Todo → 再规划/执行 → Task PASS。
3. 临时检查错误只重试检查；持续错误和最终覆盖不足达到上限后等待。
4. 前提变化触发同 ID 的 Stage 修订；局部修复上限触发调查；有据的 TodoPlanPatch 替换受影响工作项，保留未改动项和原始验收覆盖。
5. 环形初始计划先修正才允许行动；前向依赖按就绪状态选 Todo；伪造 Patch 证据不能替换计划。
6. 规划中取消、Actor 阻塞、未知检查、完成后重入、阶段预算跨 Resume 保留，以及从持久 REPAIR 状态直接进入新 Actor 尝试。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -X utf8 examples/19_task_executor.py
.\.venv\Scripts\python.exe -X utf8 examples/19_task_executor.py --integration-failure
```

示例 19 外部只调用一次 `services.executor.run()`，不手工安排 Stage/验证/反馈事件。它复用 ScriptedModelBackend 和可控 Sandbox Fixture，文件通过原 write_file/WorkspaceMutationService 真实落盘，检查返回预设规则下的结果。此结果证明控制流程与调用边界，不能代替真实模型、真实测试进程或 SRT 端到端效果评测。

上述为批次 5 交付记录；三项推理改进现已由批次 6 接入。

### 5.6 批次 6

2026-09-14 完成三项推理策略，累计 **158 项离线测试通过，本批新增 25 项测试**。继续扩展原 Planner、FeedbackRouter、Verifier、ContextCompiler 和 reducer；唯一新增生产模块为 `loop/progress.py`，用于从已有 CheckEvidence 派生知识和缺口。指导信息、诊断和进展复用 RunState / Artifact，不新增模型 Backend、工具执行通道、数据库表或迁移。

**配置与兼容：**

```python
config = LoopConfig(
    layered_feedback=True,
    dynamic_stage_planning=True,
    track_gap_progress=True,
    max_no_progress_investigations=2,
)
```

`config/loop.example.toml` 显式启用三项机制；直接使用 `LoopConfig()` 或读取缺少这些字段的旧 Run 时保持 False，避免重建服务就静默改变旧策略。每个 Run 的策略仍由原仓储固定；比较策略应创建独立 Run，不能中途换开关并重置预算。关闭某项机制不关闭权限、执行账本、原验收覆盖、版本校验或独立 Task 验证。旧 CheckSpec 没有新增元数据时，其检查定义指纹保持兼容。

| 开关 | 启用后的职责 | 关闭后的行为 |
| --- | --- | --- |
| layered_feedback | 按证实的错误层级选择调整范围，输出 preserve / invalidate / focus | 使用原有基础反馈策略；已证伪前提仍不能沿用修复 |
| dynamic_stage_planning | 根据关键问题和验收缺口约束阶段类型 | 不注入阶段类型建议或强制类型；保留原路由对调查等行为的约束 |
| track_gap_progress | 校验阶段关联、按实际发现去重并处理连续无进展 | 不启用关联约束和无进展路由；原完成验收始终生效 |

任一策略需要时，Verifier 都可派生 ProgressReport，作为共同的事实输入；开关控制的是各项决策和约束，不是关闭其他机制所需的基础证据。

**分层反馈如何落地：**

应用在原 CheckSpec 上可补充 `on_failure=DiagnosisRule(...)`，声明这个具体断言失败能够证明的层级：local_action、stage_assumption、todo_decomposition 等。Verifier 只为实际、仍有效的 FAIL 生成 FailureDiagnosis；未配置诊断的失败标为 unknown。普通测试退出码只能证明失败，不能自动证明根因，模型 Judge 的诊断文字也不能替代检查依据。

- 局部失败证据充分时，沿用原计划作有界 REPAIR；不再调用 Stage Planner，不重做全局 Todo。
- 检查否定阶段前提时选择 REPLAN_STAGE，标出具体失效前提。新计划不得继续使用这些前提；关闭实验策略也不能绕过原方案修复的正确性约束。
- 检查证明任务分解问题时才走 REPLAN_TODO，仍需原 TodoPlanPatch 和原始契约覆盖检查。
- 原因未知的阶段失败，或仅有 Actor 声称前提变化时，先 INVESTIGATE。必要时增加关联原验收项的问题；不会把模型猜测直接升级成全局计划错误。
- 检查环境 ERROR 优先有界 RETRY_CHECK，保留业务方案。证据不足保留原补查/等待边界，不假定重写代码可以解决。

FeedbackDecision 的 preserve / invalidate 使用有版本的 task、todo、stage、approach、assumption 和 evidence 引用，focus 指向当前 Todo 的验收项或相关问题。reducer 拒绝未知对象、无关 Todo、原任务义务的失效请求，以及一边保留方案修复一边否定其前提的矛盾决策。这些字段说明下一阶段的保留/调整范围；不会删除历史证据、自动撤回文件或修改用户契约。实际重规划、证据重查和完成传播继续通过现有事件执行。

**动态阶段与缺口关联：**

原 Planner 在规划前用当前文件、环境和完整检查目录重算 ProgressReport。相关未决 Questions 视为当前关键不确定性，优先 INVESTIGATE；没有这些问题且尚有验收缺口时 IMPLEMENT；验收项已有有效通过凭据、但 Todo 的 done_when 或语义确认仍未完成时 VALIDATE。明确任务允许一个实施阶段完成，不强制先调查，也不强制拆成固定数量小步骤。Question 是否关键由应用提供及受信任验证反馈控制，当前没有学习得到的重要性评分。

控制器给出类型、focus 和已有证据，模型仍负责本轮 expected_results、approach、stop_when / replan_when，以及可选 information_sources。StagePlan 的类型、版本和缺口关联先校验，非法输出进入原有界协议修正；Actor 继续按原停止/重规划条件交回。REPAIR 完整保留原计划，不在修复入口重新生成阶段。

启用缺口关联时，Stage 检查需要用 `addresses` 或 QuestionConclusion.question_id 关联原验收项/问题，不能仅有一个与 Todo 无关的“命令运行成功”。StagePlan.addresses 必须指向当前未解决缺口，并有受信任阶段检查覆盖。`information_sources` 可选取已有 Stage 检查 ID；模型不能据此删减 Todo / Task 检查。

```python
from qharness.verification import CheckSpec, DiagnosisRule, QuestionConclusion

investigation = CheckSpec(
    id="exclude-input-branch", scope="stage", targets=("排除输入校验分支",),
    command="python -m unittest tests.test_input_diagnosis", kind="unittest",
    inputs=("pagination.py", "validation.py", "tests/test_input_diagnosis.py"),
    addresses=("Q1",),
    conclusions=(QuestionConclusion(question_id="Q1", kind="eliminated",
        finding="输入校验分支不是当前分页错误的原因", fact_id="pagination-not-input"),),
)
```

上例绑定必须确实由该检查证明，不能给任意绿色测试贴上调查结论。resolved / narrowed / eliminated 由应用定义检查的成功语义。普通检查未配置结论时，仍能验证阶段，但不会凭空产生问题进展。运行中出现目录尚无法验证的新问题时，需要应用补充相应检查；当前会保留缺口并在有界尝试后等待，不把模型生成的检查直接授予可信地位。

**保留发现和停止重复调查：**

ProgressTracker 从原 CheckEvidence 与 ProgressDelta 重建有效发现。文件依赖、环境、检查定义改变/消失，或同一检查后来明确失败时，旧发现不再作为有效依据。Todo 重分解后，仍关联原验收项且证据有效的调查结果继续可用；不用重新读取同样的事实。

发现优先用应用提供的 fact_id 标识；未提供时只做 Unicode、空白和大小写规范化后的文字去重，不声称具备任意语义等价识别能力。排除假设和缩小范围也算新信息；重复产生不同 Evidence ID、改写 Stage 目标、增加文件读取量或自评分不算新发现。调查结论只更新 Questions，不把阶段 PASS 变成 Todo PASS。

只有已完整交回且有稳定 Stage PASS 检查的调查才参与连续无进展计数。首个新发现不计停滞；连续两次没有新增验收/问题信息时，反馈要求 change_strategy 并有界重规划。Planner 按实际检查命令、cwd、类型和输入依赖比较来源；换检查 ID 或阶段措辞无法绕过，新来源仍须来自应用目录。真实新发现重置停滞计数；不存在可用替代来源时有界等待。ERROR / INCONCLUSIVE、Actor 内尚在分页、轮询或长工具执行都不递增调查停滞；已有工具与 Actor 总预算仍然适用。

**验证与示例：**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -X utf8 examples/20_reasoning_strategies.py
.\.venv\Scripts\python.exe -X utf8 examples/20_reasoning_strategies.py --ablation
```

示例 20 用一次 Executor 调用运行“排除假设 → 重复两次没有新信息 → 更换来源并定位问题 → 实施失败 → 局部修复 → 独立 Task 验收”。对照模式枚举 2³ 种开关组合，在同一明确的局部修复 Fixture 上记录实际预算和阶段数：八组均完成，均为 3 个 Attempt、7 次模型调用尝试、10 个工具逻辑调用。这是开关与执行语义对照，不能据此推断策略提升了成功率或降低了成本。

专项测试还覆盖：类型和无关检查在 Actor 前被拒绝、缺少 done_when 时选择 VALIDATE、原 Task 覆盖缺失仍等待、未知失败先调查、前提失效不能重用、改名无法规避停滞、不同来源的排除/缩小范围算进展、调查证据过期、有效知识跨 Todo 重分解保留、在途阶段不增加停滞、环境故障只补查、原共享预算仍生效，以及旧配置和旧检查指纹兼容。原 133 项行为检查继续通过。

**下一步为批次 7：长期任务、用户交互与恢复联调。** 在现有 ContextCompiler、RunContext、仓储和 Executor 上完成上下文工作记忆与压缩、RunService/收件箱、Steering 与审批回复、恢复核对和单写者协调。真实模型、SRT 与跨任务策略收益评测继续按批次 8 验收。

## 6. 进度维护规则

每个批次完成后更新勾选项，并补充实际文件、验证命令、结果和遗留限制。局部完成可以逐项勾选，批次出口未满足时不能标记整批完成。

如实现中发现设计冲突，优先保持用户目标、三层完成语义和执行正确性，更新对应设计说明及受影响用例。接口调整应解释原因，不通过跳过验收或压缩任务范围解决困难。
