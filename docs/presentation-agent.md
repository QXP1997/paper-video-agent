# Presentation Agent

Presentation Agent 将论文、技术博客、技术文档、知识点、数据集或用户笔记整理成可复用的演示文稿。
它基于 QHarness 的通用 Agent Loop 运行，但演示文稿领域规则不写进 QHarness，而是由
`create-presentation` Skill 提供。

## 两阶段流程

Presentation Agent 只激活一个 Skill，但内部明确分成两个阶段：

1. `SlideDeckSpec` 设计：从输入材料建立来源、证据、页面叙事、视觉资产和演讲备注，并校验 `deck_spec.json`。
2. PPTX 生成：需要 PowerPoint 时，使用同一份已校验的 Spec，通过开源 PptxGenJS 生成可编辑的 `.pptx`。

如果用户只需要内容方案或 Spec，在第一阶段结束；用户需要 PPT 时才继续第二阶段。

`SlideDeckSpec` 是 PPTX、网页幻灯片和讲解视频渲染器共同使用的事实来源，包含：

- 输入来源与可定位引用；
- 可复用或待生成的图片、图表、表格、公式、代码、视频素材；
- 每页目的、版式、元素和配套讲稿；
- 渐显、高亮、聚焦、替换与播放等渲染器无关的构建步骤；
- 素材、引用和动画目标的确定性关系校验。

PPTX 渲染器使用 PptxGenJS 负责版式、可编辑元素、备注和动画意图映射。QHarness 沙箱提供托管的 Node 和 Python，Skill 不要求用户系统全局安装 Node。PptxGenJS 本身不负责逐页位图栅格化；宿主环境有受控 PPTX/PDF 转换器时才额外导出位图做视觉检查。视频工作流可消费 Deck Spec、PPTX 或渲染后的页面，不反向修改 QHarness。

## Skill 生命周期

Skill 导入时会把整个目录复制到 `.qharness/skills/<code>/`。SQLite 只保存
`SKILL.md` 入口路径、code、中文名称、描述、摘要、扩展元数据和启停状态；
`references/`、`scripts/` 和 `assets/` 都从入口文件的父目录相对解析，不在数据库里维护全量文件清单。

新导入 Skill 默认停用。未来的通用对话 Agent 使用 `load_enabled()` 查询已启用项；
Presentation Agent 是内置业务 Agent，因此按固定 code `create-presentation` 读取，不受该开关影响。
装配到具体任务时仍会把指令快照写入 `TaskContract.active_skills`。

Presentation Agent 的任务装配如下：

```python
from presentation_agent import (
    build_presentation_check_catalog,
    build_presentation_contract,
    load_presentation_skills,
)
from qharness.persistence import DatabaseManager, load_database_config
from qharness.skills import SkillCatalog, SkillToolProvider

database = DatabaseManager(load_database_config("packages/qharness/config/database.toml"))
database.initialize()
skills = SkillCatalog(database.session_factory, skill_root=".qharness/skills")

contract = build_presentation_contract(
    task_id="deck-001",
    objective="把输入技术文章做成面向工程师的中文演示文稿",
    skill_catalog=skills,
    include_pptx_skill=True,
    output_path="output/deck_spec.json",
    pptx_output_path="output/research-deck.pptx",
)
checks = build_presentation_check_catalog(
    "output/deck_spec.json",
    pptx_output_path="output/research-deck.pptx",
)

loaded = load_presentation_skills(skills)
skill_tools = SkillToolProvider.from_contract(
    contract,
    {skill.code: skill for skill in loaded},
)
```

Skill 指令会以不可变快照进入 `TaskContract.active_skills`，可随任务持久化、恢复和上下文压缩；
它与业务 `constraints` 分离，不会被最终验证器误当成验收项，也不能扩大工具权限或用户授权。

## 确定性校验

```bash
python -m research_presentation_core validate output/deck_spec.json
python -m research_presentation_core schema
```

校验通过只证明 JSON 结构、ID 和引用关系正确，不代替内容事实审核与逐页视觉检查。

## 使用 QHarness 调试 PPTX 渲染

仓库提供了一个真实 Agent Loop 调试入口。它会把已有 MinerU 结果复制成独立工作区输入，
激活 `create-presentation` Skill，并向 Actor 暴露文件、Skill 资源和沙箱命令工具。模型必须
先读取 Skill reference，再分析材料、生成 Spec，最后由模型发起校验和 PptxGenJS 渲染：

```powershell
.\.venv\Scripts\python.exe .\packages\qharness\examples\22_presentation_skill.py
```

该脚本不接收命令行参数；论文目录、`DEBUG_RUN_NAME`、目标听众、页数和输出文件都写在顶部，
方便直接在 PyCharm 中修改变量和下断点。模型配置优先读取
`packages/qharness/config/model.toml`；文件不存在时，这个调试示例会复用仓库根目录 `.env` 中的
`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`，不会复制或打印密钥。为了避免误覆盖，
同一 `DEBUG_RUN_NAME` 已经产出 Spec 或 PPTX 时会停止，需要改成新的运行名称。
