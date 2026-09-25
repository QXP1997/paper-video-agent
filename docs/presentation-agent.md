# Presentation Agent

Presentation Agent 将论文、技术博客、知识点、数据集或用户笔记整理成可复用的演示文稿规格。
它基于 QHarness 的通用 Agent Loop 运行，但演示文稿领域规则不写进 QHarness，而是由
`create-research-deck` Skill 提供。

## 当前边界

当前版本输出并校验 `deck_spec.json`，还不直接承诺生成最终 PPTX。`SlideDeckSpec` 是后续
PPTX、网页幻灯片和讲解视频渲染器共同使用的事实来源，包含：

- 输入来源与可定位引用；
- 可复用或待生成的图片、图表、表格、公式、代码、视频素材；
- 每页目的、版式、元素和配套讲稿；
- 渐显、高亮、聚焦、替换与播放等渲染器无关的构建步骤；
- 素材、引用和动画目标的确定性关系校验。

PPTX 渲染器属于下一层能力：它读取通过校验的 Deck Spec，负责版式、可编辑元素、动画映射、
逐页预览和视觉质检。视频工作流再消费 Deck Spec、PPTX 或渲染后的页面，不反向修改 QHarness。

## Skill 生命周期

Skill 导入时会把整个目录复制到 `.qharness/skills/<code>/`。SQLite 只保存
`SKILL.md` 入口路径、code、中文名称、描述、摘要、扩展元数据和启停状态；
`references/`、`scripts/` 和 `assets/` 都从入口文件的父目录相对解析，不在数据库里维护全量文件清单。

新导入 Skill 默认停用。未来的通用对话 Agent 使用 `load_enabled()` 查询已启用项；
Presentation Agent 是内置业务 Agent，因此按固定 code `create-research-deck` 读取，不受该开关影响。
装配到具体任务时仍会把指令快照写入 `TaskContract.active_skills`。

Presentation Agent 的任务装配如下：

```python
from presentation_agent import (
    build_presentation_check_catalog,
    build_presentation_contract,
    load_presentation_skill,
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
    output_path="output/deck_spec.json",
)
checks = build_presentation_check_catalog("output/deck_spec.json")

skill = load_presentation_skill(skills)
skill_tools = SkillToolProvider.from_contract(contract, {skill.name: skill})
```

Skill 指令会以不可变快照进入 `TaskContract.active_skills`，可随任务持久化、恢复和上下文压缩；
它与业务 `constraints` 分离，不会被最终验证器误当成验收项，也不能扩大工具权限或用户授权。

## 确定性校验

```bash
python -m research_presentation_core validate output/deck_spec.json
python -m research_presentation_core schema
```

校验通过只证明 JSON 结构、ID 和引用关系正确，不代替内容事实审核与逐页视觉检查。
