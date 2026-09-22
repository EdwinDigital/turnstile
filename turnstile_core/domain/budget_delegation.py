from __future__ import annotations

from uuid import UUID

from pydantic import Field, HttpUrl, model_validator

from .models import StrictModel


class BudgetDelegationTarget(StrictModel):
    audience: str = Field(min_length=1, max_length=2048)
    allowed_origins: tuple[HttpUrl, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> BudgetDelegationTarget:
        if any(character.isspace() for character in self.audience):
            raise ValueError("Delegation requires one exact token audience")
        for origin in self.allowed_origins:
            if (
                origin.scheme != "https"
                or origin.username
                or origin.password
                or origin.path not in {None, "/"}
                or origin.query
                or origin.fragment
            ):
                raise ValueError("Delegation requires exact credential-free HTTPS origins")
        return self


class BudgetDelegationTrust(StrictModel):
    tenant_id: UUID
    audience: str = Field(min_length=1, max_length=2048)
    client_ids: tuple[UUID, ...] = Field(min_length=1)
    object_ids: tuple[UUID, ...] = Field(min_length=1)
