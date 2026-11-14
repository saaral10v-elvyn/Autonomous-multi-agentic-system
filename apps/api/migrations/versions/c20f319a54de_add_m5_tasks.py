"""Add M5 durable tasks and allow repeated agent roles."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c20f319a54de"
down_revision: str | None = "b6d81f32c740"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_agents_run_role", "agents", type_="unique")
    op.drop_constraint("ck_agents_status", "agents", type_="check")
    op.create_check_constraint(
        "ck_agents_status",
        "agents",
        "status IN ('created', 'running', 'completed', 'failed', 'interrupted')",
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("external_id", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("depends_on", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "external_id", name="uq_tasks_run_external_id"),
    )
    op.create_index("ix_tasks_run_status", "tasks", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_tasks_run_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_constraint("ck_agents_status", "agents", type_="check")
    op.create_check_constraint(
        "ck_agents_status",
        "agents",
        "status IN ('created', 'running', 'completed', 'failed')",
    )
    op.create_unique_constraint("uq_agents_run_role", "agents", ["run_id", "role"])
