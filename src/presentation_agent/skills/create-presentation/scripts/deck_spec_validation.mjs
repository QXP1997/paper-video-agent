export function validateDeck(deck) {
  const identifier = /^[A-Za-z][A-Za-z0-9_-]*$/;
  if (!deck || typeof deck !== "object" || Array.isArray(deck)) {
    throw new Error("SlideDeckSpec 必须是 JSON 对象");
  }
  if ((deck.schema_version ?? "1.0") !== "1.0") {
    throw new Error("SlideDeckSpec schema_version 必须为 1.0");
  }
  if (typeof deck.title !== "string" || !deck.title.trim()) {
    throw new Error("SlideDeckSpec title 不能为空");
  }
  if (!Array.isArray(deck.sources) || deck.sources.length === 0) {
    throw new Error("SlideDeckSpec 至少需要一个 source");
  }
  if (!Array.isArray(deck.slides) || deck.slides.length === 0) {
    throw new Error("SlideDeckSpec 至少需要一页 slide");
  }
  if (!Array.isArray(deck.assets)) throw new Error("SlideDeckSpec assets 必须是数组");

  const collectIds = (items, label) => {
    const ids = items.map((item) => item?.id);
    if (ids.some((id) => typeof id !== "string" || !identifier.test(id))) {
      throw new Error(`${label} 包含非法 id`);
    }
    if (new Set(ids).size !== ids.length) throw new Error(`${label} id 重复`);
    return new Set(ids);
  };
  const sourceIds = collectIds(deck.sources, "source");
  const assetIds = collectIds(deck.assets, "asset");
  collectIds(deck.slides, "slide");

  const citations = [];
  for (const asset of deck.assets) {
    if (!asset.path && !asset.prompt && asset.data == null) {
      throw new Error(`资产 ${asset.id} 没有 path、prompt 或 data`);
    }
    citations.push(...(asset.citations || []));
  }
  for (const slide of deck.slides) {
    if (typeof slide.title !== "string" || !slide.title.trim()) {
      throw new Error(`Slide ${slide.id} title 不能为空`);
    }
    if (!Array.isArray(slide.elements) || slide.elements.length === 0) {
      throw new Error(`Slide ${slide.id} 至少需要一个 element`);
    }
    const elementIds = collectIds(slide.elements, `Slide ${slide.id} element`);
    for (const element of slide.elements) {
      if (!element.content && !element.asset_id) {
        throw new Error(`Element ${element.id} 没有 content 或 asset_id`);
      }
      if (element.asset_id && !assetIds.has(element.asset_id)) {
        throw new Error(`Slide ${slide.id} 引用未知 asset: ${element.asset_id}`);
      }
      citations.push(...(element.citations || []));
    }
    for (const step of slide.build_steps || []) {
      for (const targetId of step.target_ids || []) {
        if (!elementIds.has(targetId)) {
          throw new Error(`Slide ${slide.id} 的动画引用未知 element: ${targetId}`);
        }
      }
    }
    citations.push(...(slide.citations || []));
  }
  for (const citation of citations) {
    if (!sourceIds.has(citation?.source_id)) {
      throw new Error(`Citation 引用未知 source: ${citation?.source_id}`);
    }
  }
}
