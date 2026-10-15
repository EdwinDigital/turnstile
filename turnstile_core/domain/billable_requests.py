from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from .models import StrictModel


class BillableRequestPlan(StrictModel):
    operation_key: str = Field(min_length=1, max_length=512)
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_type: Literal["person", "application", "system"]
    scope_id: str = Field(min_length=1, max_length=255)
    period_start: date
    model_id: str = Field(min_length=1, max_length=255)
    model_key: str = Field(min_length=1, max_length=255)
    reserved_tokens: int = Field(ge=1)
    authorization_id: UUID | None = None
    attempt_limit: int = Field(default=1, ge=1, le=32)
    reuse_requires: dict[str, Any] = Field(default_factory=dict)
    admission_authority: Literal["postgresql", "apim_execution"] = "postgresql"

    @field_validator("period_start")
    @classmethod
    def first_day_of_month(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("A budget period must start on the first day of its month")
        return value


class BillableRequestOutcome(StrictModel):
    actual_tokens: StrictInt | None = Field(default=None, ge=0)
    correlation_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class BillableRequestAttempt(StrictModel):
    id: UUID
    operation_key: str
    plan_sha256: str
    scope_type: Literal["person", "application", "system"]
    scope_id: str
    period_start: date
    model_id: str
    model_key: str
    reserved_tokens: int
    authorization_id: UUID | None = None
    attempt_index: int
    state: Literal["started", "exact", "uncertain"]
    actual_tokens: StrictInt | None = Field(default=None, ge=0)
    correlation_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def measured_state(self) -> Self:
        if (self.state == "exact") != (self.actual_tokens is not None):
            raise ValueError("Exact acknowledgements require a measured token total")
        return self


class BillableBudgetExceeded(ValueError):
    pass


class BillableOutcomeUncertain(ValueError):
    pass


def recover_billable_attempt(
    plan: BillableRequestPlan, attempts: Sequence[BillableRequestAttempt],
) -> BillableRequestAttempt:
    matches = [attempt for attempt in attempts if (
        attempt.operation_key == plan.operation_key and attempt.plan_sha256 == plan.plan_sha256
        and attempt.scope_type == plan.scope_type and attempt.scope_id == plan.scope_id
        and attempt.model_id == plan.model_id and attempt.model_key == plan.model_key
        and attempt.authorization_id == plan.authorization_id
        and attempt.state == "exact" and attempt.actual_tokens is not None
        and attempt.evidence.get("admission_authority", "postgresql") == plan.admission_authority
        and all(name in attempt.evidence and attempt.evidence[name] == value
                for name, value in plan.reuse_requires.items())
    )]
    if len(attempts) > 32 or len(matches) != 1:
        raise BillableOutcomeUncertain("Original validated billable evidence is unavailable")
    return matches[0]