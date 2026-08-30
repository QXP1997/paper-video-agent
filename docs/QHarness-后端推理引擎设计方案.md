# QHarness 后端推理引擎设计方案

> 文档状态：初稿  
> 当前阶段：架构设计  
> 产品形态：本地单用户 Coding Agent Runtime

## 1. 项目定位

QHarness 是一套运行在用户本地的 Coding Agent Harness。它位于大语言模型与本地操作系统之间，负责驱动模型持续完成以下闭环：

```text
理解任务 → 组织上下文 → 调用模型 → 执行工具 → 获取环境反馈
        → 再次推理 → 验证结果 → 完成任务或请求用户介入
```

QHarness 不负责训练或托管模型，也不是 vLLM、llama.cpp 一类模型推理服务。它通过模型厂商 API 或本地 OpenAI-compatible 接口使用模型。

首个版本定位为：

- 本地运行；
- 单用户；
- 单 Agent；
- 面向代码阅读、修改、命令执行和验证；
- 以后可接入 CLI、TUI、桌面客户端或 IDE 插件；
- 核心引擎不依赖任何特定前端。

## 2. 当前范围

### 2.1 第一阶段实现

- Agent 推理循环；
- Run、Turn、Step 状态管理；
- OpenAI-compatible 模型接入；
- 文件读取与搜索；
- Patch 修改；
- Shell 命令及长进程管理；
- Git 状态和 Diff；
- Workspace 边界控制；
- 本地权限判断与用户审批；
- SQLite 持久化；
- 中断恢复；
- Context Token Budget；
- 会话压缩；
- 结构化日志和运行轨迹。

### 2.2 暂不实现

- Web 或桌面前端；
- 多租户；
- 独立模型网关；
- LiteLLM Proxy 部署；
- PostgreSQL、Temporal、S3 等服务端基础设施；
- 远程 Worker；
- 多 Agent 与 Agent 间协作；
- A2A 协议；
- 企业级集中策略中心；
- 长期用户记忆；
- 可视化工作流编排。

A2A 后续只作为外围协议适配层，不进入核心运行时数据模型。

## 3. 设计原则

### 3.1 本地优先

默认运行时应当是单进程、无外部服务依赖的。用户安装 QHarness 后，只需配置模型凭据即可使用。

### 3.2 小内核

Kernel 只负责状态推进、事件分发、恢复和停止判断。模型协议、工具实现、存储方式和前端表现通过接口隔离。

### 3.3 完整历史与模型上下文分离

SQLite 保存完整、可审计的运行历史；Context Compiler 根据当前模型、Token Budget 和任务状态生成本轮请求。不能把数据库消息列表直接作为模型上下文。

### 3.4 副作用必须受控

文件修改、Shell、网络访问、Git 远程操作等行为必须在真正执行前经过策略判断。安全不能只依赖 Prompt 中的文字要求。

### 3.5 可恢复优先

每个模型调用和工具调用都应有明确生命周期。进程退出后，应能判断哪些步骤已完成、哪些可以重试、哪些结果未知。

### 3.6 内部协议独立

模型 SDK、MCP、A2A 或未来其他协议都不得直接成为 QHarness 内部领域模型。外部协议统一通过 Adapter 转换。

## 4. 总体架构

```text
CLI / TUI / Desktop / IDE（以后）
                │
                ▼
          Harness Interface
                │
                ▼
┌─────────────────────────────────────────────┐
│                QHarness Engine              │
│                                             │
│  Agent Kernel ─────── Context Compiler      │
│       │                       │              │
│       ├──── Model Backend ────┘              │
│       │       ├── OpenAI                     │
│       │       ├── Anthropic（以后）           │
│       │       └── OpenAI-compatible          │
│       │                                      │
│       ├──── Local Policy / Approval          │
│       │                                      │
│       └──── Tool Runtime                     │
│               ├── File                      │
│               ├── Search                    │
│               ├── Apply Patch               │
│               ├── Shell / Process           │
│               ├── Git                       │
│               └── MCP（以后）                │
│                                             │
│  Workspace Manager                          │
│  SQLite Event Store                         │
│  Local Artifact Store                       │
└─────────────────────────────────────────────┘
```

## 5. 核心推理循环

```text
创建或恢复 Run
      ↓
加载 Session、Workspace 和 Instructions
      ↓
Context Compiler 生成本轮 ModelRequest
      ↓
调用 Model Backend 并消费流式 ModelEvent
      ↓
模型是否请求工具？
  ├── 否：验证停止条件和任务结果
  │       ├── 已完成 → COMPLETED
  │       └── 未完成 → 进入下一 Turn
  │
  └── 是：生成 ToolCall
          ↓
       Policy 判断
          ├── DENY → 将拒绝结果反馈给模型
          ├── ASK  → 持久化并等待用户审批
          └── ALLOW
                 ↓
              执行工具
                 ↓
              保存 ToolResult
                 ↓
              检查预算、循环和上下文
                 ↓
              进入下一 Turn
```

## 6. 核心模块

### 6.1 Domain Protocol

QHarness 自己定义并维护以下核心对象：

- `Session`：一个连续的用户会话；
- `Run`：一次需要执行和完成的任务；
- `Turn`：一次模型决策及其产生的工具动作；
- `Step`：模型调用、工具调用、压缩或审批等原子步骤；
- `Message`：用户、模型和工具的可见消息；
- `ModelCall`：一次模型请求及响应元数据；
- `ToolCall`：一次工具调用意图；
- `ToolResult`：标准化工具结果；
- `Approval`：用户对具体动作的授权；
- `Artifact`：大型文本、Diff、报告或生成文件；
- `Checkpoint`：可恢复状态；
- `RunEvent`：状态变化的事实记录；
- `Usage`：Token、费用和耗时。

所有持久化对象应包含版本、时间和关联 ID。涉及副作用的调用应包含幂等键。

### 6.2 Agent Kernel

职责：

- 推进状态机；
- 驱动多轮模型调用；
- 分发工具调用；
- 处理暂停、恢复和取消；
- 应用停止条件和预算；
- 识别重复动作与无进展循环；
- 将内部状态变化输出为 `RunEvent`；
- 在启动时恢复未结束 Run。

Kernel 不直接依赖模型厂商 SDK、SQLite SQL 语句或具体 Shell 实现。

### 6.3 Model Backend

Model Backend 是进程内接口，不是独立网关服务。

```python
class ModelBackend:
    async def generate(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelEvent]: ...
```

首版实现：

- `OpenAICompatibleBackend`。

后续实现：

- `OpenAIBackend`；
- `AnthropicBackend`；
- `OllamaBackend` 或其他本地模型 Backend。

Provider Adapter 必须同时保留：

1. QHarness 统一的 Canonical Event；
2. 厂商原始 Wire State。

原始状态可能包含 response ID、reasoning item、加密数据、tool call ID、缓存信息或 continuation token，不能在统一转换时丢失。

### 6.4 Context Compiler

输入：

- Session 完整历史；
- 当前 Run 状态；
- System Instructions；
- 项目指令；
- Skills；
- Workspace 信息；
- Tool Schema；
- Model Capabilities；
- Token Budget。

输出：

- 某个 Model Backend 可直接使用的 `ModelRequest`。

主要能力：

- 指令发现和优先级合并；
- 历史消息选择；
- 工具结果截断；
- 文件内容选择；
- Token 估算和预算；
- Prompt Cache 友好的内容排序；
- 自动 Compaction；
- 压缩后连续性恢复；
- Provider-specific Projection。

### 6.5 Tool Runtime

首版工具：

- `file.read`；
- `file.list`；
- `file.search`，底层使用 ripgrep；
- `file.apply_patch`；
- `shell.exec`；
- `process.write`；
- `process.poll`；
- `process.terminate`；
- `git.status`；
- `git.diff`。

所有工具使用统一结果协议：

```python
class ToolResult:
    status: ToolStatus
    content: list[ContentBlock]
    artifacts: list[ArtifactRef]
    exit_code: int | None
    truncated: bool
    retryable: bool
    metadata: dict
```

需要统一处理：

- 参数校验；
- 超时和取消；
- stdout、stderr 与退出码；
- 输出截断；
- 大结果转 Artifact；
- 错误分类；
- 副作用等级；
- 幂等性；
- ToolCall 审计。

### 6.6 Workspace Manager

职责：

- 定义工作区根目录；
- 路径正规化；
- 阻止 `../` 和符号链接逃逸；
- 处理 Ignore 规则；
- 跟踪文件修改；
- 获取 Git 状态和 Diff；
- 管理临时文件和 Artifact；
- 控制工作区并发。

默认并发规则：

```text
同一 Workspace 最多存在一个写 Run；
多个只读 Run 可以并行。
```

### 6.7 Local Policy 与 Approval

策略结果统一为：

```text
ALLOW：直接执行
DENY：拒绝，并向模型返回原因
ASK：暂停 Run，等待用户审批
```

首版权限模式：

- `READ_ONLY`；
- `WORKSPACE_WRITE`；
- `ASK_ON_RISK`；
- `FULL_ACCESS`。

默认风险规则：

- 读取工作区：允许；
- 修改工作区：根据模式决定；
- 工作区外写入：询问或拒绝；
- 递归删除：询问；
- 网络访问：询问；
- Git push：询问；
- 系统级安装：询问；
- Secret 输出：拒绝。

审批必须与完整的 ToolCall 参数绑定。参数变化、审批过期或 Run 变化后，旧审批自动失效。

### 6.8 SQLite Event Store

SQLite 是本地版本的默认存储，保存运行状态和可恢复事件。

建议表：

```text
sessions
runs
turns
run_events
messages
model_calls
tool_calls
approvals
checkpoints
artifacts
workspaces
provider_profiles
schema_migrations
```

建议配置：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
```

实现约束：

- 使用短事务；
- 不在数据库事务中等待模型或工具；
- 不持久化每个流式 Token；
- 持久化有恢复价值的关键事件；
- 使用异步写入队列串行化高频事件；
- 每次升级执行 Schema Migration；
- 提供导出和备份能力。

SQLite 保证数据库事务，但不能保证外部副作用 exactly-once。工具开始后、结果落库前发生崩溃时，状态必须设为 `TOOL_OUTCOME_UNKNOWN`，不得盲目自动重试。

### 6.9 Local Artifact Store

大型内容使用本地文件系统保存，SQLite 只记录元数据。

```text
用户数据目录/
├── qharness.db
├── config.toml
├── logs/
├── artifacts/
│   └── {run_id}/
└── cache/
```

Artifact 元数据包括：

- 相对路径；
- MIME 类型；
- 文件大小；
- SHA-256；
- 来源 Run 和 ToolCall；
- 创建时间；
- 保留策略。

### 6.10 配置与凭据

普通配置使用 TOML。API Key 不以明文写入 SQLite 或项目配置文件。

优先使用操作系统凭据存储：

- Windows Credential Manager；
- macOS Keychain；
- Linux Secret Service。

数据库中只保存 `credential_ref`。

## 7. 状态机

```text
CREATED
  → PREPARING
  → WAITING_MODEL
  → WAITING_TOOL
  → WAITING_APPROVAL
  → COMPACTING
  → COMPLETED

任意非终态还可能进入：
FAILED / CANCELLED / INTERRUPTED
```

建议停止原因：

```text
COMPLETED
USER_CANCELLED
APPROVAL_REQUIRED
TOKEN_BUDGET_EXCEEDED
COST_BUDGET_EXCEEDED
MAX_TURNS_EXCEEDED
MAX_TOOL_CALLS_EXCEEDED
TIME_BUDGET_EXCEEDED
NO_PROGRESS
POLICY_DENIED
MODEL_FAILURE
TOOL_FAILURE
```

## 8. 关键事件

```text
run.created
run.started
run.interrupted
run.completed
run.failed
run.cancelled

turn.started
turn.completed

context.compilation.started
context.compiled
context.compaction.started
context.compacted

model.requested
model.item.received
model.completed
model.failed

tool.proposed
tool.started
tool.output
tool.completed
tool.failed
tool.outcome_unknown

policy.decided
approval.requested
approval.responded

artifact.created
```

事件分为：

- Durable Event：影响恢复和审计，必须保存；
- Ephemeral Event：流式 Token 或临时进度，可只发送给当前客户端。

## 9. 启动恢复策略

应用启动时扫描所有非终态 Run：

```text
WAITING_MODEL
  → 判断请求是否已获得完整响应
  → 依据 Provider 能力重新发起或恢复

WAITING_APPROVAL
  → 恢复为等待状态

WAITING_TOOL
  → 如果工具可能有副作用，标记 TOOL_OUTCOME_UNKNOWN
  → 先检查环境，不自动重复执行

COMPACTING
  → 仅使用已完成的上一个 Checkpoint
  → 未完成压缩可以重新执行
```

长进程默认在 QHarness 进程退出时终止。未来如需跨客户端重启保留进程，再引入可选 Local Daemon。

## 10. 开源组件复用

| 能力 | 采用方案 | QHarness 自研部分 |
|---|---|---|
| 数据库 | SQLite | Schema、Event、恢复语义 |
| ORM/迁移 | SQLAlchemy、Alembic | Repository 与领域映射 |
| 数据校验 | Pydantic | Domain Model |
| HTTP | HTTPX | Model Backend |
| 模型 SDK | 厂商官方 SDK | Provider Adapter |
| 文件搜索 | ripgrep | 面向模型的 Search Tool |
| Git | git CLI | Git Tool Contract |
| Patch | git apply 或 unified diff 库 | Patch Tool Contract 与审计 |
| 凭据 | keyring | Credential 引用规则 |
| 日志 | logging 或 structlog | RunEvent 映射 |
| 工具扩展 | MCP SDK，后续 | MCP Adapter 与权限 |
| 沙箱 | Docker，可选 | Workspace 挂载和生命周期 |
| 测试 | pytest、pytest-asyncio | Kernel 和 Coding Eval |

第一阶段不应自研数据库、模型 HTTP 客户端、Git、代码搜索、容器运行时、MCP 协议或日志后端。

## 11. 推荐工程结构

```text
qharness/
├── domain/
│   ├── run.py
│   ├── events.py
│   ├── messages.py
│   ├── tools.py
│   └── policies.py
├── kernel/
│   ├── runtime.py
│   ├── state_machine.py
│   ├── reducer.py
│   ├── stopping.py
│   └── recovery.py
├── providers/
│   ├── base.py
│   └── openai_compatible.py
├── context/
│   ├── compiler.py
│   ├── budgeting.py
│   ├── compaction.py
│   └── instructions.py
├── tools/
│   ├── registry.py
│   ├── filesystem.py
│   ├── search.py
│   ├── shell.py
│   ├── process.py
│   ├── patch.py
│   └── git.py
├── workspace/
│   ├── manager.py
│   ├── paths.py
│   └── locking.py
├── policy/
│   ├── engine.py
│   ├── rules.py
│   └── approvals.py
├── storage/
│   ├── run_store.py
│   ├── event_store.py
│   ├── artifact_store.py
│   └── sqlite/
├── telemetry/
├── config/
├── cli/
└── tests/
```

## 12. 分期路线

### V0.1：最小闭环

实现：

- Domain Model；
- 内存 Event Store；
- Agent Kernel；
- Scripted Fake Model；
- Read、Search、ApplyPatch、Shell；
- 最大 Turn 和取消；
- 流式 RunEvent。

验收：

> 在不连接真实模型的情况下，通过脚本化模型完整测试“搜索代码、读取文件、修改文件、执行测试、返回结果”的多轮流程。

### V0.2：真实模型与持久化

实现：

- OpenAI-compatible Backend；
- SQLite Event Store；
- Run Snapshot；
- 本地 Artifact；
- Usage 统计；
- 启动恢复。

验收：

> 使用真实模型完成一个代码修改任务；进程在安全节点退出后可以继续运行。

### V0.3：上下文与可靠性

实现：

- Context Budget；
- Compaction；
- Provider 原生状态保存；
- Tool 幂等；
- `TOOL_OUTCOME_UNKNOWN`；
- 重复调用和无进展检测；
- 长 Shell Process。

验收：

> 长任务经过多轮工具调用和一次上下文压缩后，仍能保持目标、约束和已完成事项。

### V0.4：安全与扩展

实现：

- Local Policy；
- Approval；
- Workspace 隔离；
- 可选 Docker Sandbox；
- Hooks；
- MCP Adapter；
- 第二个原生 Model Backend。

验收：

> 危险操作无法绕过策略，审批可以跨进程恢复，模型不能访问未授权路径。

## 13. 测试策略

### 13.1 Kernel Conformance

- 状态转换；
- 取消；
- 模型断流；
- Tool 超时；
- 审批恢复；
- 数据库冲突；
- 崩溃恢复；
- 副作用未知；
- Context Overflow；
- Budget Stop。

### 13.2 Provider Conformance

使用同一组脚本验证所有 Backend：

- 流式文本；
- Tool Call；
- 多 Tool Call；
- Structured Output；
- Provider 错误；
- Context Overflow；
- Continuation；
- Usage。

### 13.3 Coding Task Eval

- 定位并修复单元测试；
- 小范围重构；
- 增加测试；
- 修改配置；
- 分析但不修改；
- 遵守只读模式；
- 拒绝危险操作；
- 压缩后继续任务。

## 14. 关键风险

1. **Provider 过度统一**：丢失 reasoning 或 continuation 状态；
2. **上下文与历史混用**：长任务成本和质量快速恶化；
3. **工具副作用重复**：崩溃后盲目重试造成二次修改；
4. **权限只写在 Prompt 中**：模型可以错误理解或忽略；
5. **同一 Workspace 并发写入**：多个 Run 相互覆盖；
6. **保存所有流式 Token**：SQLite 写放大且没有恢复价值；
7. **过早实现多 Agent**：放大单 Agent 尚未解决的恢复和上下文问题；
8. **过早插件化所有模块**：核心语义难以稳定和测试。

## 15. 后续扩展边界

核心接口预留：

```python
class HarnessInterface:
    async def run(
        self,
        request: RunRequest,
    ) -> AsyncIterator[RunEvent]: ...

    async def resume(
        self,
        run_id: str,
        input: ResumeInput,
    ) -> AsyncIterator[RunEvent]: ...

    async def cancel(self, run_id: str) -> None: ...
```

未来能力均通过适配层接入：

```text
A2A Message → RunRequest
RunEvent → A2A Task / Artifact

Desktop / IDE / CLI
→ HarnessInterface

SubAgent
→ 创建新的本地 Run
```

这些扩展不得要求修改 Agent Kernel 的基础状态语义。

## 16. 当前架构结论

QHarness 第一阶段采用以下组合：

```text
自研：
  Agent Kernel
  Domain Protocol
  Context Compiler
  Model Backend Adapter
  Tool Contract
  Workspace Manager
  Local Policy / Approval
  SQLite Event 与恢复语义
  Harness Eval

复用：
  SQLite
  SQLAlchemy / Alembic
  Pydantic
  HTTPX / 厂商 SDK
  ripgrep
  git
  keyring
  pytest
  Docker（可选）
  MCP SDK（后续）
```

第一阶段的核心目标不是堆积功能，而是让单 Agent 在本地代码仓库中做到：执行过程可控、修改有边界、状态可恢复、行为可审计、长任务不丢目标。
