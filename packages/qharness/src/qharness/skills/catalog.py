"""Skill 托管目录与 SQLite/SQLAlchemy 元数据目录。"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from sqlalchemy import Boolean, String, Text, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from qharness.exception import SkillConfigurationError
from qharness.persistence import OrmBase
from qharness.skills.loader import discover_skills, load_skill
from qharness.skills.models import Skill


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_skill_root(workspace_root: str | Path = ".") -> Path:
    """返回工作区默认的托管目录 ``.qharness/skills``。"""
    return (Path(workspace_root).expanduser() / ".qharness" / "skills").resolve()


class SkillRecord(OrmBase):
    """Skill 可检索元数据；指令和资源仍以文件为准。"""

    __tablename__ = "skills"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    skill_path: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    metadata_json: Mapped[str] = mapped_column("metadata", Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """前端和业务 Agent 可直接查询的 Skill 元数据。"""

    code: str
    name: str
    description: str
    skill_path: Path
    digest: str
    enabled: bool
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class SkillCatalog:
    """管理托管 Skill 文件与数据库索引。"""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        skill_root: str | Path = Path(".qharness") / "skills",
    ) -> None:
        self._sessions = sessions
        self.skill_root = Path(skill_root).expanduser().resolve()

    def import_skill(
        self,
        source: str | Path,
        *,
        enabled: bool | None = None,
        update: bool = False,
    ) -> SkillMetadata:
        """
        将整个 Skill 目录复制到托管目录，再写入一条数据库索引。

        已存在且内容不同时需要显式传入 ``update=True``，避免静默覆盖。
        ``enabled=None`` 会保留旧状态，新导入的 Skill 默认停用。
        """
        source_skill = load_skill(source)
        self._reject_symlinks(source_skill.root)
        self.skill_root.mkdir(parents=True, exist_ok=True)
        target_root = self.skill_root / source_skill.code
        target_path = target_root / "SKILL.md"

        if source_skill.root != target_root.resolve():
            if target_root.exists():
                existing = load_skill(target_root)
                if existing.digest != source_skill.digest:
                    if not update:
                        raise SkillConfigurationError(
                            f"Skill 已存在且内容不同，请显式更新: {source_skill.code}"
                        )
                    self._replace_managed_directory(source_skill.root, target_root)
            else:
                self._replace_managed_directory(source_skill.root, target_root)

        managed_skill = load_skill(target_path)
        if managed_skill.code != source_skill.code or managed_skill.digest != source_skill.digest:
            raise SkillConfigurationError(f"Skill 导入后校验失败: {source_skill.code}")
        return self._upsert(managed_skill, enabled=enabled)

    def import_skills(
        self,
        roots: Iterable[str | Path],
        *,
        enabled: bool | None = None,
        update: bool = False,
    ) -> tuple[SkillMetadata, ...]:
        """发现给定目录的 Skill，并逐个导入托管目录。"""
        discovered = discover_skills(roots)
        return tuple(
            self.import_skill(skill.skill_path, enabled=enabled, update=update)
            for skill in discovered.values()
        )

    def list_metadata(self, *, enabled: bool | None = None) -> tuple[SkillMetadata, ...]:
        """仅查询数据库，不重新扫描文件系统。"""
        with self._session() as session:
            statement = select(SkillRecord)
            if enabled is not None:
                statement = statement.where(SkillRecord.enabled == enabled)
            records = session.scalars(statement.order_by(SkillRecord.code)).all()
            return tuple(self._metadata(record) for record in records)

    def get_metadata(self, code: str) -> SkillMetadata:
        """按稳定 code 查询元数据。"""
        with self._session() as session:
            return self._metadata(self._required_record(session, code))

    def set_enabled(self, code: str, enabled: bool) -> SkillMetadata:
        """修改通用对话 Agent 的 Skill 启用状态。"""
        with self._session(transaction=True) as session:
            record = self._required_record(session, code)
            record.enabled = enabled
            record.updated_at = _now()
            session.flush()
            return self._metadata(record)

    def load(self, code: str, *, require_enabled: bool = True) -> Skill:
        """按 code 读取托管 Skill，并校验文件未脱离数据库摘要。"""
        metadata = self.get_metadata(code)
        if require_enabled and not metadata.enabled:
            raise SkillConfigurationError(f"Skill 未启用: {code}")
        self._validate_managed_path(metadata.skill_path)
        skill = load_skill(metadata.skill_path)
        if skill.code != metadata.code:
            raise SkillConfigurationError(f"Skill code 与数据库不一致: {code}")
        if skill.digest != metadata.digest:
            raise SkillConfigurationError(f"Skill 文件已变更，请重新导入: {code}")
        return skill

    def load_enabled(self) -> tuple[Skill, ...]:
        """为通用对话 Agent 读取全部已启用 Skill。"""
        return tuple(self.load(item.code) for item in self.list_metadata(enabled=True))

    def load_fixed(self, code: str) -> Skill:
        """为内置业务 Agent 按固定 code 读取 Skill，忽略前端启停状态。"""
        return self.load(code, require_enabled=False)

    def _upsert(self, skill: Skill, *, enabled: bool | None) -> SkillMetadata:
        with self._session(transaction=True) as session:
            record = session.get(SkillRecord, skill.code)
            timestamp = _now()
            if record is None:
                record = SkillRecord(
                    code=skill.code,
                    name=skill.display_name,
                    description=skill.description,
                    skill_path=str(skill.skill_path),
                    digest=skill.digest,
                    enabled=False if enabled is None else enabled,
                    metadata_json=json.dumps(
                        dict(skill.metadata), ensure_ascii=False, sort_keys=True
                    ),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(record)
            else:
                record.name = skill.display_name
                record.description = skill.description
                record.skill_path = str(skill.skill_path)
                record.digest = skill.digest
                record.metadata_json = json.dumps(
                    dict(skill.metadata), ensure_ascii=False, sort_keys=True
                )
                if enabled is not None:
                    record.enabled = enabled
                record.updated_at = timestamp
            session.flush()
            return self._metadata(record)

    def _replace_managed_directory(self, source_root: Path, target_root: Path) -> None:
        # tempfile.mkdtemp() 在 Windows 会创建仅当前用户可访问的目录；目录随后
        # 原子移动到托管位置时会保留这份 ACL，导致 SRT 专用账户无法读取 Skill。
        # 直接在 skill_root 下按普通目录权限创建暂存区，让文件继承托管根目录 ACL。
        staging_parent = self.skill_root / f".skill-import-{uuid.uuid4().hex}"
        staging_parent.mkdir()
        staged_root = staging_parent / target_root.name
        backup_root: Path | None = None
        try:
            shutil.copytree(source_root, staged_root)
            load_skill(staged_root)
            if target_root.exists():
                backup_root = self.skill_root / f".{target_root.name}.backup-{uuid.uuid4().hex}"
                target_root.replace(backup_root)
            staged_root.replace(target_root)
        except (OSError, shutil.Error) as error:
            if backup_root is not None and backup_root.exists() and not target_root.exists():
                backup_root.replace(target_root)
            raise SkillConfigurationError(f"Skill 目录导入失败: {target_root.name}") from error
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)
            if backup_root is not None:
                shutil.rmtree(backup_root, ignore_errors=True)

    @staticmethod
    def _reject_symlinks(root: Path) -> None:
        if any(path.is_symlink() for path in root.rglob("*")):
            raise SkillConfigurationError("Skill 目录不允许包含符号链接")

    def _validate_managed_path(self, skill_path: Path) -> None:
        expected = (self.skill_root / skill_path.parent.name / "SKILL.md").resolve()
        if skill_path.resolve() != expected or not expected.is_relative_to(self.skill_root):
            raise SkillConfigurationError("Skill 入口路径不在托管目录内")

    @staticmethod
    def _required_record(session: Session, code: str) -> SkillRecord:
        record = session.get(SkillRecord, code)
        if record is None:
            raise SkillConfigurationError(f"Skill 未导入: {code}")
        return record

    @staticmethod
    def _metadata(record: SkillRecord) -> SkillMetadata:
        try:
            metadata = json.loads(record.metadata_json)
        except (json.JSONDecodeError, TypeError) as error:
            raise SkillConfigurationError(f"Skill 元数据损坏: {record.code}") from error
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise SkillConfigurationError(f"Skill 元数据格式错误: {record.code}")
        return SkillMetadata(
            code=record.code,
            name=record.name,
            description=record.description,
            skill_path=Path(record.skill_path),
            digest=record.digest,
            enabled=record.enabled,
            metadata=metadata,
        )

    @contextmanager
    def _session(self, *, transaction: bool = False) -> Iterator[Session]:
        session = self._sessions()
        try:
            if transaction:
                with session.begin():
                    yield session
            else:
                yield session
        except SkillConfigurationError:
            session.rollback()
            raise
        except SQLAlchemyError as error:
            session.rollback()
            raise SkillConfigurationError("Skill 目录数据库操作失败") from error
        finally:
            session.close()
