"""Encrypted external-service integration credentials."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.models.base import Base


class Integration(Base):
    """A user-owned GitHub or Vercel credential; plaintext is never persisted."""

    __tablename__ = "integrations"
    __table_args__ = (Index("ix_integrations_user_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    account_label: Mapped[str] = mapped_column(String(100), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
