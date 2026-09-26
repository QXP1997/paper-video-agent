# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Added

- 将 QHarness 作为 `packages/qharness` 下的独立子项目迁入工作台，并保留原仓库的完整 Git 提交链、
  独立打包配置、测试、文档和运行时资源。
- 为 QHarness 增加通用 Skill 托管目录、SQLite 元数据索引、启用/停用、任务快照和只读
  参考资料工具；Skill 整目录导入 `.qharness/skills`，新导入项默认停用。
- 新增 Presentation Agent 基础层、`create-presentation` Skill 与渲染器无关的
  `SlideDeckSpec`，支持来源、素材、讲稿、视觉构建步骤和确定性引用校验。
- 将内容设计和 PPTX 渲染合并为一个两阶段的 `create-presentation` Skill，支持论文、博客、文档、
  技术知识、数据集和用户笔记等通用输入。
- 新增 `research-pptx` 渲染入口，使用开源 PptxGenJS 生成可编辑 PPTX、原生表格/图表和演讲备注。

### Changed

- 项目开发环境、CI、根包与 QHarness 统一使用 Python 3.13.15；QHarness 作为所有业务 Agent
  共用的底层 Harness，并在根项目安装与测试前优先安装。
- Skill 摘要覆盖 `SKILL.md`、脚本、参考资料和其他资源，资源内容变化后会要求重新导入，避免
  任务继续绑定到实现已经变化的旧 Skill。
- 顶层项目更名为 Research Agent Workbench；Paper Video Agent 作为首个独立 Agent 保留，
  现有 Python 包名与命令保持兼容。
- 抽取 `research_agent_core` 与 `research_video_core` 共享层，统一缓存、环境配置、TTS、字幕和
  时间轴能力，为后续 Agent 复用；Paper Video Agent 的现有命令、导入路径和缓存指纹保持兼容。
- 将 PDF、MinerU、分页视觉元素、解析缓存和整页渲染迁入公共文档层；各 Agent 可显式指定自己的
  页面图片、解析缓存和 MinerU 结果目录。

## [0.2.0] - 2026-09-25

### Added

- 新增限定在章节 `source_pages` 内的口播事实审核节点，输出可缓存的 `script_audit.json`。
- 新增基于事实审核的全局口播编辑节点，输出可缓存的 `paper_script.edited.json`。
- 新增最终确定性校验与按需局部返修节点，输出 `script_validation.json` 和最终脚本。
- 最终校验仅阻断高风险事实与结构问题，数字密度、长度和轻度重复改为非阻断提醒。
- 新增基于 MinerU 图表、公式和代码素材的独立视觉脚本审核节点，输出
  `visual_review.json` 和词级对齐的 `visual_timeline.json`。

### Changed

- 视频画面默认为脚本指定的完整 PDF 页面；仅在口播明确讲解具体视觉元素时，
  临时切换到 MinerU 聚焦素材，随后切回全页。
- 相同视觉素材的相邻聚焦区间在间隔不超过 5 秒时自动合并，减少 PDF 与素材间的频繁闪切。
- Edge TTS 默认语速调整为 `+25%`，缩短成片时长并提升口播节奏。

### Removed

- 移除旧的本地 PDF 启发式图表检测；聚焦素材改由 MinerU 结构化结果提供。

## [0.1.0] - 2026-09-20

### Added

- PDF 文本、页面、插图和表格提取
- 大模型驱动的视频规划、章节口播和图表展示规划
- Edge TTS 配音、词级时间轴和字幕生成
- FFmpeg 分段合成、图表聚焦、高质量原片及社交平台压缩版
- 发布标题、简介和标签生成
- 可安装命令行、项目文档、测试和持续集成
