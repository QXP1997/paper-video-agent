---
name: create-presentation
description: 将论文、博客、技术文档、知识点、数据集或用户笔记整理成可复用的演示文稿，并在需要时生成可编辑的 PowerPoint。先产出并校验 SlideDeckSpec，再按同一规格渲染 PPTX；不用于只输出普通文字摘要的任务。
metadata:
  display-name: 演示文稿设计与生成
  category: 通用演示文稿
---

# 生成通用演示文稿

这个 Skill 面向“需要讲清楚一个主题”的材料，不限定输入必须是论文。论文、博客、技术报告、产品文档、教程、数据集和用户笔记都可以进入同一套流程。

## 两阶段流程

始终把 `SlideDeckSpec` 作为内容、证据和版式意图的唯一事实来源：

1. **设计阶段**：理解材料，建立来源和证据，设计叙事、页面、视觉资产、演讲备注和可选构建步骤，写出 `deck_spec.json` 并运行确定性校验。
2. **生成阶段**：只有用户需要 PPT/PPTX 或要求检查实际页面时，才使用同一份已校验的 Spec，通过 PptxGenJS 生成可编辑 `.pptx`。渲染阶段不重新发明事实或改变内容结论。

如果用户只要内容方案或 Spec，在第一阶段完成后停止；如果用户要 PPT，则继续第二阶段。详细规则按需读取：

- [Spec 设计与证据规则](references/spec-design.md)
- [PptxGenJS 渲染与质检规则](references/pptx-render.md)

Skill 内的执行脚本位于 `scripts/`。需要在 QHarness 导入后的 Skill 目录中直接执行时，使用
`scripts/render_pptx.py`；项目安装的 `research-pptx` 命令只是同一套公共渲染逻辑的便捷入口。

## 必须遵守的边界

- 不虚构数字、结论、引语、来源位置或已经生成的素材。
- 每个来源、外部素材和事实声明都要能通过 `source_id` 解析；演讲备注保留足够的引用信息。
- 图表、表格、公式和明确要求可编辑的图示，优先使用 PPT 原生对象；事实性图片优先复用输入材料中的原始资产。
- `build_steps` 只表达渐显、高亮、聚焦、替换和播放意图。若当前 PPTX 后端无法实现某种动画，保留静态内容并明确记录限制，不声称动画已经完成。
- 在项目环境中，生成 PPTX 前运行 `python -m research_presentation_core validate <deck_spec.json>`；Skill 被独立导入 QHarness 后，`scripts/render_pptx.py` 会调用自带渲染器执行等价的结构、引用和资产检查，不依赖宿主虚拟环境。
- QHarness Agent 应先运行 `python <skill-root>/scripts/validate_spec.py <deck_spec.json>`，再调用 `render_pptx.py`；生成后使用 `inspect_pptx.py` 检查 Open XML 包和幻灯片数量。
- PPTX 输出失败时，不能把 `deck_spec.json` 冒充成 PPTX；应报告缺失的运行时或依赖。

## 输出

至少保留经过校验的 `deck_spec.json`。请求 PPT 时，额外输出由 PptxGenJS 生成的可编辑 `.pptx` 和渲染报告；后续视频、网页幻灯片或其他工作流都应消费 Spec 或 PPTX，而不是反向修改内容来源。
