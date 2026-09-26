# PptxGenJS 渲染与质检规则

## 运行时

PPTX 后端使用开源的 PptxGenJS，不使用 Codex 私有的 `@oai/artifact-tool`。Skill 在 QHarness 沙箱里运行时，直接使用沙箱提供的 `node` 和固定的 `RUNTIME_NODE_MODULES`；不要要求用户系统全局安装 Node，也不要读取用户电脑上的任意 Node 路径。

运行时需要能解析（由 QHarness 注入 `RUNTIME_NODE_MODULES`、`PPTXGENJS_MODULES`，或安装在托管 Node 目录旁）：

```text
<RUNTIME_NODE_MODULES>/pptxgenjs/dist/pptxgen.es.js
```

项目入口：

```text
research-pptx <deck_spec.json> <output.pptx>
```

Skill 被 QHarness 导入后，也可以直接执行 Skill 自带的脚本：

```text
python <skill-root>/scripts/validate_spec.py <deck_spec.json>
python <skill-root>/scripts/render_pptx.py <deck_spec.json> <output.pptx>
python <skill-root>/scripts/inspect_pptx.py <output.pptx>
```

这个入口只使用 Python 标准库，并调用同目录的 Node 渲染器，因此不要求 QHarness 托管 Python 复用项目开发虚拟环境。渲染器会先检查 Spec 的结构、重复 id、素材引用、引用来源和动画目标，再拒绝覆盖已有输出。

PptxGenJS 负责生成可编辑的文本框、图片、表格、图表和演讲备注。输出路径已经存在时应拒绝覆盖。渲染报告记录页面数量、原生表格/图表、引用备注和未实现的动画动作。

## 元素映射

- `text`、`title`：使用可编辑文本框。
- `image`、`diagram`、`formula`、`code`：复用 `AssetSpec.path` 的原始文件，不伪造缺失素材。
- `table`：`AssetSpec.data.headers/rows` 或 `values` 映射为原生 PPT 表格。
- `chart`：`AssetSpec.data.categories/series` 映射为原生 PPT 图表。
- `speaker_notes` 和页面引用写入 PowerPoint 备注页。

## 质检

生成后检查：

1. PPTX 文件确实存在且可被 PowerPoint/Open XML 读取；
2. 页数、标题、备注、引用和资产数量与 Spec 一致；
3. 图片不被拉伸，表格和图表仍可编辑；
4. 双栏内容没有重叠、溢出或被标题遮挡；
5. `build_steps` 中除 `appear` 外的动作在报告中明确标为需要后续动画后端复核。

PptxGenJS 本身负责写入 PPTX，不负责把 PPTX 栅格化为逐页 PNG。若宿主环境提供 LibreOffice、PowerPoint 或其他受控转换器，再额外导出位图做视觉检查；没有转换器时，至少完成结构化检查，不要伪造预览图。
