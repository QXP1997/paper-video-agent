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
Copy-Item .\config\tool.example.toml .\config\tool.toml
Copy-Item .\config\sandbox.example.toml .\config\sandbox.toml
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

模型配置位于 `config/model.toml`，工具执行策略位于 `config/tool.toml`，沙箱策略位于 `config/sandbox.toml`。程序每次启动都会重新读取配置文件，不使用环境变量覆盖。

工具策略分为三层：`[tool_execution]` 保存整个 Run 的总调用数和总并发限制；`[tool_execution.defaults]` 保存所有工具的默认策略；`[tool_execution.tools.<工具名>]` 只覆盖指定工具的字段。没有具名配置的工具自动继承默认策略。

全局 `max_total_calls` 和 `max_concurrency` 默认不限制；只有在 `[tool_execution]` 中显式配置正整数后才会启用对应限制。

可提交到仓库的模板位于 [config/model.example.toml](config/model.example.toml)、[config/tool.example.toml](config/tool.example.toml) 和 [config/sandbox.example.toml](config/sandbox.example.toml)。

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

## Anthropic SRT 沙箱

QHarness 使用统一的 `SandboxBackend` 接口执行脚本和 CLI，当前实现为 `SrtSandboxBackend`。文件读取和修改仍由 QHarness 自己的工作区工具完成；只有外部进程执行进入沙箱。请求使用 `executable + arguments` 的 argv 形式，不把模型生成的内容拼成宿主 Shell 字符串。

开发环境可以把官方 SRT npm 包安装到 QHarness 的本地运行目录：

```powershell
npm install --prefix .\.qharness\runtime\srt --omit=dev --ignore-scripts --no-audit --no-fund --package-lock=false @anthropic-ai/sandbox-runtime@0.0.74
```

Windows 版 SRT 还需要用户明确执行一次系统初始化，它会弹出 UAC，并创建专用的 `srt-sandbox` 本地账户和 WFP 网络规则。QHarness 不会自动执行这个提权操作；运行 `examples/08_srt_sandbox.py` 会检查状态并打印当前机器对应的准确初始化命令。SRT 当前仍是 Anthropic 的 Research Preview，Windows 支持为 Alpha，因此版本在配置中固定为 `0.0.74`，升级时需要重新验证策略语义。

默认策略禁止网络访问，只允许读写当前工作区，同时保护模型配置、沙箱配置、`.env`、Git Hook 和 `.qharness` 运行目录。标准输出、标准错误、执行时间均有上限；超时、主动取消或输出超限时会终止整个进程树。`run_id` 和 `operation_id` 已保留在请求与结果中，后续可与文件变更日志和撤回功能关联。

## 工作区边界

`WorkspaceContext` 由客户端在每次 Agent Run 开始时根据用户选择的目录创建。所有文件工具都必须通过它解析路径，以阻止 `..`、绝对路径和符号链接逃出工作区。工作区边界不负责隔离 Shell 或外部代码；代码执行仍需单独经过 Sandbox 层。

## 工具提供器

`ToolProvider` 表示一种工具来源，负责异步发现或创建工具；`load_tool_providers()` 将多个 Provider 返回的工具统一注册到 `ToolRegistry`。内置 Provider 根据工作区创建 `list_directory`、`read_file`，并在找到可用 `ripgrep` 时增加 `search_text`。`ripgrep` 的查找顺序是：调用方显式传入的路径、QHarness 内置资源、系统 `PATH`。当前项目先内置官方 ripgrep 15.2.0 Windows x64 版本，因此这个平台不需要用户单独安装；其他平台暂时回退到系统 `PATH`。未来本地插件、MCP 和 A2A 工具可以实现同一接口。

`list_directory` 使用 `page_size` 和短随机 `cursor` 滚动分页。首次调用不传 `cursor`；返回 `has_more=true` 时，将 `next_cursor` 原样传入下一次调用。真实分页状态只保存在当前进程内存中，默认 30 分钟滑动过期、最多保存 1024 个；每次有效访问都会重新计算 30 分钟有效期。游标会绑定工作区、目录、递归开关、最大深度和隐藏文件开关，不能跨查询复用；游标过期、应用重启或目录变化导致锚点消失时，需要从第一页重新读取。客户端可以通过 `BuiltinToolProvider` 的 `list_directory_cursor_ttl_seconds` 和 `list_directory_max_cursors` 动态调整这两个值。

`read_file` 使用 `start_line` 和 `max_lines` 流式读取 UTF-8 文本，只保留本次请求的行并额外读取一行判断是否还有内容，不会把整个文件读入内存，也不再限制文件必须小于 2 MiB。返回 `has_more=true` 时，可以把 `next_start_line` 作为下一次调用的 `start_line`。

## 当前目录

```text
config/model.toml                         动态模型配置
config/tool.toml                          动态工具执行策略
config/sandbox.toml                       动态沙箱策略
src/qharness/model/config.py              模型配置读取
src/qharness/model/models.py              模型调用领域对象
src/qharness/exception/error.py           统一异常定义
src/qharness/utils/text.py                通用字符串工具
src/qharness/tools/                       工具注册、Hook 与受控执行器
src/qharness/tools/builtin/               工作区内置只读工具
src/qharness/tools/providers/             动态工具提供器
src/qharness/resources/ripgrep/           随客户端分发的 ripgrep 与许可证
src/qharness/workspace/                   工作区上下文和安全路径守卫
src/qharness/sandbox/                     统一沙箱接口与 Anthropic SRT 后端
src/qharness/backends/base.py             Backend 抽象接口
src/qharness/backends/openai_compatible.py OpenAI-compatible 实现
examples/                                 可直接运行的 main 示例
```
