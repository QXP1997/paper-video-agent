from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from qharness.persistence import DatabaseConfig, DatabaseManager
from qharness.skills import SkillCatalog

from presentation_agent import (
    build_presentation_check_catalog,
    build_presentation_contract,
    load_presentation_skill,
    load_presentation_skills,
)
from presentation_agent.pptx import render_pptx
from research_presentation_core.cli import main
from research_presentation_core.models import SlideDeckSpec


def valid_deck() -> dict:
    return {
        "title": "How the system works",
        "audience": "Software engineers",
        "sources": [
            {
                "id": "source_main",
                "kind": "web",
                "title": "Technical article",
                "location": "https://example.com/article",
            }
        ],
        "assets": [
            {
                "id": "architecture",
                "kind": "diagram",
                "description": "System architecture",
                "generator": "source",
                "path": "assets/architecture.svg",
                "citations": [{"source_id": "source_main", "locator": "Architecture"}],
            }
        ],
        "slides": [
            {
                "id": "slide_1",
                "title": "One request crosses three layers",
                "purpose": "Introduce the architecture",
                "layout": "full-visual",
                "elements": [
                    {
                        "id": "diagram",
                        "kind": "diagram",
                        "asset_id": "architecture",
                    }
                ],
                "speaker_notes": "Explain the three layers from left to right.",
                "citations": [{"source_id": "source_main", "locator": "Architecture"}],
                "build_steps": [
                    {
                        "action": "focus",
                        "target_ids": ["diagram"],
                        "duration_seconds": 1,
                    }
                ],
            }
        ],
    }


def test_valid_deck_resolves_sources_assets_and_build_targets() -> None:
    deck = SlideDeckSpec.model_validate(valid_deck())

    assert deck.schema_version == "1.0"
    assert deck.slides[0].elements[0].asset_id == "architecture"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset", "missing_asset"),
        ("source", "missing_source"),
        ("target", "missing_element"),
    ],
)
def test_invalid_references_are_rejected(field: str, value: str) -> None:
    data = valid_deck()
    if field == "asset":
        data["slides"][0]["elements"][0]["asset_id"] = value
    elif field == "source":
        data["slides"][0]["citations"][0]["source_id"] = value
    else:
        data["slides"][0]["build_steps"][0]["target_ids"] = [value]

    with pytest.raises(ValidationError):
        SlideDeckSpec.model_validate(data)


def test_cli_validates_json_file(tmp_path, capsys) -> None:
    path = tmp_path / "deck_spec.json"
    path.write_text(json.dumps(valid_deck()), encoding="utf-8")

    exit_code = main(("validate", str(path)))

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result == {
        "valid": True,
        "path": str(path),
        "slides": 1,
        "assets": 1,
        "sources": 1,
    }


def test_cli_reports_invalid_json(tmp_path, capsys) -> None:
    path = tmp_path / "deck_spec.json"
    path.write_text("{}", encoding="utf-8")

    exit_code = main(("validate", str(path)))

    result = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert result["valid"] is False


def test_presentation_agent_loads_fixed_skill_from_managed_catalog(tmp_path) -> None:
    database = DatabaseManager(
        DatabaseConfig(url=f"sqlite:///{(tmp_path / 'qharness.sqlite3').as_posix()}")
    )
    database.initialize()
    try:
        catalog = SkillCatalog(
            database.session_factory,
            skill_root=tmp_path / ".qharness" / "skills",
        )

        contract = build_presentation_contract(
            task_id="deck-001",
            objective="生成一份中文研究演示文稿",
            skill_catalog=catalog,
        )
        skill = load_presentation_skill(catalog)

        assert catalog.get_metadata("create-presentation").enabled is False
        assert skill.skill_path == (
            tmp_path / ".qharness" / "skills" / "create-presentation" / "SKILL.md"
        )
        assert contract.active_skills[0].name == "create-presentation"
        assert "生成通用演示文稿" in skill.instructions
    finally:
        database.close()


def test_presentation_agent_can_activate_content_and_pptx_skills(tmp_path) -> None:
    database = DatabaseManager(
        DatabaseConfig(url=f"sqlite:///{(tmp_path / 'qharness.sqlite3').as_posix()}")
    )
    database.initialize()
    try:
        catalog = SkillCatalog(
            database.session_factory,
            skill_root=tmp_path / ".qharness" / "skills",
        )

        skills = load_presentation_skills(catalog)
        contract = build_presentation_contract(
            task_id="deck-with-pptx-001",
            objective="输出可编辑的 PPTX",
            skill_catalog=catalog,
            include_pptx_skill=True,
        )

        assert tuple(skill.code for skill in skills) == ("create-presentation",)
        assert tuple(skill.name for skill in contract.active_skills) == ("create-presentation",)
        assert tuple(criterion.id for criterion in contract.criteria) == (
            "C_SPEC",
            "C_EVIDENCE",
            "C_PPTX",
        )
        assert catalog.get_metadata("create-presentation").name == "演示文稿设计与生成"
    finally:
        database.close()


def test_presentation_checks_cover_spec_and_pptx() -> None:
    checks = build_presentation_check_catalog(
        "output/deck_spec.json",
        pptx_output_path="output/deck.pptx",
    )

    assert tuple(check.id for check in checks.specs) == (
        "validate-slide-deck-spec-todo",
        "validate-slide-deck-spec",
        "inspect-generated-pptx-todo",
        "inspect-generated-pptx",
    )
    assert checks.specs[0].targets == ("C_SPEC", "C_EVIDENCE")
    assert checks.specs[1].targets == ("C_SPEC", "C_EVIDENCE")
    assert checks.specs[2].targets == ("C_PPTX",)
    assert checks.specs[3].targets == ("C_PPTX",)


@pytest.mark.skipif(
    not Path(os.environ.get("RUNTIME_NODE", "")).is_file()
    or not Path(os.environ.get("RUNTIME_NODE_MODULES", "")).is_dir(),
    reason="需要 QHarness 或 Codex 提供的 Node 与 PptxGenJS 运行时",
)
def test_pptx_renderer_exports_preview_and_editable_file(tmp_path) -> None:
    spec_path = tmp_path / "deck_spec.json"
    data = valid_deck()
    data["assets"] = []
    data["slides"][0]["elements"] = [
        {"id": "body", "kind": "text", "content": "渲染器写入一个可编辑的文本框。"}
    ]
    data["slides"][0]["build_steps"] = []
    data["assets"] = [
        {
            "id": "table_asset",
            "kind": "table",
            "description": "可编辑测试表格",
            "data": {"headers": ["指标", "数值"], "rows": [["样本", 3]]},
        },
        {
            "id": "chart_asset",
            "kind": "chart",
            "description": "可编辑测试图表",
            "data": {
                "chart_type": "bar",
                "categories": ["一月", "二月"],
                "series": [{"name": "数量", "values": [2, 4]}],
            },
        },
    ]
    data["slides"].extend(
        [
            {
                "id": "slide_table",
                "title": "可编辑表格",
                "purpose": "验证原生表格",
                "elements": [{"id": "table", "kind": "table", "asset_id": "table_asset"}],
                "speaker_notes": "表格应保持可编辑。",
            },
            {
                "id": "slide_chart",
                "title": "可编辑图表",
                "purpose": "验证原生图表",
                "elements": [{"id": "chart", "kind": "chart", "asset_id": "chart_asset"}],
                "speaker_notes": "图表应保持可编辑。",
            },
        ]
    )
    spec_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    output_path = tmp_path / "deck.pptx"

    result = render_pptx(spec_path, output_path, keep_build=True)

    assert output_path.is_file()
    assert result["schema_version"] == "presentation-render.v1"
    assert result["slide_count"] == 3
    preview_manifest = Path(result["preview_dir"]) / "manifest.json"
    assert preview_manifest.is_file()
    assert json.loads(preview_manifest.read_text(encoding="utf-8"))["slide_count"] == 3
    assert '"kind":"native-table"' in result["inspection"]
    assert '"kind":"native-chart"' in result["inspection"]
