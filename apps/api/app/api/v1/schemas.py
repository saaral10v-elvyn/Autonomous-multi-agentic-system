"""Public API request and response models."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.llm.types import Provider

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CredentialsRequest(BaseModel):
    """Signup or login input; passwords are masked in representations."""

    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[SecretStr, Field(min_length=8, max_length=256)]

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not _EMAIL_PATTERN.fullmatch(value.strip()):
            raise ValueError("Invalid email address.")
        return value.strip()


class UserResponse(BaseModel):
    """Safe public user representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime


class ProviderKeyCreateRequest(BaseModel):
    """Incoming provider key; never returned by a response model."""

    provider: Provider
    label: Annotated[str, Field(min_length=1, max_length=100)]
    api_key: Annotated[SecretStr, Field(min_length=8, max_length=8192)]

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Label is required.")
        return normalized

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API key is required.")
        return value


class ProviderKeyResponse(BaseModel):
    """Write-only credential metadata; the stored key is never serialized."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: Provider
    label: str
    last4: str
    created_at: datetime


class ProviderKeyTestResponse(BaseModel):
    """Sanitized provider connectivity and usage result."""

    ok: bool
    provider: Provider
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
