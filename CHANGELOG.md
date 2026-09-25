# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Changed

- 顶层项目更名为 Research Agent Workbench；Paper Video Agent 作为首个独立 Agent 保留，
  现有 Python 包名与命令保持兼容。

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
