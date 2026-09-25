"""Skill 托管目录的可检索元数据与启停状态。"""

import sqlalchemy as sa
from alembic import op

revision = "0004_skill_catalog"
down_revision = "0003_run_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "skills",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("skill_path", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_skills_enabled", "skills", ["enabled"])


def downgrade():
    op.drop_index("ix_skills_enabled", table_name="skills")
    op.drop_table("skills")
