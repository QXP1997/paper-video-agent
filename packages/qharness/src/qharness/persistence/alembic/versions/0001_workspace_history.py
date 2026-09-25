# -*- coding: utf-8 -*-
"""创建工作区绑定和操作台账表。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0001_workspace_history"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建当前工作区历史功能所需的三张业务表。"""

    op.create_table(
        "workspace_bindings",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("root_digest", sa.String(length=64), nullable=False),
        sa.Column("workspace_root", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "workspace_id"),
    )
    op.create_table(
        "workspace_operations",
        sa.Column("operation_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("base_commit_id", sa.String(length=40), nullable=True),
        sa.Column("commit_id", sa.String(length=40), nullable=True),
        sa.Column(
            "reverted_by_operation_id",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "commit_id",
            name="uq_workspace_operation_commit",
        ),
    )
    op.create_index(
        "idx_workspace_operations_run",
        "workspace_operations",
        ["tenant_id", "workspace_id", "run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_workspace_operations_commit",
        "workspace_operations",
        ["tenant_id", "workspace_id", "commit_id"],
        unique=False,
    )
    op.create_table(
        "workspace_operation_files",
        sa.Column("operation_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["workspace_operations.operation_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("operation_id", "sequence"),
    )


def downgrade() -> None:
    """按外键依赖的逆序移除首版工作区历史结构。"""

    op.drop_table("workspace_operation_files")
    op.drop_index(
        "idx_workspace_operations_commit",
        table_name="workspace_operations",
    )
    op.drop_index(
        "idx_workspace_operations_run",
        table_name="workspace_operations",
    )
    op.drop_table("workspace_operations")
    op.drop_table("workspace_bindings")
