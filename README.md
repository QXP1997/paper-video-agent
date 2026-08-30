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

模型配置位于 `config/model.toml`，工具执行策略位于 `config/tool.toml`。程序每次启动都会重新读取配置文件，不使用环境变量覆盖。

工具策略分为三层：`[tool_execution]` 保存整个 Run 的总调用数和总并发限制；`[tool_execution.defaults]` 保存所有工具的默认策略；`[tool_execution.tools.<工具名>]` 只覆盖指定工具的字段。没有具名配置的工具自动继承默认策略。

全局 `max_total_calls` 和 `max_concurrency` 默认不限制；只有在 `[tool_execution]` 中显式配置正整数后才会启用对应限制。

可提交到仓库的模板位于 [config/model.example.toml](config/model.example.toml) 和 [config/tool.example.toml](config/tool.example.toml)。

## 工具参数

本地 Python 工具可以通过 Pydantic `BaseModel` 子类声明参数，QHarness 会自动生成 JSON Schema 并在执行前校验参数。MCP、A2A 等动态工具也可以直接传入原始 JSON Schema 字典；两种形式可以同时注册和执行。

## 直接运行示例

```powershell
.\.venv\Scripts\python.exe .\examples\01_basic_chat.py
.\.venv\Scripts\python.exe .\examples\02_stream_chat.py
.\.venv\Scripts\python.exe .\examples\03_tool_call.py
.\.venv\Scripts\python.exe .\examples\04_thinking_tool_call.py
.\.venv\Scripts\python.exe .\examples\05_tool_runtime.py
```

示例用途：

1. 普通非流式调用；
2. 流式文本与 Usage；
3. 普通模式工具调用闭环；
4. DeepSeek 思考模式工具调用及 `reasoning_content` 回填。
5. 工具注册、参数校验、Hook 与执行次数限制。

## 当前目录

```text
config/model.toml                         动态模型配置
config/tool.toml                          动态工具执行策略
src/qharness/model/config.py              模型配置读取
src/qharness/model/models.py              模型调用领域对象
src/qharness/exception/error.py           统一异常定义
src/qharness/utils/text.py                通用字符串工具
src/qharness/tools/                       工具注册、Hook 与受控执行器
src/qharness/backends/base.py             Backend 抽象接口
src/qharness/backends/openai_compatible.py OpenAI-compatible 实现
examples/                                 可直接运行的 main 示例
```
