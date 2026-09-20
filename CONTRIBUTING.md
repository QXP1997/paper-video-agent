# Contributing

感谢你对 Paper Video Agent 的关注。

## 开始之前

请先搜索现有 Issue，确认问题尚未被报告。较大的功能改动建议先创建 Issue，说明使用场景、
方案和可能的兼容性影响，再开始实现。

安全漏洞不要通过公开 Issue 报告，请遵循 [SECURITY.md](SECURITY.md)。

## 本地开发

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
ruff check .
pytest
```

提交 Pull Request 前请确保：

- 新行为有相应测试或清楚说明无法自动测试的原因；
- `ruff check .` 和 `pytest` 均通过；
- 不包含 API Key、论文原文、未授权图片或生成的大型媒体文件；
- README 和 `.env.example` 已同步反映新增配置；
- 提交信息简洁说明变更目的。

## Pull Request

Pull Request 描述应包含变更背景、实现方式、验证结果和必要的截图或样例。尽量保持一次 PR
只解决一个主题，避免夹带无关格式化或重构。

