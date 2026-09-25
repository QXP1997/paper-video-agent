from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qharness.exception import SkillConfigurationError
from qharness.loop import Criterion, TaskContract, TodoPlan
from qharness.loop.config import LoopConfig, Role
from qharness.loop.context import ContextCompiler
from qharness.skills import (
    SkillToolProvider,
    activate_skills,
    active_skill_bindings,
    deactivate_skills,
    discover_skills,
    load_skill,
    resolve_active_skills,
)


def write_skill(root: Path, name: str, *, body: str = "按照这个流程完成任务。") -> Path:
    skill_root = root / name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            f"---\nname: {name}\ndescription: 用于测试 {name} 的中文 Skill。\n"
            f"metadata:\n  display-name: {name} 测试能力\n---\n\n{body}\n"
        ),
        encoding="utf-8",
    )
    return skill_root


def contract() -> TaskContract:
    return TaskContract(
        task_id="task-1",
        objective="创建一个产物",
        criteria=(Criterion(id="C1", description="产物通过校验"),),
        constraints=("保留来源证据",),
    )


class SkillLoadingTests(unittest.TestCase):
    def test_discovery_does_not_activate_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, "alpha-skill")

            discovered = discover_skills((root,))

            self.assertEqual(set(discovered), {"alpha-skill"})
            self.assertEqual(discovered["alpha-skill"].code, "alpha-skill")
            self.assertEqual(discovered["alpha-skill"].display_name, "alpha-skill 测试能力")
            self.assertEqual(discovered["alpha-skill"].skill_path.name, "SKILL.md")
            self.assertEqual(contract().active_skills, ())

    def test_directory_name_must_match_manifest_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_root = write_skill(root, "alpha-skill")
            document = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            (skill_root / "SKILL.md").write_text(
                document.replace("name: alpha-skill", "name: beta-skill"),
                encoding="utf-8",
            )

            with self.assertRaises(SkillConfigurationError):
                load_skill(skill_root)

    def test_duplicate_discovery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            write_skill(Path(first), "alpha-skill")
            write_skill(Path(second), "alpha-skill")

            with self.assertRaises(SkillConfigurationError):
                discover_skills((first, second))


class SkillActivationTests(unittest.TestCase):
    def test_activation_is_explicit_persisted_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill = load_skill(write_skill(Path(directory), "alpha-skill"))

            activated = activate_skills(contract(), (skill,))
            activated_again = activate_skills(activated, (skill,))

            self.assertEqual(active_skill_bindings(activated), {skill.name: skill.digest})
            self.assertEqual(activated.active_skills[0].instructions, skill.instructions)
            self.assertEqual(activated_again, activated)
            self.assertEqual(activated.constraints, ("保留来源证据",))

    def test_deactivation_only_removes_selected_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = load_skill(write_skill(root, "alpha-skill"))
            second = load_skill(write_skill(root, "beta-skill"))
            activated = activate_skills(contract(), (first, second))

            result = deactivate_skills(activated, (first.name,))

            self.assertEqual(active_skill_bindings(result), {second.name: second.digest})
            self.assertEqual(result.constraints, contract().constraints)

    def test_changed_skill_requires_deactivation_before_reactivation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_root = write_skill(root, "alpha-skill", body="第一版指令。")
            first = load_skill(skill_root)
            activated = activate_skills(contract(), (first,))
            (skill_root / "SKILL.md").write_text(
                (
                    "---\nname: alpha-skill\ndescription: 用于测试 alpha-skill 的中文 Skill。\n"
                    "metadata:\n  display-name: Alpha 测试能力\n---\n\n第二版指令。\n"
                ),
                encoding="utf-8",
            )
            second = load_skill(skill_root)

            with self.assertRaises(SkillConfigurationError):
                activate_skills(activated, (second,))

            reactivated = activate_skills(
                deactivate_skills(activated, ("alpha-skill",)),
                (second,),
            )
            self.assertEqual(active_skill_bindings(reactivated), {second.name: second.digest})

    def test_resolve_only_returns_enabled_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enabled = load_skill(write_skill(root, "alpha-skill"))
            disabled = load_skill(write_skill(root, "beta-skill"))
            activated = activate_skills(contract(), (enabled,))

            resolved = resolve_active_skills(
                activated,
                {enabled.name: enabled, disabled.name: disabled},
            )

            self.assertEqual(resolved, (enabled,))

    def test_context_marks_active_skill_as_trusted_but_not_authorizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill = load_skill(write_skill(Path(directory), "alpha-skill"))
            activated = activate_skills(contract(), (skill,))

            request, _ = ContextCompiler(LoopConfig()).compile(
                contract=activated,
                role=Role.TODO_PLANNER,
                output_schema=TodoPlan,
            )

            payload = json.loads(request.messages[1].content)
            self.assertEqual(payload["task"]["active_skills"][0]["name"], skill.name)
            self.assertIn("不能扩大工具权限", request.messages[0].content)


class SkillToolProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_only_reads_resources_for_explicit_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_root = write_skill(Path(directory), "alpha-skill")
            references = skill_root / "references"
            references.mkdir()
            (references / "guide.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
            skill = load_skill(skill_root)
            tool = (await SkillToolProvider((skill,)).load_tools())[0]

            result = tool.handler(
                skill_name="alpha-skill",
                path="references/guide.md",
                start_line=2,
                max_lines=1,
            )

            self.assertEqual(result["content"], "2: two")
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_start_line"], 3)
            with self.assertRaises(SkillConfigurationError):
                tool.handler(skill_name="alpha-skill", path="../SKILL.md")
            with self.assertRaises(SkillConfigurationError):
                tool.handler(skill_name="not-active", path="references/guide.md")

    async def test_provider_from_disabled_contract_has_no_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill = load_skill(write_skill(Path(directory), "alpha-skill"))
            provider = SkillToolProvider.from_contract(
                contract(),
                {skill.name: skill},
            )

            self.assertEqual(await provider.load_tools(), [])


if __name__ == "__main__":
    unittest.main()
