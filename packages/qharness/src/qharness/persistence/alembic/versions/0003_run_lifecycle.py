"""持久收件箱与可补读事件；状态、结果和凭据继续复用原表。"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT
from alembic import op

revision = "0003_run_lifecycle"
down_revision = "0002_loop_ledger"
branch_labels = None
depends_on = None


def upgrade():
    payload = lambda: sa.Column("payload", sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    op.create_table("loop_inputs",
        sa.Column("run_key", sa.String(64), sa.ForeignKey("loop_runs.key"), primary_key=True),
        sa.Column("input_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False), payload(),
        sa.Column("status", sa.String(32), nullable=False))
    op.create_table("loop_events",
        sa.Column("run_key", sa.String(64), sa.ForeignKey("loop_runs.key"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False), payload())


def downgrade():
    op.drop_table("loop_events")
    op.drop_table("loop_inputs")
