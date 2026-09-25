from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from qharness.persistence import DatabaseConfig, DatabaseManager
from qharness.skills import SkillCatalog

from presentation_agent import build_presentation_contract, load_presentation_skill
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

        assert catalog.get_metadata("create-research-deck").enabled is False
        assert skill.skill_path == (
            tmp_path / ".qharness" / "skills" / "create-research-deck" / "SKILL.md"
        )
        assert contract.active_skills[0].name == "create-research-deck"
        assert "生成研究演示文稿" in skill.instructions
    finally:
        database.close()
