"""增加 Loop 状态、调用尝试、消息与 Artifact；保留原工作区历史表。"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT
from alembic import op


revision = "0002_loop_ledger"
down_revision = "0001_workspace_history"
branch_labels = None
depends_on = None


def payload(name, nullable=False):
    return sa.Column(name, sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=nullable)


def run_key():
    return sa.Column("run_key", sa.String(64), nullable=False)


def call_id():
    return sa.Column("call_id", sa.String(128), nullable=False)


def upgrade():
    op.create_table("loop_runs",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        payload("contract"), payload("state", True), payload("policy"),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in (
            "version", "ledger_version", "model_attempts", "used_tokens", "reserved_tokens", "tool_calls", "tool_executions")])
    op.create_table("loop_model_calls", run_key(), call_id(),
        payload("binding"),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False), payload("response", True), payload("error", True),
        sa.PrimaryKeyConstraint("run_key", "call_id"), sa.ForeignKeyConstraint(["run_key"], ["loop_runs.key"]))
    op.create_table("loop_model_attempts", run_key(), call_id(),
        sa.Column("attempt", sa.Integer(), nullable=False), payload("request"),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_known", sa.Boolean(), nullable=False), payload("response", True), payload("error", True),
        sa.Column("started_at", sa.String(40), nullable=False), sa.Column("finished_at", sa.String(40)),
        sa.PrimaryKeyConstraint("run_key", "call_id", "attempt"),
        sa.ForeignKeyConstraint(["run_key", "call_id"], ["loop_model_calls.run_key", "loop_model_calls.call_id"]))
    op.create_table("loop_messages", run_key(), call_id(),
        sa.Column("attempt", sa.Integer(), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False), payload("payload"),
        sa.PrimaryKeyConstraint("run_key", "call_id", "attempt", "sequence"),
        sa.ForeignKeyConstraint(["run_key", "call_id", "attempt"],
            ["loop_model_attempts.run_key", "loop_model_attempts.call_id", "loop_model_attempts.attempt"]))
    op.create_table("loop_tool_calls", run_key(), call_id(),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("tool_name", sa.String(128), nullable=False),
        payload("arguments"), payload("binding"), payload("policy"),
        sa.Column("operation_id", sa.String(32), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("executions", sa.Integer(), nullable=False), payload("result", True),
        sa.PrimaryKeyConstraint("run_key", "call_id"), sa.UniqueConstraint("operation_id"),
        sa.ForeignKeyConstraint(["run_key"], ["loop_runs.key"]))
    op.create_table("loop_tool_executions", run_key(), call_id(),
        sa.Column("execution", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=False), sa.Column("finished_at", sa.String(40)), payload("result", True),
        sa.PrimaryKeyConstraint("run_key", "call_id", "execution"),
        sa.ForeignKeyConstraint(["run_key", "call_id"], ["loop_tool_calls.run_key", "loop_tool_calls.call_id"]))
    op.create_table("loop_artifacts", run_key(), sa.Column("artifact_id", sa.String(64), nullable=False),
        payload("content"), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False), sa.Column("media_type", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("run_key", "artifact_id"), sa.ForeignKeyConstraint(["run_key"], ["loop_runs.key"]))


def downgrade():
    for name in ("loop_artifacts", "loop_tool_executions", "loop_tool_calls", "loop_messages",
                 "loop_model_attempts", "loop_model_calls", "loop_runs"):
        op.drop_table(name)
