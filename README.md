# QHarness

QHarness 是一个本地优先的 Coding Agent Harness。当前首先实现模型调用层，默认通过 OpenAI Python SDK 调用 DeepSeek 的 OpenAI-compatible Chat Completions API。

## 环境准备

```powershell
.\.venv\Scripts\python.exe -m ensurepip --upgrade
.\.venv\Scripts\python.exe -m pip install -e .
```

首次使用时，如果本地配置不存在，复制配置模板：

```powershell
Copy-Item .\config\model.example.toml .\config\model.toml
Copy-Item .\config\database.example.toml .\config\database.toml
Copy-Item .\config\tool.example.toml .\config\tool.toml
Copy-Item .\config\sandbox.example.toml .\config\sandbox.toml
Copy-Item .\config\history.example.toml .\config\history.toml
```

然后编辑 `config/model.toml`，直接填写模型、API 地址和 DeepSeek API Key：

```toml
[model]
provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key = "替换成你的 API Key"
model = "deepseek-v4-flash"
```

`config/model.toml` 已加入 `.gitignore`，不会作为普通未跟踪文件提交。仍然不要主动分享或上传包含真实密钥的文件。

## 动态配置

模型配置位于 `config/model.toml`，应用级数据库配置位于 `config/database.toml`，工具执行策略位于 `config/tool.toml`，沙箱策略位于 `config/sandbox.toml`，Dulwich 工作区历史配置位于 `config/history.toml`。程序每次启动都会重新读取配置文件，不使用环境变量覆盖。

工具策略分为三层：`[tool_execution]` 保存整个 Run 的总调用数和总并发限制；`[tool_execution.defaults]` 保存所有工具的默认策略；`[tool_execution.tools.<工具名>]` 只覆盖指定工具的字段。没有具名配置的工具自动继承默认策略。

全局 `max_total_calls` 和 `max_concurrency` 默认不限制；只有在 `[tool_execution]` 中显式配置正整数后才会启用对应限制。

可提交到仓库的模板位于 [config/model.example.toml](config/model.example.toml)、[config/database.example.toml](config/database.example.toml)、[config/tool.example.toml](config/tool.example.toml)、[config/sandbox.example.toml](config/sandbox.example.toml) 和 [config/history.example.toml](config/history.example.toml)。

## 工具参数

本地 Python 工具可以通过 Pydantic `BaseModel` 子类声明参数，QHarness 会自动生成 JSON Schema 并在执行前校验参数。MCP、A2A 等动态工具也可以直接传入原始 JSON Schema 字典；两种形式可以同时注册和执行。

## 直接运行示例

```powershell
.\.venv\Scripts\python.exe .\examples\01_basic_chat.py
.\.venv\Scripts\python.exe .\examples\02_stream_chat.py
.\.venv\Scripts\python.exe .\examples\03_tool_call.py
.\.venv\Scripts\python.exe .\examples\04_thinking_tool_call.py
.\.venv\Scripts\python.exe .\examples\05_tool_runtime.py
.\.venv\Scripts\python.exe .\examples\06_workspace.py
.\.venv\Scripts\python.exe .\examples\07_builtin_tools.py
.\.venv\Scripts\python.exe .\examples\08_srt_sandbox.py
.\.venv\Scripts\python.exe .\examples\09_runtime_manager.py
.\.venv\Scripts\python.exe .\examples\10_node_sandbox.py
.\.venv\Scripts\python.exe .\examples\11_run_command_tool.py
.\.venv\Scripts\python.exe .\examples\12_file_mutation_history.py
.\.venv\Scripts\python.exe .\examples\13_file_rollback_conflict.py
.\.venv\Scripts\python.exe .\examples\14_apply_patch.py
```

示例用途：

1. 普通非流式调用；
2. 流式文本与 Usage；
3. 普通模式工具调用闭环；
4. DeepSeek 思考模式工具调用及 `reasoning_content` 回填。
5. 工具注册、参数校验、Hook 与执行次数限制。
6. 工作区初始化、安全路径解析与越界访问拦截。
7. 通过 `BuiltinToolProvider` 动态加载并执行工作区只读工具。
8. 检查 Anthropic SRT 状态，并在可用时执行一个受隔离的 Python 进程。
9. 检查 RuntimeManager 状态，并验证托管 Python、Node 的安装和名称解析。
10. 使用逻辑名称 `node` 在 SRT 沙箱中执行 JavaScript。
11. 创建 RunContext，通过 `run_command` 执行带管道的命令，并记录文件变化。
12. 演示文件完整写入、精确替换、Diff、历史查询和安全回滚。
13. 模拟用户后续编辑，验证回滚不会覆盖较新的文件内容。
14. 一次补丁修改多个文件，查询 Dulwich Commit 历史并整体回滚。

## Anthropic SRT 沙箱

QHarness 使用统一的 `SandboxBackend` 接口执行脚本和 CLI，当前实现为 `SrtSandboxBackend`。文件读取和精确修改仍优先使用 QHarness 自己的工作区工具；外部命令统一进入沙箱。`SandboxExecutionRequest.command` 接收完整 Shell 命令，因此模型可以使用管道、重定向、变量和条件语法。QHarness 本身仍以固定 argv 启动 SRT，不用宿主 Shell 解释模型文本；模型命令只由 SRT 隔离账户内的 Shell 解释。Windows 固定使用 PowerShell，Linux/macOS 使用 SRT 的 Bash 命令模式。

项目随包携带独立的 CPython 运行时。默认调用 `sandbox.prepare()` 时会校验资源包 SHA-256，并原子解压到 `.qharness/runtime/python`；同版本后续调用直接复用。执行时 QHarness 把选定 Python 的目录放到沙箱 PATH 最前面，因此 Agent 在命令中直接写 `python script.py` 即可。只有用户在 `[sandbox.srt]` 中明确配置 `python_path` 时才使用该解释器；路径不可用会明确失败，不会静默回退。

Node 使用同一个 `RuntimeManager` 管理。默认从清单固定的 Node.js 官方 HTTPS 地址下载 `24.21.0`，校验文件大小和 SHA-256 后原子安装到 `.qharness/runtime/node`；Agent 在命令中直接写 `node script.js` 即可。SRT CLI 和沙箱内命令默认共同使用该托管版本。只有用户在 `[sandbox.srt]` 中明确配置 `node_path` 时才改用用户指定的 Node；路径不可用或未附带 npm 时会明确失败，不会静默回退。

SRT npm 包也无需人工运行 npm。`sandbox.prepare()` 会通过托管 Node 附带的 npm，按照应用内置的 `package-lock.json` 下载并安装精确版本 `0.0.74` 到 `.qharness/runtime/srt/packages`。安装时禁用生命周期脚本，并验证包名、版本和 CLI 后再原子发布。用户明确配置 `package_path` 时则使用自定义包。

Windows 版 SRT 还需要一次系统初始化，它会创建专用的 `srt-sandbox` 本地账户和 WFP 网络规则。`sandbox.check_status()` 始终只读；客户端发现 `setup_required=true` 后调用 `sandbox.setup()`，QHarness 会先显示原生说明窗口，用户点击“是”后再由 Windows 显示 UAC，并以提权方式运行固定的 `srt-win.exe install`，不再要求用户复制命令。运行 `examples/08_srt_sandbox.py` 可以直接验证完整流程。该示例使用 `.qharness/workspaces` 作为开发期托管工作区，不会把整个 QHarness 源码仓库授权给沙箱进程。SRT 当前仍是 Anthropic 的 Research Preview，Windows 支持为 Alpha，因此升级版本时需要重新验证策略语义。

默认策略禁止网络访问，只允许写入当前托管工作区，并保护工作区内的 `.env` 和整个 `.git` 目录。SRT 的读取策略会继续沿用 Windows 原有 ACL；不要通过拒绝整个 Windows 用户主目录来模拟读取白名单，SRT 0.0.74 在该路径上可能发生 ACL 超时。标准输出、标准错误、执行时间均有上限；超时、主动取消或输出超限时会终止整个进程树。开启 SRT DEBUG 后，原生日志实时写入 QHarness 日志；命令成功时不会把它填到模型可见的 `stderr`，命令失败时才保留完整诊断。`run_command` 执行结束后会比较完整工作区；有可跟踪变化时，将它们保存为一个私有 Commit，并让工作区操作记录复用本次沙箱请求的 `operation_id`。即使命令失败，已经落盘的变化也会记录下来并标记验证失败。

## 工作区边界

`WorkspaceContext` 由客户端在每次 Agent Run 开始时根据用户选择的目录创建。所有文件工具都必须通过它解析路径，以阻止 `..`、绝对路径和符号链接逃出工作区。工作区边界不负责隔离 Shell 或外部代码；代码执行仍需单独经过 Sandbox 层。

`RunContext` 将 `tenant_id`、`workspace_id`、`run_id`、`WorkspaceContext`、`SandboxBackend`、工具调用计数和取消事件绑定到一次运行。它不是进程级单例；同一个 Harness 进程可以同时创建多个 RunContext。`workspace_root` 由完成租户授权的上层动态传入，不从租户标识拼接，也不写进全局配置。语言运行时可以跨 Run 复用，工作区、沙箱实例、工具注册表、ToolExecutor 和 ToolExecutionState 必须按 Run 隔离。

传入 `history_config` 创建 RunContext 时，会同时创建 `WorkspaceMutationService`。数据库基础设施已经独立到 `qharness.persistence`：应用启动时根据 `[database].url` 创建一个 `DatabaseManager`，并把同一个 SessionFactory 交给各功能仓储。桌面端默认只使用一个 `qharness.sqlite3`，所有租户和工作区共享表结构并通过 `tenant_id`、`workspace_id` 逻辑隔离。以后切换 MySQL 或 PostgreSQL 只需更换 SQLAlchemy URL 和相应驱动，RunContext、业务服务和仓储调用方式不变。数据库结构由应用级 Alembic revision 统一升级，业务仓储不建库、不建表，也不读取配置。

Dulwich 与操作数据库的存储方式不同：数据库是全局共用的一个库，Dulwich 私有裸仓库仍按租户和工作区使用固定长度摘要子目录，因为每个工作区需要独立的 HEAD、Tree 和 Commit 链。私有版本目录必须位于 Agent 工作区之外，也不会修改用户项目自己的 `.git`。逻辑工作区还会在数据库中绑定规范化根路径，避免同一组租户和工作区标识被复用于其他目录。

职责已经拆分为两层：数据库表 `workspace_operations` 与 `workspace_operation_files` 只保存操作状态、来源、Run、涉及路径以及 `base_commit_id`/`commit_id` 关联；Dulwich 私有裸仓库保存 Blob、Tree、Commit，并作为版本内容和 Diff 的唯一事实来源。

`write_file`、`replace_text` 和 `apply_patch` 每次实际修改都会返回 unified diff、前后 Blob、SHA-256、`operation_id`、`base_commit_id` 和 `commit_id`。`apply_patch` 支持在一个业务操作和一个 Commit 中新增、更新、删除多个 UTF-8 文件；任一 hunk 不匹配时拒绝执行，落盘中途失败时补偿恢复已经修改的文件。`get_file_history` 直接遍历 Dulwich Commit 链；`get_workspace_status` 比较当前磁盘与私有 HEAD，因此能够发现用户、IDE、沙箱或其他进程产生的未提交变化。`WorkspaceMutationService.inspect()` 仍供客户端展示单次操作详情，但不注册为模型工具，避免模型把操作台账误当成当前工作区状态。

所有修改前都会先检查当前磁盘。如果发现工具之外的文件变化，会先建立独立的 `external_checkpoint` Commit，避免把用户修改混入 Agent Commit。完整工作区快照默认忽略 `.git`、`.qharness`、虚拟环境、`node_modules`、Python 缓存和测试缓存，并继续遵守项目 `.gitignore`。`rollback_file_change` 反向应用目标操作涉及的全部文件，而不是重置整个工作区；回滚前必须确认这些文件仍等于目标操作的修改后版本。只要其中一个文件后来发生变化，就返回稳定错误码 `conflict`，不会覆盖用户内容。回滚本身也会生成新的操作和 Commit。

## 工具提供器

`ToolProvider` 表示一种工具来源，负责异步发现或创建工具；`load_tool_providers()` 将多个 Provider 返回的工具统一注册到 `ToolRegistry`。`BuiltinToolProvider` 根据工作区创建 `list_directory`、`read_file`，并在找到可用的 ripgrep 时增加 `search_text`；`FileMutationToolProvider` 创建 `write_file`、`replace_text`、`apply_patch`、`get_file_history`、`get_workspace_status` 和 `rollback_file_change`；`SandboxToolProvider` 根据 RunContext 创建 `run_command`。工具 Handler 不保存调用次数、并发、审批或工具超时，这些全部由外层 ToolExecutor 按工具名称实施。未来本地插件、MCP 和 A2A 工具可以实现同一接口。

每个 Run 应创建自己的 ToolRegistry、ToolExecutor 和 ToolExecutionState。工具默认策略以及 `[tool_execution.tools.run_command]` 等具名覆盖仍来自统一配置，但实际计数和并发信号量不跨 Run 共享。`ripgrep` 的查找顺序是：调用方显式传入的路径、QHarness 内置资源、系统 `PATH`。当前项目先内置官方 ripgrep 15.2.0 Windows x64 版本，因此这个平台不需要用户单独安装；其他平台暂时回退到系统 `PATH`。

`list_directory` 使用 `page_size` 和短随机 `cursor` 滚动分页。首次调用不传 `cursor`；返回 `has_more=true` 时，将 `next_cursor` 原样传入下一次调用。真实分页状态只保存在当前进程内存中，默认 30 分钟滑动过期、最多保存 1024 个；每次有效访问都会重新计算 30 分钟有效期。游标会绑定工作区、目录、递归开关、最大深度和隐藏文件开关，不能跨查询复用；游标过期、应用重启或目录变化导致锚点消失时，需要从第一页重新读取。客户端可以通过 `BuiltinToolProvider` 的 `list_directory_cursor_ttl_seconds` 和 `list_directory_max_cursors` 动态调整这两个值。

`read_file` 使用 `start_line` 和 `max_lines` 流式读取 UTF-8 文本，只保留本次请求的行并额外读取一行判断是否还有内容，不会把整个文件读入内存，也不再限制文件必须小于 2 MiB。返回 `has_more=true` 时，可以把 `next_start_line` 作为下一次调用的 `start_line`。

## Agent Loop 实现进度

已完成[实现计划](docs/Agent-Loop实现计划.md)的批次 1—7：Planner / Executor 已串起完整任务主线，并接入分层反馈、动态阶段选择、证据关联的进展判断及长任务生命周期。应用提供原始 TaskContract 和受信任的 CheckCatalog，通过 `RunService(services)` 调度，会生成初步 Todo、动态规划阶段、执行行动、验证和选择反馈，最终返回 COMPLETED / WAITING / TERMINATED 对应的 RunState。初始规划前暂停时返回 None，保留持久输入与调用账本。

`qharness.run.create_loop_services()` 在已有 RunContext、ModelBackend、ToolExecutor 和 DatabaseManager 上装配调用服务。原独立模型及工具入口继续可用；Loop 请求关闭 SDK 内部重试，由角色调用服务逐次计量。`config/loop.example.toml` 配置 Loop，工具配置与 None 继承语义仍沿用 `tool.toml`。

以下离线检查不需要模型 API Key，也不初始化系统沙箱：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe examples/15_loop_state.py
.\.venv\Scripts\python.exe -X utf8 examples/16_loop_calls.py
.\.venv\Scripts\python.exe -X utf8 examples/18_verification.py
.\.venv\Scripts\python.exe -X utf8 examples/18_verification.py --integration-failure
.\.venv\Scripts\python.exe -X utf8 examples/19_task_executor.py
.\.venv\Scripts\python.exe -X utf8 examples/19_task_executor.py --integration-failure
.\.venv\Scripts\python.exe -X utf8 examples/20_reasoning_strategies.py
.\.venv\Scripts\python.exe -X utf8 examples/20_reasoning_strategies.py --ablation
.\.venv\Scripts\python.exe -X utf8 examples/21_run_lifecycle.py
.\.venv\Scripts\python.exe -X utf8 examples/21_run_lifecycle.py --mode steering
.\.venv\Scripts\python.exe -X utf8 examples/21_run_lifecycle.py --mode approval
.\.venv\Scripts\python.exe -X utf8 examples/21_run_lifecycle.py --mode recovery
```

累计 187 项离线测试通过，第七批新增 29 项生命周期、恢复和上下文测试。示例 19 演示基础任务主线及集成失败后的继续修复；示例 20 演示推理策略并提供八种开关组合对照；示例 21 演示暂停恢复、追加约束、审批以及结果丢失后的历史核对，四种模式均通过。文件和历史真实落盘，模型及检查输出使用可控 Fixture，不能据此宣称真实任务收益。示例 17 接入已配置的真实 Backend 与 SRT：

```powershell
.\.venv\Scripts\python.exe examples/17_stage_actor.py --stream
```

也可以在原有装配后调用 `await services.actor.run(stage_plan, attempt_id="attempt-1", stream=True)`。只有可信声明的独立只读工具允许并行；写入与未知效果顺序执行，前序失败会跳过后续无法证明独立的调用。半截流不执行工具，阶段条件引用错误会进入有界协议修正。

Actor 只交回 candidate / needs_replan / blocked / stalled，不把 Todo 或 Task 标成完成。装配结果现在提供 `services.verifier.verify_stage()` / `verify_task()`，检查继续走已有 ToolService、工具预算和 SRT 沙箱；证据、检查意图和 Failure Bundle 复用 Artifact 表，不增加数据库表。模型 Judge 可补充语义审查，但不能覆盖确定性失败或补出缺失证据。

检查定义由应用提供 `CheckSpec`：Stage 的 targets 对应 expected_results，Todo 对应 acceptance_refs 和 done_when，Task 对应原始 criteria 和 constraints，再组成 `CheckCatalog(tuple(specs))`。检查输入必须包含实现、测试和配置；依赖未知时默认保守扫描。应用还应设置 `context.metadata["verification_environment"]` 为运行时、依赖及外部服务版本的稳定身份，或向 CheckRunner 注入动态身份提供器。Executor 在规划边界调用 `verifier.refresh()` 重开失效 Todo；Task 使用独立检查重新验收。

REPAIR 复用完整 StagePlan，只建立新 Attempt；RETRY_CHECK 不重新调用 Actor。规划和修复上下文会回填最近的完整检查输出。TodoPlanPatch 需绑定当前版本、修订理由和反馈证据，并保持原始验收覆盖。阶段次数、局部修复、重规划、检查重试和事件预算有明确上限；效果未知、预算或环境阻塞进入 WAITING，不伪称完成。

`layered_feedback`、`dynamic_stage_planning`、`track_gap_progress` 三个开关独立控制分层反馈、阶段类型选择、缺口关联与调查停滞处理；示例配置显式启用，旧配置和 `LoopConfig()` 保持关闭。CheckSpec 可增加 addresses、on_failure 和带 fact_id 的 QuestionConclusion，前提是实际断言确实证明这些关联、诊断或结论。模型自评不能生成进展；重复取得同一结论不算新信息，改写阶段标题也不能代替改变实际检查来源。具体接口和可信检查边界见[实现计划第 5.6 节](docs/Agent-Loop实现计划.md#56-批次-6)。

`from qharness.run import RunService` 后，用 `lifecycle = RunService(services)` 装配生命周期入口。`lifecycle.start(checks)` 启动，`await lifecycle.wait()` 等待；UI 断开等待不会取消服务任务。应用通过 `submit(input_id, kind, payload)` 提交已授权的 pause / cancel / resume / steer / approval，并按 `events(after=...)` 补读状态事件。WAITING 后需处理条件再启动；审批绑定具体行动及当前前提，未知效果必须核对后恢复，已完成写入不重放。同主机的 Run 和物理工作区由操作系统锁互斥；原独立入口保留，生命周期保证需要统一经过 RunService。

`context_compaction` 在容量不足时压缩历史，保留完整任务与验收要求；原文和 ContextManifest 存入 Artifact，可经只读 `read_run_artifact` 工具分页取回。示例配置启用压缩，旧配置默认关闭；Artifact 默认保留上限 512 MiB，超限拒绝写入，已有证据不自动删除。接口、恢复取证与单主机边界见[实现计划第 5.7 节](docs/Agent-Loop实现计划.md#57-批次-7)。真实模型、SRT 和生产 Profile 验收是下一批次。

数据库仍由应用级 DatabaseManager.initialize() 统一升级，当前 revision 为 `0003_run_lifecycle`：在七张 Loop 表基础上增加输入、事件两张表，并保留工作区历史；已验证 SQLite 旧数据升级。工具返回的完整业务数据在 `ToolExecutionResult.data`，模型摘要在 `content`，持久输出引用在 `artifact_id`；外层 success 不代表 Shell 退出码为零。

## 当前目录

```text
config/model.toml                         动态模型配置
config/database.toml                      应用级数据库与连接池配置
config/tool.toml                          动态工具执行策略
config/sandbox.toml                       动态沙箱策略
config/history.toml                       Dulwich 私有版本存储配置
src/qharness/persistence/                 应用级数据库、连接池与 Alembic 迁移
src/qharness/model/config.py              模型配置读取
src/qharness/model/models.py              模型调用领域对象
src/qharness/exception/error.py           统一异常定义
src/qharness/utils/text.py                通用字符串工具
src/qharness/tools/                       工具注册、Hook 与受控执行器
src/qharness/tools/builtin/               工作区内置读写与执行工具
src/qharness/tools/providers/             动态工具提供器
src/qharness/resources/ripgrep/           随客户端分发的 ripgrep 与许可证
src/qharness/resources/python/            随客户端分发的独立 Python 归档与来源清单
src/qharness/resources/node/              托管 Node 下载地址、版本和摘要清单
src/qharness/resources/srt/               固定版本 SRT 的 npm 清单与锁文件
src/qharness/runtime/                     托管运行时清单、校验、安全安装与名称解析
src/qharness/run/                         单次 Run 的租户、工作区、沙箱和取消上下文
src/qharness/loop/                        推理契约、状态转换、调用账本、Planner、Actor、Executor 与反馈路由
src/qharness/verification/                三层验证、检查解析、证据有效性与失败包
src/qharness/workspace/                   路径守卫、SQLAlchemy 台账、Dulwich 历史、补丁与回滚
src/qharness/sandbox/                     统一沙箱接口与 Anthropic SRT 后端
src/qharness/backends/base.py             Backend 抽象接口
src/qharness/backends/openai_compatible.py OpenAI-compatible 实现
examples/                                 可直接运行的 main 示例
tests/                                    基于 unittest 的离线行为检查
```
