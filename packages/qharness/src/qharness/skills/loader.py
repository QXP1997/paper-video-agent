"""加载、发现并显式激活文件系统 Skill。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from qharness.exception import SkillConfigurationError
from qharness.loop import TaskContract
from qharness.skills.models import Skill

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_BYTES = 128 * 1024


def _skill_digest(root: Path) -> str:
    """计算整个 Skill 目录的稳定摘要，而不只校验入口文档。"""

    digest = hashlib.sha256()
    try:
        entries = sorted(
            root.rglob("*"),
            key=lambda item: item.relative_to(root).as_posix(),
        )
        for entry in entries:
            if entry.is_symlink():
                raise SkillConfigurationError("Skill 目录不允许包含符号链接")
            if not entry.is_file():
                continue
            relative_path = entry.relative_to(root).as_posix().encode("utf-8")
            digest.update(b"file\0")
            digest.update(relative_path)
            digest.update(b"\0")
            with entry.open("rb") as resource:
                while chunk := resource.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
    except SkillConfigurationError:
        raise
    except OSError as error:
        raise SkillConfigurationError(f"无法计算 Skill 目录摘要: {root}") from error
    return digest.hexdigest()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise SkillConfigurationError("Skill frontmatter 包含无效双引号字符串") from error
        return str(decoded).strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'").strip()
    return value


def _parse_skill_document(text: str) -> tuple[dict[str, str], dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillConfigurationError("SKILL.md 必须以 YAML frontmatter 开头")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:
        raise SkillConfigurationError("SKILL.md 缺少 frontmatter 结束标记") from error

    manifest: dict[str, str] = {}
    custom_metadata: dict[str, str] = {}
    section: str | None = None
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, value = line.split(":", 1)
        key = key.strip()
        if indent == 0:
            section = key if key == "metadata" and not value.strip() else None
            if key in {"name", "description"}:
                manifest[key] = _unquote(value)
        elif section == "metadata" and indent >= 2 and value.strip():
            custom_metadata[key] = _unquote(value)
    instructions = "\n".join(lines[end + 1:]).strip()
    return manifest, custom_metadata, instructions


def load_skill(path: str | Path) -> Skill:
    """加载一个 Skill 目录或其中的 SKILL.md，并校验稳定身份。"""
    supplied = Path(path).expanduser()
    skill_path = supplied / "SKILL.md" if supplied.is_dir() else supplied
    if skill_path.name != "SKILL.md" or not skill_path.is_file():
        raise SkillConfigurationError(f"Skill 入口不存在: {skill_path}")
    if skill_path.stat().st_size > _MAX_SKILL_BYTES:
        raise SkillConfigurationError("SKILL.md 超过 128 KiB 上限，请使用按需 references")
    try:
        text = skill_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillConfigurationError(f"无法读取 UTF-8 SKILL.md: {skill_path}") from error

    manifest, metadata, instructions = _parse_skill_document(text)
    name = manifest.get("name", "")
    description = manifest.get("description", "")
    display_name = metadata.get("display-name", name)
    root = skill_path.parent.resolve()
    if not _SKILL_NAME.fullmatch(name) or len(name) > 64:
        raise SkillConfigurationError("Skill name 必须是 1 至 64 位小写字母、数字或连字符")
    if root.name != name:
        raise SkillConfigurationError("Skill 目录名必须与 frontmatter name 一致")
    if not description:
        raise SkillConfigurationError("Skill description 不能为空")
    if not display_name:
        raise SkillConfigurationError("Skill metadata.display-name 不能为空")
    if not instructions:
        raise SkillConfigurationError("Skill 指令正文不能为空")

    digest = _skill_digest(root)
    return Skill(
        name=name,
        display_name=display_name,
        description=description,
        instructions=instructions,
        skill_path=skill_path.resolve(),
        digest=digest,
        metadata=metadata,
    )


def discover_skills(roots: Iterable[str | Path]) -> dict[str, Skill]:
    """从若干目录发现其直属 Skill；发现只表示已安装，不会自动启用。"""
    discovered: dict[str, Skill] = {}
    for supplied_root in roots:
        root = Path(supplied_root).expanduser()
        candidates = [root] if (root / "SKILL.md").is_file() else (
            sorted(path for path in root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file())
            if root.is_dir()
            else []
        )
        for candidate in candidates:
            skill = load_skill(candidate)
            if skill.name in discovered:
                raise SkillConfigurationError(f"发现重复 Skill: {skill.name}")
            discovered[skill.name] = skill
    return discovered


def active_skill_bindings(contract: TaskContract) -> dict[str, str]:
    """返回契约内已启用 Skill 的名称和摘要，不读取本地安装目录。"""
    return {skill.name: skill.digest for skill in contract.active_skills}


def resolve_active_skills(
    contract: TaskContract,
    installed: Mapping[str, Skill],
) -> tuple[Skill, ...]:
    """按契约解析本地 Skill，并拒绝缺失或被静默替换的版本。"""
    resolved: list[Skill] = []
    for activation in contract.active_skills:
        skill = installed.get(activation.name)
        if skill is None:
            raise SkillConfigurationError(f"任务启用的 Skill 未安装: {activation.name}")
        if skill.digest != activation.digest:
            raise SkillConfigurationError(f"任务启用的 Skill 版本不匹配: {activation.name}")
        resolved.append(skill)
    return tuple(resolved)


def activate_skills(contract: TaskContract, skills: Iterable[Skill]) -> TaskContract:
    """显式启用选中的 Skill，并把完整指令固化进任务契约。"""
    existing_bindings = active_skill_bindings(contract)
    activations = list(contract.active_skills)
    selected: dict[str, Skill] = {}
    for skill in skills:
        if skill.name in selected and selected[skill.name].digest != skill.digest:
            raise SkillConfigurationError(f"同一任务不能激活两个不同版本的 Skill: {skill.name}")
        selected[skill.name] = skill

    for skill in selected.values():
        existing_digest = existing_bindings.get(skill.name)
        if existing_digest == skill.digest:
            continue
        if existing_digest is not None:
            raise SkillConfigurationError(f"任务已绑定不同版本的 Skill: {skill.name}")
        activations.append(skill.activation())

    return TaskContract.model_validate({
        **contract.model_dump(mode="python"),
        "active_skills": tuple(activations),
    })


def deactivate_skills(contract: TaskContract, names: Iterable[str]) -> TaskContract:
    """显式停用指定 Skill；普通任务约束和其他 Skill 保持不变。"""
    disabled = set(names)
    invalid = sorted(name for name in disabled if not _SKILL_NAME.fullmatch(name))
    if invalid:
        raise SkillConfigurationError(f"待停用 Skill 名称无效: {', '.join(invalid)}")

    activations = tuple(
        skill for skill in contract.active_skills if skill.name not in disabled
    )

    return TaskContract.model_validate({
        **contract.model_dump(mode="python"),
        "active_skills": activations,
    })
