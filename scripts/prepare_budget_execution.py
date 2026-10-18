from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from turnstile_core.domain.budget_counting import ModelCountContract
from turnstile_core.domain.budget_delegation import BudgetDelegationTrust
from turnstile_core.domain.ledger import LedgerSummary
from turnstile_core.domain.models import StrictModel
from turnstile_core.integrations.apim_budget_execution import (
    budget_execution_fence,
    compose_budget_execution_parent,
)
from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy


class FenceConfiguration(StrictModel):
    protocol_version: Literal[2] = 2
    gateway_profile_id: UUID
    apim_service_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    epoch: UUID
    mode: Literal["prepared", "paused", "active"] = "prepared"
    gateways: dict[str, list[str]] = Field(min_length=1)
    model_contracts: dict[str, ModelCountContract] = Field(min_length=1)
    delegation: BudgetDelegationTrust | None = None
    validation_revision: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    validation_application_id: UUID | None = None
    validation_subscription_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,126}$"
    )
    validation_person_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_routes(self) -> FenceConfiguration:
        if (self.validation_revision is None) != (self.validation_application_id is None):
            raise ValueError("Candidate validation requires one exact revision and Application")
        if (self.validation_revision is None) != (self.validation_subscription_id is None):
            raise ValueError("Candidate validation must fence the same trusted subscription")
        if self.validation_revision is not None and self.mode not in {"prepared", "paused"}:
            raise ValueError("Candidate validation is separate from global activation")
        if self.validation_person_id is not None and (
            self.mode != "paused" or self.validation_revision is None
        ):
            raise ValueError("Two-scope validation requires paused legacy admission")
        if any(
            not gateway.strip() or not revisions or any(not item.strip() for item in revisions)
            for gateway, revisions in self.gateways.items()
        ):
            raise ValueError("Every native Gateway requires explicit compatible revisions")
        return self

    @property
    def partition(self) -> str:
        service = hashlib.sha256(self.apim_service_id.lower().encode()).hexdigest()
        return f"budget-protocol|{service}|{self.api_id}"


class CutoverScope(StrictModel):
    summary: LedgerSummary
    legacy_active_reservations: int = Field(ge=0, strict=True)
    legacy_upper_bound_rows: int = Field(ge=0, strict=True)
    legacy_confirmed_tokens: int = Field(ge=0, strict=True)
    legacy_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime


class CutoverReadback(StrictModel):
    observed_at: AwareDatetime
    old_admission_fenced: bool = Field(strict=True)
    all_gateway_revisions_verified: bool = Field(strict=True)
    old_inflight_drained: bool = Field(strict=True)
    compatible_readers_verified: bool = Field(strict=True)
    scopes: list[CutoverScope] = Field(min_length=1)

    def summaries(self, epoch: UUID, now: datetime) -> list[LedgerSummary]:
        if not all(
            (
                self.old_admission_fenced,
                self.all_gateway_revisions_verified,
                self.old_inflight_drained,
                self.compatible_readers_verified,
            )
        ):
            raise ValueError(
                "Cutover requires observed old-writer fencing and drained in-flight work"
            )
        if not 0 <= (now - self.observed_at).total_seconds() <= 30:
            raise ValueError("Cutover readback must be no older than 30 seconds")
        seen = set()
        for item in self.scopes:
            state = item.summary
            if state.scope.partition in seen or state.epoch != epoch or state.version != 0:
                raise ValueError(
                    "Cutover cannot duplicate a logical scope or replace an existing epoch"
                )
            seen.add(state.scope.partition)
            if item.legacy_active_reservations or item.legacy_upper_bound_rows:
                raise ValueError("Legacy outstanding charges require a separately reviewed drain")
            if (
                state.confirmed_tokens != item.legacy_confirmed_tokens
                or state.active_reserved_tokens
                or state.upper_bound_tokens
            ):
                raise ValueError(
                    "Cutover must preserve the drained legacy confirmed balance exactly"
                )
            if not 0 <= (now - item.observed_at).total_seconds() <= 30:
                raise ValueError("Every scope requires a fresh independent readback")
        return [item.summary for item in self.scopes]


def render_candidate(template: str, *, delegation: BudgetDelegationTrust | None = None) -> str:
    return compose_budget_execution_parent(template, template, enabled=True, delegation=delegation)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render default-off budget artifacts; no cloud access"
    )
    parser.add_argument(
        "--template", type=Path, default=Path("infra/policies/foundry-finops-policy.xml")
    )
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    configuration = FenceConfiguration.model_validate_json(arguments.configuration.read_text())
    if configuration.mode != "prepared":
        raise ValueError("Offline preparation can only produce a disabled fence configuration")
    output: Path = arguments.output
    output.mkdir(parents=True, exist_ok=False)
    compiled = render_candidate(arguments.template.read_text(), delegation=configuration.delegation)
    (output / "candidate-parent.xml").write_text(compiled)
    fence = serialize_policy(budget_execution_fence([configuration.api_id]))
    parse_policy(fence)
    (output / "shared-fence-fragment.xml").write_text(fence)
    entity = {
        "PartitionKey": configuration.partition,
        "RowKey": "F",
        "Document": configuration.model_dump_json(),
    }
    (output / "prepared-fence-entity.json").write_text(json.dumps(entity, indent=2) + "\n")
    print(
        json.dumps(
            {
                "cloud_operations": 0,
                "mode": "prepared",
                "candidate_sha256": hashlib.sha256(compiled.encode()).hexdigest(),
                "scope_initialization": "requires separately authorized fresh cloud readback",
            }
        )
    )


if __name__ == "__main__":
    main()
