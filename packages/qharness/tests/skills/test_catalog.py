from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qharness.exception import SkillConfigurationError
from qharness.persistence import DatabaseConfig, DatabaseManager
from qharness.skills import SkillCatalog
from sqlalchemy import text


def write_skill(root: Path, *, body: str = "根据证据生成演示文稿。") -> Path:
    skill_root = root / "create-demo-deck"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: create-demo-deck\n"
        "description: 将论文或技术材料整理成可编辑的中文演示文稿。\n"
        "metadata:\n"
        "  display-name: 演示文稿生成\n"
        "  category: 内容创作\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    references = skill_root / "references"
    references.mkdir()
    (references / "schema.md").write_text("演示文稿结构约定。\n", encoding="utf-8")
    return skill_root


class SkillCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = DatabaseManager(
            DatabaseConfig(url=f"sqlite:///{(self.root / 'qharness.sqlite3').as_posix()}")
        )
        self.database.initialize()
        self.addCleanup(self.database.close)
        self.catalog = SkillCatalog(
            self.database.session_factory,
            skill_root=self.root / ".qharness" / "skills",
        )

    def test_import_copies_whole_directory_and_indexes_only_entry_path(self) -> None:
        source = write_skill(self.root / "source")

        metadata = self.catalog.import_skill(source)

        managed_root = self.root / ".qharness" / "skills" / "create-demo-deck"
        self.assertEqual(metadata.code, "create-demo-deck")
        self.assertEqual(metadata.name, "演示文稿生成")
        self.assertFalse(metadata.enabled)
        self.assertEqual(metadata.skill_path, managed_root / "SKILL.md")
        self.assertTrue((managed_root / "references" / "schema.md").is_file())
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text("select skill_path, metadata from skills where code = :code"),
                {"code": "create-demo-deck"},
            ).mappings().one()
        self.assertEqual(row["skill_path"], str(managed_root / "SKILL.md"))
        self.assertNotIn("schema.md", row["metadata"])

    def test_generic_agent_obeys_enabled_but_fixed_agent_bypasses_it(self) -> None:
        self.catalog.import_skill(write_skill(self.root / "source"))

        self.assertEqual(self.catalog.list_metadata(enabled=True), ())
        self.assertEqual(self.catalog.load_fixed("create-demo-deck").code, "create-demo-deck")
        with self.assertRaises(SkillConfigurationError):
            self.catalog.load("create-demo-deck")

        enabled = self.catalog.set_enabled("create-demo-deck", True)

        self.assertTrue(enabled.enabled)
        self.assertEqual(
            tuple(skill.code for skill in self.catalog.load_enabled()),
            ("create-demo-deck",),
        )

    def test_update_requires_explicit_flag_and_preserves_enabled_state(self) -> None:
        source = write_skill(self.root / "source")
        self.catalog.import_skill(source, enabled=True)
        (source / "SKILL.md").write_text(
            (source / "SKILL.md").read_text(encoding="utf-8").replace(
                "根据证据生成演示文稿。", "根据可追溯证据生成演示文稿。"
            ),
            encoding="utf-8",
        )

        with self.assertRaises(SkillConfigurationError):
            self.catalog.import_skill(source)

        updated = self.catalog.import_skill(source, update=True)

        self.assertTrue(updated.enabled)
        self.assertIn("可追溯证据", self.catalog.load("create-demo-deck").instructions)

    def test_file_change_is_rejected_until_reimported(self) -> None:
        self.catalog.import_skill(write_skill(self.root / "source"))
        managed_path = self.catalog.get_metadata("create-demo-deck").skill_path
        managed_path.write_text(
            managed_path.read_text(encoding="utf-8").replace(
                "根据证据生成演示文稿。", "未入库的修改。"
            ),
            encoding="utf-8",
        )

        with self.assertRaises(SkillConfigurationError):
            self.catalog.load_fixed("create-demo-deck")


if __name__ == "__main__":
    unittest.main()
