# QHarness 后端推理引擎设计方案

> 文档状态：实现对齐版
> 基线版本：0.1.0
> 更新日期：2026-09-13
> 当前形态：本地客户端后端、单进程、可承载多个 Run 和工作区
> 本文范围：当前已经实现的模型调用、工具运行时、工作区、版本历史、沙箱、托管运行时和持久化
> 明确不包含：Agent Loop、Context Compiler、会话编排和前端 UI 的详细设计

## 1. 文档目的

本文以当前仓库代码为事实来源，说明 QHarness 在 Agent Loop 之前已经具备的后端能力、模块边界、安全约束和后续扩展口。

本文严格区分三类状态：

- 已实现：当前代码和示例已经具备；
- 已预留：已有抽象或上下文字段，但尚未形成完整业务能力；
- 未实现：仍属于后续规划，不能被上层当作现有保证。

Agent Loop 的研究依据和建议设计单独维护，见
[《Agent Loop 文献综述与 QHarness 设计建议》](./Agent-Loop文献综述与QHarness设计建议.md)。

## 2. 项目定位

QHarness 是运行在用户电脑上的 Coding Agent Harness 后端。它不是面向公网的多租户模型网关，也不是 Web 管理后台。

当前核心职责是：

- 通过 OpenAI-compatible 协议连接 DeepSeek 等模型；
- 向模型暴露结构化、可校验、可限制的工具；
- 把每次 Run 绑定到明确授权的工作区；
- 在 Anthropic Sandbox Runtime 中执行命令；
- 管理 QHarness 自带的 Python、Node 和 SRT；
- 对文件变更建立操作台账、私有版本历史和安全回滚能力；
- 使用应用级数据库持久化结构化业务数据；
- 为未来单进程多工作区、远程数据库和 Agent Loop 留出边界。

当前优先场景是本地单用户，但代码中的 tenant_id、workspace_id 和 run_id 已用于数据隔离与关联。它们不是已经完成的多租户鉴权系统。

## 3. 当前范围

### 3.1 已实现

- OpenAI-compatible Chat Completions 非流式与流式调用；
- DeepSeek reasoning_content 等推理字段适配；
- 文本、推理内容、工具调用和 Usage 的统一领域对象；
- TOML 模型、工具、沙箱、数据库和历史配置；
- 动态工具注册、Pydantic 或原始 JSON Schema 参数声明；
- 工具参数校验、次数限制、并发限制、超时、取消、结果截断和 Hook；
- 工作区路径守卫、目录分页、按行读取和 ripgrep 搜索；
- 全量写入、精确替换和多文件 Patch；
- SQLAlchemy 操作台账；
- Dulwich 私有 Commit、Diff、状态、文件历史和冲突安全回滚；
- Anthropic SRT 命令沙箱；
- Windows 上的 SRT 状态检查、自动准备和 UAC 初始化入口；
- QHarness 托管 Python 与 Node 运行时；
- 应用级 SQLite，以及通过 SQLAlchemy URL 切换数据库的基础；
- 统一日志和领域异常；
- 可直接运行的示例程序。

### 3.2 已预留但未形成完整产品能力

- RunContext 已具备 Run 级工作区、沙箱、计数、取消和元数据；
- ToolExecutionHook 已提供执行前允许或拒绝、成功后处理和异常处理入口；
- 工具策略已有 requires_approval 字段；
- WorkspaceHistoryRepository、FileVersionStore、ModelBackend 和 SandboxBackend 均为可替换边界；
- 数据表已按 tenant_id、workspace_id 和 run_id 建立关联；
- 数据库可以使用非 SQLite SQLAlchemy URL。

这些能力只是基础设施，不等于已经实现用户身份认证、租户授权、审批 UI、分布式调度、崩溃恢复或 Agent Loop。

### 3.3 暂未实现

- Agent Loop、Turn/Step 状态机和停止判定；
- Context Compiler、Token Budget 和自动压缩；
- 会话、消息、模型请求与响应持久化；
- Provider 原生 continuation 状态恢复；
- A2A、MCP、Skill 和子 Agent 调度；
- 图形界面和工作区授权弹窗；
- 工作区外路径的临时授权流程；
- 后台长驻 Shell Session 和软件安装专用工具；
- Java 等托管运行时；
- macOS、Linux 和非 x64 Windows 资源包；
- 跨进程或分布式工作区锁；
- 完整的操作崩溃恢复器。

## 4. 总体架构

当前代码的核心依赖关系如下：

~~~text
调用方或未来 Agent Loop
    ├── ModelBackend
    │     └── OpenAICompatibleBackend
    │              └── openai.AsyncOpenAI / Chat Completions
    └── RunContext
          ├── WorkspaceContext
          ├── SandboxBackend
          │     └── SrtSandboxBackend
          │            ├── RuntimeManager
          │            └── SrtPackageManager
          ├── ToolExecutionState
          └── WorkspaceMutationService（可选）
                  ├── WorkspaceHistoryRepository
                  │       └── SqlAlchemyWorkspaceHistoryRepository
                  └── FileVersionStore
                          └── DulwichFileVersionStore

ToolProvider
    ├── BuiltinToolProvider
    ├── FileMutationToolProvider
    └── SandboxToolProvider
           │
           ▼
      ToolRegistry
           │
           ▼
      ToolExecutor
~~~

当前没有一个内核自动把模型响应、工具调用结果和下一轮模型请求连接起来。示例或未来 Agent Loop 是这些模块的调用方。

## 5. 一次 Run 的基础装配

create_run_context 是当前标准装配入口，其流程为：

1. 接收 tenant_id、workspace_id、run_id 和已经授权的 workspace_root；
2. 使用 WorkspaceContext 规范化并固定工作区根目录；
3. 根据 SandboxConfig 创建绑定该工作区的 SandboxBackend；
4. 如果启用历史，初始化应用级 DatabaseManager；
5. 创建绑定 tenant_id 与 workspace_id 的 SQLAlchemy 操作台账仓储；
6. 创建对应逻辑工作区的 Dulwich 私有版本仓库；
7. 组合 WorkspaceMutationService；
8. 返回一次 Run 独享的 RunContext。

重要约束：

- tenant_id 和 workspace_id 只作为逻辑标识，绝不能直接拼接成磁盘路径；
- workspace_root 必须由未来的客户端授权层决定；
- 同一个进程可以创建多个 RunContext，不需要因工作区不同而启动多个 Harness 进程；
- 一个 RunContext 的工具计数和取消事件不会与其他 Run 共享；
- RuntimeManager 和 DatabaseManager 适合由应用进程复用。

## 6. 模型调用层

### 6.1 抽象接口

ModelBackend 定义 complete、stream 和 close。当前实现为 OpenAICompatibleBackend，底层使用 openai.AsyncOpenAI，而不是 LangChain。

选择 OpenAI-compatible 的原因是：

- DeepSeek 原生兼容该协议；
- 本地只需要一个轻量模型适配层；
- 不把 Harness 核心状态机绑定到大型编排框架；
- 后续可以增加其他 ModelBackend，而不改变工具和工作区模块。

### 6.2 请求与响应

ChatRequest 支持 system、developer、user、assistant、tool 消息，模型工具声明、tool_choice、response_format、temperature、max_tokens、thinking_mode、reasoning_effort、extra_body 和流式 Usage 选项。

统一响应保留 assistant 文本、reasoning 内容、工具调用、Finish Reason、Token Usage 和 Provider 原始响应。流式事件支持文本增量、推理内容增量、工具调用增量、Usage 更新和完成事件，并最终聚合成 ChatResponse。

### 6.3 异常与限制

模型异常会转换为 QHarness 领域异常，并区分连接失败、超时、限流和服务端异常等可重试场景。retries 是 SDK 级请求重试配置，未来 Agent Loop 不应在不了解幂等性时再无条件叠加业务重试。

当前限制：

- 使用 Chat Completions，不是 OpenAI Responses API；
- 尚未持久化完整模型请求、响应和流事件；
- 尚未实现故障切换、多模型路由和 continuation 恢复；
- API Key 当前来自 TOML，未接入操作系统凭据库。

## 7. 工具系统

### 7.1 Tool 定义与参数校验

一个 Tool 包含唯一名称、中文描述、参数模型或 JSON Schema、Handler 和可选元数据。

参数声明支持两种方式：

1. Pydantic BaseModel 子类；
2. 原始 JSON Schema 字典。

推荐新工具优先使用 Pydantic。注册时会生成 Draft 2020-12 JSON Schema；执行时只走与声明类型对应的一种校验，不会对同一参数重复校验两遍。Pydantic 校验后使用 Python 模式导出，允许 Handler 接收 Path、Enum 等本地对象。

### 7.2 动态注册

ToolProvider 根据当前 Run、工作区和本机能力动态产生工具集合。

| 提供器 | 工具 | 装载条件 |
| --- | --- | --- |
| BuiltinToolProvider | list_directory、read_file | 始终装载 |
| BuiltinToolProvider | search_text | 找到显式、随包或系统 ripgrep 时装载 |
| FileMutationToolProvider | write_file、replace_text、apply_patch、get_file_history、get_workspace_status、rollback_file_change | Run 启用了 WorkspaceMutationService |
| SandboxToolProvider | run_command | Run 已绑定 SandboxBackend |

ToolRegistry 校验工具名称与 Schema，并拒绝重复名称。工具不是写死在 Agent Loop 里的，未来可以继续增加 MCP、Skill 或应用插件提供器。

### 7.3 ToolExecutor 执行流程

一次工具调用按以下顺序执行：

1. 消耗本次 Run 的总工具调用计数；
2. 查找 Tool；
3. 消耗该工具自己的调用计数；
4. 解析 JSON 字符串或 Mapping；
5. 限制参数字符数；
6. 使用 Pydantic 或 JSON Schema 校验；
7. 执行 before_execute Hook；
8. 检查 requires_approval；
9. 获取全局和单工具并发许可；
10. 应用取消与超时；
11. 调用异步 Handler，或在线程池中调用同步 Handler；
12. 序列化和限制结果大小；
13. 执行 after_execute Hook；
14. 返回结构化 ToolExecutionResult。

未知工具、无效参数等失败尝试也会消耗调用次数，避免模型通过错误调用绕过预算。

### 7.4 限制模型

ToolExecutionPolicy 包含：

- max_total_calls：整个 Run 的工具总次数，默认不限制；
- max_concurrency：整个 Run 的工具并发数，默认不限制；
- defaults：所有工具的默认策略；
- tools.工具名：指定工具的覆盖项。

单工具策略支持 max_calls、max_concurrency、timeout_seconds、max_argument_chars、max_result_chars、truncate_oversized_results 和 requires_approval。没有单独配置的工具继承全局默认值。

### 7.5 Hook、审批与错误码

ToolExecutionHook 当前可以在执行前允许或拒绝、成功后观察结果、失败后观察异常。requires_approval 为 true 时，需要执行前 Hook 明确允许，否则调用会被拒绝。

当前尚无完整审批管理器、审批持久化或 UI 弹窗。因此 Hook 是扩展口，不是完整审批产品。

工具结果使用稳定错误码：

- unknown_tool；
- invalid_arguments；
- conflict；
- limit_exceeded；
- rejected；
- timeout；
- cancelled；
- execution_failed；
- hook_failed；
- result_too_large。

未来 Agent Loop 应按错误码决策，而不是解析中文错误文案。

### 7.6 同步 Handler 的取消限制

普通同步 Handler 在线程池中运行。asyncio 超时或取消能够停止等待，但不能强制终止已经运行的 Python 线程。有外部进程副作用的逻辑应使用 run_command，因为 SRT 后端可以终止整个进程树。

## 8. 内置只读工具

### 8.1 list_directory

list_directory 支持稳定的广度优先遍历、不区分大小写排序、递归、隐藏文件开关和最大 20 层深度。它不递归进入符号链接目录；指向工作区外部的链接只返回 blocked_symlink，不泄露目标。

分页采用服务端游标：

- page_size 范围 1 至 1000，默认 200；
- 游标是短随机标识，不是完整遍历状态的 Base64；
- 默认 30 分钟过期，每次继续读取刷新有效期；
- 单个 Provider 默认最多保存 1024 个游标；
- 查询条件变化、游标过期或进程重启后需要从头查询。

这种方式适合递归目录，因为不需要为了第 N 页重复扫描前 N 页。

### 8.2 read_file

read_file 按行读取 UTF-8 或 UTF-8 BOM 文本，返回带行号内容。它支持 start_line 和 max_lines，默认最多 200 行，单次最多 2000 行，并返回 has_more 和 next_start_line。

实现通过文件流逐行跳过与读取，不把整个文本一次性加载到内存；通过前部 NUL 采样拒绝疑似二进制文件。当前不负责图片、PDF 或任意二进制解析。

### 8.3 search_text

search_text 使用 ripgrep JSON 输出，支持字面量或正则、大小写、glob、隐藏文件和 ignore 开关，最大结果数为 1000，并将 UTF-8 字节偏移转换成模型容易理解的字符列。

ripgrep 查找顺序为：

1. 调用方显式路径；
2. QHarness 随包资源；
3. 系统 PATH。

当前发布资源只包含 Windows x64 的 rg.exe。其他平台以后需要加入对应资源或要求系统安装。

## 9. 文件修改工具

所有文件修改统一经过 WorkspaceMutationService，而不是各工具自行写磁盘。

### 9.1 write_file

- 创建或全量写入 UTF-8 文本；
- 父目录必须已经存在；
- 默认不覆盖现有文件；
- overwrite=true 时允许覆盖；
- expected_sha256 可阻止基于旧内容覆盖新版本；
- 单文件默认上限为 2 MiB。

### 9.2 replace_text

- 对 UTF-8 文件执行精确文本替换；
- old_text 必须非空；
- expected_replacements 默认等于 1；
- 实际匹配数不一致时拒绝修改，并把实际数量告诉模型；
- 模型要替换全部已知匹配时，应显式传入对应数量；
- 支持 expected_sha256 乐观并发校验。

这比按模糊行号修改更可控，但模型在文件变化后必须重新读取。

### 9.3 apply_patch

apply_patch 接受 QHarness 的结构化 Patch 文本，一次可执行 Add、Update 和 Delete 多个文件。Update 使用上下文块定位，拒绝找不到或存在歧义的匹配。

多文件 Patch 作为一次工作区操作和一次私有 Commit 处理；发生中途落盘异常时，会逆序补偿已经写入的文件。它不要求模型输出整个文件，也不依赖用户机器上的 patch 命令。

## 10. 工作区与路径安全

### 10.1 WorkspaceContext

WorkspaceContext 持有本次 Run 已授权的绝对根目录，并通过 WorkspacePathGuard 解析所有工具路径。

核心规则：

- 工具对模型暴露相对路径；
- 真正访问前恢复为工作区下的绝对路径；
- 解析 .. 与符号链接后的最终路径必须仍位于根目录；
- 不允许用路径穿越逃出工作区；
- Windows 下拒绝 ADS 冒号、保留设备名、尾随点或空格及空字符；
- 返回给模型的路径统一使用 POSIX 风格分隔符。

相对路径只是安全、简洁的外部表示，不会影响执行时找到真实文件。

### 10.2 工作区外文件

当前文件工具不能读写工作区外文件，这是有意的安全边界。

未来 UI 可以在捕获 WorkspacePathError 后，展示规范化路径，让用户选择允许一次、允许本次 Run、加入长期授权或拒绝，然后创建新的授权根或受限挂载并重试。该授权服务尚未实现，当前不能绕过路径守卫。

### 10.3 保留目录

文件修改服务拒绝修改工作区中的 .git 和 .qharness。私有历史仓库也不存放在用户工作区中，避免污染用户自己的 Git 仓库。

## 11. 文件变更、历史和回滚

### 11.1 两类存储

SQLAlchemy 数据库保存操作台账：

- 工作区、Run、工具和路径；
- 操作来源；
- pending、applied、failed 或 reverted 状态；
- base_commit_id、commit_id 和回滚关联；
- 简短错误信息。

Dulwich 私有仓库保存版本事实：

- Blob、Tree、Commit 和 HEAD；
- 实时 Diff；
- 文件历史；
- 当前磁盘相对私有 HEAD 的状态。

数据库不重复保存完整文件内容和 Diff。需要展示 Diff 时，根据 Commit 指针从 Dulwich 实时计算。

### 11.2 私有仓库

每个 tenant_id 与 workspace_id 对应独立命名空间，历史根目录下保存 bare Git 对象仓库，使用专用引用：

    refs/qharness/workspace-head

它与用户工作区里的 .git 完全分离，用户不需要手动执行 git add 或 git commit。

默认不跟踪 .git、.qharness、.venv、venv、node_modules、__pycache__、pyc、pytest/mypy/ruff 缓存，并尊重工作区 .gitignore。

### 11.3 文件工具修改流程

1. 获取进程内工作区锁；
2. 拒绝与同工作区正在运行的外部命令交错；
3. 检查磁盘相对私有 HEAD 的外部修改并建立检查点；
4. 校验路径、内容、文件大小和 expected_sha256；
5. 在数据库创建 pending 操作；
6. 使用同目录临时文件、flush、fsync 和 os.replace 原子替换单文件；
7. 多文件操作按计划落盘；
8. 在 Dulwich 中基于预期 HEAD 创建 Commit；
9. 在数据库把操作确认成 applied；
10. 返回 operation_id、Commit 和文件变化。

落盘中途失败时会逆序恢复已经修改的文件，并尽力把台账标为 failed。数据库事务、文件系统操作和 Git 对象写入不是一个分布式事务，因此当前实现是补偿式一致性，不应描述为绝对原子。

### 11.4 外部修改与命令修改

用户或 IDE 直接修改文件时，数据库不会即时收到事件。在下一次 QHarness 修改前，服务会比较磁盘与私有 HEAD，把变化创建为 origin=external、tool_name=external_checkpoint 的 Commit 和台账。

run_command 执行前建立基线并阻止同进程文件工具交错；结束后扫描全部可跟踪变化。即使命令退出码非 0，也会保存已经发生的变化，并把 validation_status 标记为 failed，因为失败退出不代表没有副作用。

### 11.5 历史工具与安全回滚

当前给模型注册：

- get_file_history：从 Dulwich Commit 链读取文件历史；
- get_workspace_status：比较磁盘与私有 HEAD，不创建检查点；
- rollback_file_change：安全反向应用指定 operation_id。

没有注册仅供 UI 展示的 inspect_file_change 工具。服务内部仍可按 Commit 指针计算操作 Diff。

回滚只处理原操作涉及的路径，并检查文件当前摘要是否仍等于该操作完成后的摘要。如果用户或后续 Agent 已再次修改目标文件，回滚返回 conflict。成功回滚会创建新 Commit 和新 rollback 操作，而不是删除历史。

## 12. 沙箱与命令执行

### 12.1 抽象接口

SandboxBackend 定义：

- check_status：只读检查；
- prepare：自动准备用户态依赖；
- setup：执行需要用户确认或系统初始化的步骤；
- execute：执行一次命令。

当前实现是 SrtSandboxBackend。

### 12.2 run_command 的职责

run_command 是模型调用沙箱的工具入口。模型传入完整 Shell 命令，因此支持普通 CLI、管道、重定向、条件执行、环境变量、Python、Node、其他已授权可执行文件、可选 stdin 和可选工作目录。

沙箱本身是执行后端，不会自动成为模型工具。run_command 负责参数 Schema、Run 关联、工具限制、结果结构化和文件变更记录。

Windows 上通过 PowerShell 执行命令。复杂 JavaScript 或 Python 代码仍建议先写入文件，再执行脚本，以减少多层 Shell 引号问题。

### 12.3 文件系统与网络策略

SRT 设置由 QHarness 在每次执行前动态生成。路径策略相对于本次 workspace 解析，也可包含明确绝对路径。

配置支持：

- allow_read；
- deny_read；
- allow_write；
- deny_write；
- allowed_domains；
- denied_domains；
- allow_local_binding。

allow 与 deny 不是简单二选一。SRT 使用它们生成对工作区、运行时和明确敏感路径的许可与拒绝规则。

当前默认网络域名为空、禁止本地端口监听。run_command 能否联网由沙箱网络策略决定，不由 Shell 语法决定。

### 12.4 执行控制

SRT 执行时：

- stdout 与 stderr 并发持续读取，避免任一管道写满造成死锁；
- stdin 由独立异步任务写入并关闭；
- 同时等待进程退出、输出超限、Run 取消和超时；
- 任一输出超过限制时终止进程树；
- Windows 使用新进程组和 taskkill /T /F；
- Linux/macOS 代码路径使用新 Session 和进程组，但当前尚无对应托管资源验证；
- 返回退出码、耗时、超时、取消、输出截断和 Run/Operation 标识。

每次并发调用会启动独立的 SRT 和目标进程，不共享一个可变 Shell Session。

### 12.5 SRT 调试日志

SRT 调试信息与目标进程 stderr 来自同一管道。QHarness 会把 SRT 调试行写入自身 DEBUG 日志、隐藏命令内容，并在成功且开启 debug 时清空返回给模型的调试型 stderr，以减少 Token 浪费；失败时保留 stderr。

当前限制是：成功场景下，如果目标程序主动向 stderr 输出警告，它可能和 SRT 调试内容一起被清空。后续可通过更可靠的日志分流改进。

### 12.6 Windows 初始化

SRT 的 npm 包和 srt-win.exe 文件存在，只表示程序文件已安装。Windows 隔离账户、权限和网络过滤基础设施还需要执行一次：

    srt-win.exe install

setup 会通过原生 Windows 确认框提示用户，并使用 UAC 提权启动安装命令。应用不能在用户不知情时静默取得管理员权限。

杀毒或终端防护软件可能阻止 srt-win.exe，表现为 WinError 5。QHarness 不应自动关闭或绕过安全软件。

### 12.7 不降级原则

SRT 未准备好时，run_command 应明确失败，不允许静默切换到无沙箱 subprocess。版本控制能够恢复文件，但不能替代沙箱，因为命令还可能访问工作区外文件、网络、注册表或系统进程。

## 13. 托管运行时

### 13.1 RuntimeManager

RuntimeManager 管理 QHarness 私有的 Python 和 Node：

- check_status：只读检查清单和已安装状态，不下载、不解压；
- ensure：缺失时校验、下载或安装，返回可执行文件绝对路径；
- resolve_executable：把受支持的裸名称解析为托管运行时。

当前识别 python、python3、node 及其 Windows 可执行文件名。显式路径和其他命令保持原样。managed 缺失时准备固定版本，失败就明确报错，不会静默使用系统 PATH。

### 13.2 当前版本与来源

| 组件 | 当前版本 | 交付方式 |
| --- | --- | --- |
| Python | 3.13.15 | Windows x64 NuGet 归档随 QHarness 包分发 |
| Node | 24.21.0 | 固定官方 HTTPS 地址按需下载 |
| Anthropic SRT | 0.0.74 | 使用锁定的 package-lock.json 安装 |
| ripgrep | 资源 VERSION 文件记录 | Windows x64 可执行文件随包分发 |

所有运行时归档在安装前校验固定 SHA-256。下载型归档还校验预期字节数，并通过临时目录安装后原子发布完成标记，避免中断安装留下半成品。

### 13.3 managed 与 custom

默认不要求用户在配置里写运行时路径：

- 未配置路径：使用 managed；
- managed 缺失：自动准备；
- managed 准备失败：明确报错。

用户明确配置 python_path、node_path 或 package_path 时：

- 使用指定资源；
- 校验失败时明确报错；
- 不静默回退到 managed。

SRT 自身使用的 Node 和沙箱命令中的 node 都可以来自 QHarness 托管运行时。沙箱环境会把托管 Python、Node 的目录放到 PATH 前部，因此 Agent 可以写 python 或 node，不需要知道绝对路径。

### 13.4 其他语言

java、go、rustc 等不属于当前托管运行时。run_command 仍可执行这些命令，但前提是用户机器已安装、Shell PATH 能找到、SRT 文件系统策略允许读取安装目录，且相关写入与网络行为未被策略拒绝。

后续可按 RuntimeName 增加固定版本、平台清单、摘要和安装规则。

## 14. 持久化设计

### 14.1 应用级数据库

DatabaseManager 为整个 QHarness 进程创建一个 SQLAlchemy Engine 和 SessionFactory。业务仓储只接收 SessionFactory，不自行读取配置、创建 Engine 或执行迁移。

这意味着：

- SQLite 使用一个应用数据库文件，不是每个工作区一个文件；
- 多个工作区通过 tenant_id 和 workspace_id 字段隔离；
- 以后切换 MySQL 时主要修改 database.url 和驱动依赖；
- 业务代码不需要方言专用的 SqliteWorkspaceHistoryRepository。

当前默认 URL 为：

    sqlite+pysqlite:///../.qharness/qharness.sqlite3

相对 SQLite 路径按 database.toml 所在目录解析。

### 14.2 SQLite 策略

当前会为 SQLite：

- 创建数据库父目录；
- 设置 check_same_thread=false；
- 启用 WAL；
- 启用外键；
- 设置 busy_timeout；
- 使用 synchronous=NORMAL。

SQLite 适合本地客户端和单进程早期版本。

### 14.3 数据库切换能力

SQLAlchemy 仓储不依赖 SQLite 方言。项目已经直接依赖 PyMySQL，因此 MySQL URL 可在现有依赖下使用。

PostgreSQL 在 ORM 设计上可以支持，但当前 pyproject.toml 没有安装 PostgreSQL 驱动，不能仅修改 URL 就视为已交付支持。

数据库迁移统一由 Alembic 执行，并复用 DatabaseManager 的现有连接，避免迁移器另读一套 URL。

### 14.4 当前表

| 表 | 用途 |
| --- | --- |
| workspace_bindings | 固定 tenant_id + workspace_id 与真实工作区根目录的绑定 |
| workspace_operations | 保存文件工具、外部检查点和回滚操作台账 |
| workspace_operation_files | 保存一次操作涉及的相对路径索引 |
| alembic_version | Alembic 迁移版本 |

workspace_bindings 会比较规范化根目录摘要。同一 tenant_id 与 workspace_id 试图绑定另一个物理目录时会被拒绝，避免历史串用。

### 14.5 尚未进入数据库的业务

当前还没有 sessions、runs、turns、messages、model_requests、model_responses、tool_calls、approvals、artifacts 和 checkpoints 等表。

这些表应在 Agent Loop 和会话模型确定后再设计，避免当前工作区操作表反向绑死未来领域模型。

## 15. 存储布局

本地开发默认布局：

~~~text
D:\QHarness
├── config
│   ├── model.toml
│   ├── tool.toml
│   ├── sandbox.toml
│   ├── database.toml
│   └── history.toml
├── .qharness
│   ├── qharness.sqlite3
│   ├── history
│   │   └── 按 tenant/workspace 摘要隔离的 objects.git
│   ├── runtime
│   │   ├── python
│   │   ├── node
│   │   ├── srt
│   │   └── sandbox
│   └── workspaces
└── src
    └── qharness
~~~

工作区根目录不是进程级全局常量。未来客户端可以在 .qharness/workspaces 下创建托管工作区，也可以把用户明确授权的其他目录传给 create_run_context。

## 16. 配置设计

### 16.1 配置文件

| 文件 | 作用 |
| --- | --- |
| model.toml | Provider、Base URL、API Key、模型参数和推理选项 |
| tool.toml | Run 级与单工具限制 |
| sandbox.toml | SRT、文件系统、网络和运行时选择 |
| database.toml | SQLAlchemy URL 和连接策略 |
| history.toml | Dulwich 私有历史根目录 |

配置路径中的相对路径统一相对于对应 TOML 文件解析。公共 TOML 工具负责读取文档和类型、校验正数并拒绝未知字段。

未知配置键直接报错，而不是只记录警告。安全策略或资源路径拼错后继续运行，比启动失败更危险。

### 16.2 配置优先级与凭据

当前只有配置文件，不使用环境变量覆盖。这符合本地客户端可见、可编辑和行为可预测的目标。

model.toml 中的 API Key 是明文凭据。最低要求是：

- 不提交到 Git；
- 不打进安装包；
- 限制文件 ACL；
- 日志与异常不得输出完整 Key；
- 示例只能使用占位符。

正式客户端建议把 Key 存入 Windows Credential Manager、macOS Keychain 或 Linux Secret Service，TOML 只保存凭据引用。该能力尚未实现。

## 17. 日志与异常

### 17.1 日志

项目使用 Python 标准 logging：

- 示例与业务代码不使用 print；
- 控制台和文件日志使用 UTF-8；
- 业务状态用 INFO；
- SRT 原生日志用 DEBUG；
- 失败诊断用 ERROR 或 WARNING；
- 命令等敏感内容在 SRT 调试日志中隐藏。

未来应增加 run_id、operation_id、tenant_id 和 workspace_id 的结构化上下文字段，而不是依赖文案检索。

### 17.2 异常

领域异常统一放在 qharness.exception 中，按模型、工具、工作区、沙箱、运行时、数据库和配置分类。底层依赖异常在模块边界转换为领域异常，避免未来 Agent Loop 直接依赖 openai、SQLAlchemy、Dulwich 或 subprocess 的异常类型。

## 18. 并发、一致性与取消

### 18.1 当前并发边界

- RunContext 的工具计数和 asyncio.Event 按 Run 隔离；
- ToolExecutor 支持 Run 全局与单工具 Semaphore；
- WorkspaceMutationService 使用规范化工作区根目录对应的进程内 RLock；
- SQLAlchemy 台账仓储按逻辑工作区使用进程内 RLock；
- run_command 活跃期间，进程内文件工具不能修改同一工作区；
- Dulwich HEAD 更新使用预期基线检测冲突。

### 18.2 不能承诺的能力

当前锁都是进程内锁，因此不能保证两个 QHarness 进程修改同一工作区时互斥、多台机器共享工作区时互斥、多个进程推进同一私有 HEAD 时完全无竞争，也不能让数据库与文件系统具备跨资源 ACID。

未来多进程或分布式部署必须增加数据库租约、文件锁或单工作区调度所有权。

### 18.3 取消语义

RunContext.cancel 会设置本次 Run 的 asyncio.Event，工具请求自动携带该事件。

- 尚未执行的工具应尽快拒绝；
- 异步工具在等待点响应取消；
- run_command 会终止进程树；
- 普通同步线程无法被 Python 安全强杀，只能停止等待其结果。

取消是协作式信号，不等于所有副作用都可撤销。文件变更仍需通过版本历史检查和回滚。

## 19. 安全模型

| 层 | 主要责任 |
| --- | --- |
| WorkspacePathGuard | 阻止路径穿越和工作区外访问 |
| 文件工具 | 限制 UTF-8、大小、覆盖、替换次数和旧版本摘要 |
| ToolExecutor | 限制参数、次数、并发、超时、取消和返回长度 |
| Hook | 在执行前做策略允许或拒绝 |
| SRT | 约束外部进程的文件系统与网络 |
| RuntimeManager | 固定来源、版本、大小和 SHA-256 |
| Dulwich | 保存变更事实、冲突检测和可追溯回滚 |
| SQLAlchemy 台账 | 记录操作归属、状态与 Commit 关联 |

需要明确：

- 沙箱是主要执行边界，版本控制不是沙箱替代品；
- 私有历史用于恢复文件，无法撤销所有系统级副作用；
- requires_approval 尚未形成完整人工确认链；
- SRT 当前仍属于需要持续验证的外部安全组件；
- 当前代码不做无沙箱自动降级。

## 20. 扩展接口

当前适合扩展的稳定边界：

- ModelBackend：新增模型 Provider；
- ToolProvider：新增动态工具来源；
- ToolExecutionHook：新增策略、审批、审计和遥测；
- SandboxBackend：新增本地或远程沙箱；
- RuntimeManager 清单：新增语言与平台运行时；
- WorkspaceHistoryRepository：替换操作台账存储；
- FileVersionStore：替换版本事实存储；
- SQLAlchemy SessionFactory：切换数据库；
- RunContext.metadata：携带非安全关键的上层调度信息。

未来 A2A 应作为 Harness 外部输入输出适配层，不应让 A2A 对象渗透到 ModelBackend、ToolExecutor 或 WorkspaceMutationService 内部。

## 21. 发布与平台现状

### 21.1 Python 项目交付

开发阶段可以源代码运行。正式 Windows 客户端可以使用 PyInstaller、Nuitka 等方式构建 exe，用户无需拉取 GitHub 源码或自行安装项目 Python。

无论最终打包为何种形式，运行时资源都应继续通过 package-data 或应用资源清单明确纳入，不能扫描开发机目录自动打包。

### 21.2 当前资源矩阵

| 平台 | QHarness Python 包 | 托管 Python | 托管 Node | SRT | ripgrep |
| --- | --- | --- | --- | --- | --- |
| Windows x64 | 当前开发目标 | 已实现 | 已实现 | 已实现并验证初始化流程 | 已随包 |
| macOS | Python 代码可能可运行 | 未提供 | 未提供 | 未集成验证 | 未提供 |
| Linux | Python 代码可能可运行 | 未提供 | 未提供 | 未集成验证 | 未提供 |

当前不能宣称跨平台开箱即用。后续应为每个平台维护独立清单、下载源、摘要、许可文件和 CI 验证。

## 22. 开源组件复用

| 能力 | 当前组件 | QHarness 自己负责的部分 |
| --- | --- | --- |
| OpenAI-compatible 调用 | openai | 领域模型、DeepSeek 适配、流聚合、异常语义 |
| 参数模型 | Pydantic | Tool 定义、调用状态和业务错误 |
| 原始 Schema 校验 | jsonschema | Schema 选择和错误转换 |
| ORM 与连接池 | SQLAlchemy | 应用数据库边界和业务仓储 |
| 数据迁移 | Alembic | 迁移生命周期和表设计 |
| 私有 Git 对象 | Dulwich | 工作区快照、操作关联、冲突回滚 |
| 命令隔离 | Anthropic SRT | 准备、配置、UAC、进程控制和结果清洗 |
| 文本搜索 | ripgrep | 资源发现、JSON 解析和工作区约束 |
| TOML | Python tomllib | 严格配置读取与类型错误 |
| 日志 | Python logging | 日志规范、编码和敏感信息处理 |

核心原则是复用成熟的协议、ORM、Git 对象库和沙箱，不把 QHarness 的领域边界外包给通用 Agent 框架。

## 23. 示例与验收入口

| 编号 | 主要验证内容 |
| --- | --- |
| 01 | 基础模型调用 |
| 02 | 流式模型调用 |
| 03 | 模型工具调用 |
| 04 | 推理内容与工具调用 |
| 05 | 工具执行限制 |
| 06 | 工作区路径守卫 |
| 07 | 内置只读工具 |
| 08 | SRT 沙箱 |
| 09 | 托管运行时 |
| 10 | Node 沙箱执行 |
| 11 | run_command 与工作区变化 |
| 12 | 文件修改与历史 |
| 13 | 回滚冲突 |
| 14 | apply_patch |

这些是 main 方法式的人工验收程序，不是完整自动化测试套件。正式发布前仍需补充单元测试、Provider 契约测试、ToolExecutor 并发与取消测试、路径穿越与符号链接安全测试，以及文件补偿失败测试。

## 24. 下一阶段建议

在进入 Agent Loop 前，建议先把当前基础设施验收稳定：

1. 为模型、工具、路径、沙箱、运行时和持久化补自动化测试；
2. 明确成功时目标 stderr 与 SRT debug 日志的分流方案；
3. 为 API Key 接入系统凭据存储；
4. 为 Windows 打包流程加入资源完整性检查；
5. 定义跨进程工作区所有权策略；
6. 设计 UI 工作区授权与 SRT UAC 交互契约；
7. 明确外部修改检查点和 pending 台账的启动恢复策略。

完成这些后，再依据文献综述实现 Agent Loop。Loop 应只编排现有 ModelBackend、ToolExecutor 和 RunContext，不把模型协议、文件写入或沙箱细节重新塞进循环内部。

## 25. 当前架构结论

当前 QHarness 已经形成 Agent Loop 之下较完整的执行底座：

- 模型层使用轻量 OpenAI-compatible 适配，而非引入 LangChain；
- 工具层具备动态发现、参数校验、限制、Hook 和稳定错误；
- RunContext 提供单进程多 Run、多工作区的依赖边界；
- 工作区工具通过路径守卫控制访问范围；
- 文件修改统一进入操作台账和 Dulwich 私有历史；
- Shell 命令通过 SRT 沙箱执行，并记录实际文件副作用；
- Python、Node、SRT 和 ripgrep 具备托管或随包资源路径；
- 数据库已经从文件功能中抽成应用级 SQLAlchemy 基础设施；
- SQLite 是当前本地默认值，但业务仓储不绑定 SQLite。

当前仍不是完整 Agent 产品。它缺少的主要是上层推理循环、上下文管理、会话持久化、恢复、审批产品和 UI，而不是再造一套模型 SDK、Git、ORM 或沙箱。
