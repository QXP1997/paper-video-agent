import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { validateDeck } from "./deck_spec_validation.mjs";

const arguments_ = process.argv.slice(2);

function requiredPath(argumentIndex, environmentName) {
  const value = arguments_[argumentIndex] || process.env[environmentName];
  if (!value) throw new Error(`缺少 ${environmentName} 或对应命令行参数`);
  return path.resolve(value);
}

function optionalPath(argumentIndex, environmentName, fallback) {
  const value = arguments_[argumentIndex] || process.env[environmentName];
  return path.resolve(value || fallback);
}

async function ensureAbsent(target, label) {
  try {
    await fs.access(target);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw error;
  }
  throw new Error(`${label} 已存在，拒绝覆盖: ${target}`);
}

const specPath = requiredPath(0, "DECK_SPEC_PATH");
const candidatePath = requiredPath(1, "PPTX_CANDIDATE_PATH");
const outputStem = path.basename(candidatePath, path.extname(candidatePath));
const previewDir = optionalPath(2, "PPTX_PREVIEW_DIR", path.join(path.dirname(candidatePath), `${outputStem}.preview`));
const reportPath = optionalPath(3, "PPTX_REPORT_PATH", path.join(path.dirname(candidatePath), `${outputStem}.render-report.json`));
const runtimeModules = process.env.RUNTIME_NODE_MODULES || process.env.PPTXGENJS_MODULES;
if (!runtimeModules || !path.isAbsolute(runtimeModules)) {
  throw new Error("RUNTIME_NODE_MODULES 或 PPTXGENJS_MODULES 必须是绝对路径");
}
const pptxgenModule = pathToFileURL(
  path.join(runtimeModules, "pptxgenjs", "dist", "pptxgen.es.js"),
).href;
const { default: PptxGenJS } = await import(pptxgenModule);

const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
validateDeck(spec);
await ensureAbsent(candidatePath, "PPTX 输出");
await ensureAbsent(previewDir, "预览目录");
await ensureAbsent(reportPath, "渲染报告");
await fs.mkdir(path.dirname(candidatePath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const sizes = {
  "16:9": { width: 13.333, height: 7.5 },
  "9:16": { width: 7.5, height: 13.333 },
  "4:3": { width: 10, height: 7.5 },
};
const slideSize = sizes[spec.aspect_ratio] ?? sizes["16:9"];
const layoutName = "QH_PRESENTATION";
const pptx = new PptxGenJS();
pptx.defineLayout({ name: layoutName, ...slideSize });
pptx.layout = layoutName;
pptx.author = "research-agent-workbench";
pptx.subject = spec.title;
pptx.title = spec.title;
pptx.company = "research-agent-workbench";
pptx.lang = spec.language ?? "zh-CN";

const warnings = [];
const renderedAssets = [];
const fontFamily = process.env.PRESENTATION_FONT || "Arial";
const colors = {
  background: "F8FAFC",
  title: "0F172A",
  text: "334155",
  accent: "2563EB",
  muted: "64748B",
  white: "FFFFFF",
  border: "CBD5E1",
};

function positionFor(slide, index, total, kind) {
  const width = slideSize.width;
  const height = slideSize.height;
  const margin = width * 0.07;
  const top = height * 0.23;
  const gap = 0.25;
  const visual = ["image", "chart", "table", "diagram", "formula"].includes(kind);
  const isTwoColumn = (slide.layout === "two-column" || slide.layout === "comparison") && total === 2;
  if (isTwoColumn) {
    const columnWidth = (width - margin * 2 - gap) / 2;
    return {
      x: margin + index * (columnWidth + gap),
      y: top,
      w: columnWidth,
      h: height - top - margin,
    };
  }
  if (total === 1 || visual) {
    return { x: margin, y: top, w: width - margin * 2, h: height - top - margin };
  }
  const columnWidth = (width - margin * 2 - gap) / 2;
  return {
    x: margin + (index % 2) * (columnWidth + gap),
    y: top + Math.floor(index / 2) * 1.75,
    w: columnWidth,
    h: 1.45,
  };
}

function addText(slide, text, position, style = {}) {
  slide.addText(String(text ?? ""), {
    ...position,
    fontFace: fontFamily,
    fontSize: style.fontSize ?? 18,
    color: style.color ?? colors.text,
    bold: style.bold ?? false,
    align: style.align ?? "left",
    valign: style.valign ?? "top",
    margin: style.margin ?? 0.08,
    fit: "shrink",
    breakLine: false,
    lang: spec.language ?? "zh-CN",
  });
}

function assetMap(deck) {
  return new Map((deck.assets ?? []).map((asset) => [asset.id, asset]));
}

function tableValues(data) {
  if (Array.isArray(data?.values)) return data.values;
  if (Array.isArray(data?.rows)) return data.headers ? [data.headers, ...data.rows] : data.rows;
  return null;
}

function chartValues(data) {
  if (!Array.isArray(data?.categories) || !Array.isArray(data?.series)) return null;
  return data.series.map((series) => ({
    name: String(series.name ?? "Series"),
    labels: data.categories.map(String),
    values: series.values ?? [],
  }));
}

async function addAsset(slide, asset, position, specDirectory) {
  if (asset.data && asset.kind === "table") {
    const values = tableValues(asset.data);
    if (!values?.length || !values[0]?.length) throw new Error(`Asset ${asset.id} 的表格 data 为空`);
    const columns = Math.max(...values.map((row) => row.length));
    const normalized = values.map((row) => [...row, ...Array(columns - row.length).fill("")]);
    slide.addTable(normalized, {
      ...position,
      fontFace: fontFamily,
      fontSize: 11,
      color: colors.text,
      border: { type: "solid", color: colors.border, pt: 1 },
      fill: { color: colors.white },
      margin: 0.05,
      valign: "mid",
      autoFit: false,
      bold: false,
    });
    renderedAssets.push({ id: asset.id, kind: "native-table", rows: normalized.length, columns });
    return;
  }
  if (asset.data && asset.kind === "chart") {
    const values = chartValues(asset.data);
    if (!values?.length) throw new Error(`Asset ${asset.id} 的图表 data 缺少 categories 或 series`);
    const chartType = String(asset.data.chart_type ?? "bar");
    slide.addChart(chartType, values, {
      ...position,
      fontFace: fontFamily,
      chartColors: [colors.accent, "14B8A6", "F59E0B", "8B5CF6"],
      showLegend: values.length > 1,
      showTitle: false,
      showValue: true,
      dataLabelPosition: "outEnd",
      catAxisLabelFontFace: fontFamily,
      valAxisLabelFontFace: fontFamily,
      valAxisLabelColor: colors.muted,
      catAxisLabelColor: colors.muted,
      valGridLine: { color: colors.border, pt: 1 },
      showCatName: false,
      showSerName: false,
      showPercent: false,
      barDir: chartType === "bar" ? "col" : undefined,
      barGrouping: chartType === "bar" ? "clustered" : undefined,
    });
    renderedAssets.push({ id: asset.id, kind: "native-chart", series: values.length });
    return;
  }
  if (asset.path) {
    const assetPath = path.resolve(specDirectory, asset.path);
    await fs.access(assetPath);
    slide.addImage({
      path: assetPath,
      altText: asset.description,
      // PptxGenJS 的 x/y 属于 ImageProps 顶层；sizing 中的 x/y 仅用于
      // crop 偏移。把坐标塞进 sizing 会让图片回落到页面左上角。
      x: position.x,
      y: position.y,
      sizing: { type: "contain", w: position.w, h: position.h },
      objectName: asset.id,
    });
    renderedAssets.push({ id: asset.id, kind: "image-asset", path: assetPath });
    return;
  }
  if (asset.prompt) {
    throw new Error(`Asset ${asset.id} 只有 prompt，当前渲染器不会伪造未生成的素材`);
  }
  throw new Error(`Asset ${asset.id} 没有可渲染的 path 或 data`);
}

const assets = assetMap(spec);
const inspectionLines = [
  JSON.stringify({ kind: "deck", title: spec.title, renderer: "PptxGenJS" }),
];

for (const [slideIndex, slideSpec] of (spec.slides ?? []).entries()) {
  const slide = pptx.addSlide();
  slide.background = { color: colors.background };
  const isCover = slideSpec.layout === "cover";
  addText(
    slide,
    slideSpec.title,
    { x: 0.75, y: isCover ? 2.2 : 0.5, w: slideSize.width - 1.5, h: isCover ? 0.9 : 0.65 },
    { fontSize: isCover ? 30 : 25, color: colors.title, bold: true, align: isCover ? "center" : "left" },
  );
  if (isCover) {
    addText(
      slide,
      slideSpec.purpose,
      { x: 1.25, y: 3.7, w: slideSize.width - 2.5, h: 0.8 },
      { fontSize: 16, color: colors.muted, align: "center" },
    );
  }

  const elements = slideSpec.elements ?? [];
  for (let index = 0; index < elements.length; index += 1) {
    const element = elements[index];
    const asset = element.asset_id ? assets.get(element.asset_id) : undefined;
    const kind = asset?.kind ?? element.kind;
    const position = isCover && !asset
      ? { x: 1.3, y: 5.25, w: slideSize.width - 2.6, h: 0.45 }
      : positionFor(slideSpec, index, elements.length, kind);
    if (asset) {
      await addAsset(slide, asset, position, path.dirname(specPath));
    } else if (element.content) {
      addText(slide, element.content, position, {
        fontSize: element.kind === "title" ? 22 : 16,
        bold: element.kind === "title",
      });
    }
  }

  const citations = [...(slideSpec.citations ?? [])].map(
    (citation) => `${citation.source_id}${citation.locator ? `: ${citation.locator}` : ""}`,
  );
  const notes = [slideSpec.speaker_notes, citations.length ? `引用：${citations.join("；")}` : ""]
    .filter(Boolean)
    .join("\n\n");
  slide.addNotes(notes);
  for (const step of slideSpec.build_steps ?? []) {
    if (step.action !== "appear") {
      warnings.push(`Slide ${slideSpec.id} 的 ${step.action} 需要在 PPTX 中人工或专用动画渲染器复核`);
    }
  }
  inspectionLines.push(JSON.stringify({
    kind: "slide",
    slide: slideIndex + 1,
    id: slideSpec.id,
    title: slideSpec.title,
    element_count: elements.length,
    notes: Boolean(notes),
  }));
}

await pptx.writeFile({ fileName: candidatePath, compression: true });

const previewManifest = {
  schema_version: "presentation-preview-manifest.v1",
  renderer: "PptxGenJS",
  note: "PptxGenJS 负责生成可编辑 PPTX；如需逐页位图预览，请使用宿主环境提供的 PPTX/PDF 渲染器。",
  slide_count: (spec.slides ?? []).length,
  slides: (spec.slides ?? []).map((slide, index) => ({
    index: index + 1,
    id: slide.id,
    title: slide.title,
    layout: slide.layout,
    elements: slide.elements.map((element) => ({ id: element.id, kind: element.kind, asset_id: element.asset_id })),
  })),
};
await fs.writeFile(
  path.join(previewDir, "manifest.json"),
  JSON.stringify(previewManifest, null, 2),
  "utf8",
);

const inspection = [
  ...inspectionLines,
  ...renderedAssets.map((asset) => JSON.stringify({ kind: asset.kind, ...asset })),
].join("\n");
await fs.writeFile(
  reportPath,
  JSON.stringify({
    schema_version: "presentation-render.v1",
    renderer: "PptxGenJS",
    slide_count: (spec.slides ?? []).length,
    warnings,
    inspection,
  }, null, 2),
  "utf8",
);
