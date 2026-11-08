"""Public API request and response models."""

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.llm.types import Provider


class AutonomyLevel(StrEnum):
    """User-selected autonomy preset for a run."""

    STEP_BY_STEP = "step_by_step"
    MILESTONES = "milestones"
    AUTOPILOT = "autopilot"


class RunCreateRequest(BaseModel):
    """Initial run request accepted by the M2 API."""

    idea: Annotated[str, Field(min_length=1, max_length=20_000)]
    autonomy_level: AutonomyLevel
    budget_usd: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]

    @field_validator("idea")
    @classmethod
    def validate_idea(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idea is required.")
        return normalized


class RunResponse(BaseModel):
    """Safe run representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    idea: str
    autonomy_level: AutonomyLevel
    status: str
    budget_usd: Decimal
    spent_usd: Decimal
    created_at: datetime


class RunEventResponse(BaseModel):
    """Serialized durable run event."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    seq: int
    type: str
    payload: dict[str, object]
    created_at: datetime


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
