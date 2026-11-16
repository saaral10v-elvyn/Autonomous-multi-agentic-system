"""add encrypted M7 integrations"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d7a18f46c2b1"
down_revision: str | None = "c20f319a54de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("token_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("account_label", sa.String(length=100), nullable=False),
        sa.Column("scopes", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_integrations_user_id", "integrations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_integrations_user_id", table_name="integrations")
    op.drop_table("integrations")
