"""渲染器无关的 SlideDeckSpec；它是 PPT 与视频流程共享的事实来源。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"),
]


class DeckModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Citation(DeckModel):
    """指向输入证据的位置；locator 可表示页码、章节、URL 锚点或单元格。"""

    source_id: Identifier
    locator: str | None = None
    note: str | None = None


class DeckSource(DeckModel):
    id: Identifier
    kind: Literal["pdf", "web", "note", "dataset", "other"]
    title: Text
    location: Text


class AssetSpec(DeckModel):
    id: Identifier
    kind: Literal["image", "chart", "table", "diagram", "formula", "code", "video"]
    description: Text
    generator: Literal[
        "source",
        "matplotlib",
        "manim",
        "jupyter",
        "imagegen",
        "other",
    ] = "source"
    path: str | None = None
    prompt: str | None = None
    citations: tuple[Citation, ...] = ()

    @model_validator(mode="after")
    def validate_materialization(self) -> Self:
        if not self.path and not self.prompt:
            raise ValueError("Asset 必须提供已有 path 或可执行的生成 prompt")
        return self


class ElementSpec(DeckModel):
    id: Identifier
    kind: Literal[
        "title",
        "text",
        "image",
        "chart",
        "table",
        "diagram",
        "formula",
        "code",
        "video",
    ]
    content: str | None = None
    asset_id: Identifier | None = None
    role: Literal["primary", "supporting", "annotation", "decoration"] = "primary"
    citations: tuple[Citation, ...] = ()

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if not self.content and not self.asset_id:
            raise ValueError("Slide element 必须提供 content 或 asset_id")
        return self


class BuildStep(DeckModel):
    action: Literal["appear", "highlight", "focus", "replace", "play"]
    target_ids: tuple[Identifier, ...] = Field(min_length=1)
    duration_seconds: Annotated[float, Field(gt=0, le=120)] = 0.5
    narration_cue: str | None = None


class SlideSpec(DeckModel):
    id: Identifier
    title: Text
    purpose: Text
    layout: Literal[
        "cover",
        "section",
        "title-content",
        "two-column",
        "comparison",
        "full-visual",
        "blank",
    ] = "title-content"
    elements: tuple[ElementSpec, ...] = Field(min_length=1)
    speaker_notes: Text
    citations: tuple[Citation, ...] = ()
    build_steps: tuple[BuildStep, ...] = ()
    duration_hint_seconds: Annotated[float, Field(gt=0, le=900)] = 30

    @model_validator(mode="after")
    def validate_build_targets(self) -> Self:
        element_ids = tuple(element.id for element in self.elements)
        if len(element_ids) != len(set(element_ids)):
            raise ValueError(f"Slide {self.id} 的 element id 重复")
        unknown = {
            target
            for step in self.build_steps
            for target in step.target_ids
            if target not in element_ids
        }
        if unknown:
            raise ValueError(f"Slide {self.id} 的动画引用未知 element: {sorted(unknown)}")
        return self


class SlideDeckSpec(DeckModel):
    schema_version: Literal["1.0"] = "1.0"
    title: Text
    audience: Text
    language: Text = "zh-CN"
    aspect_ratio: Literal["16:9", "9:16", "4:3"] = "16:9"
    theme: Text = "research-light"
    sources: tuple[DeckSource, ...] = Field(min_length=1)
    assets: tuple[AssetSpec, ...] = ()
    slides: tuple[SlideSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        source_ids = tuple(source.id for source in self.sources)
        asset_ids = tuple(asset.id for asset in self.assets)
        slide_ids = tuple(slide.id for slide in self.slides)
        for label, values in (
            ("source", source_ids),
            ("asset", asset_ids),
            ("slide", slide_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} id 重复")

        known_sources = set(source_ids)
        known_assets = set(asset_ids)
        citations = [citation for asset in self.assets for citation in asset.citations]
        for slide in self.slides:
            citations.extend(slide.citations)
            citations.extend(
                citation for element in slide.elements for citation in element.citations
            )
            unknown_assets = {
                element.asset_id
                for element in slide.elements
                if element.asset_id and element.asset_id not in known_assets
            }
            if unknown_assets:
                raise ValueError(
                    f"Slide {slide.id} 引用未知 asset: {sorted(unknown_assets)}"
                )
        unknown_sources = {
            citation.source_id
            for citation in citations
            if citation.source_id not in known_sources
        }
        if unknown_sources:
            raise ValueError(f"Citation 引用未知 source: {sorted(unknown_sources)}")
        return self
