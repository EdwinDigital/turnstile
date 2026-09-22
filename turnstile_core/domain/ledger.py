from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .budget_completion import completion_transaction, validate_completion
from .models import StrictModel, TokenUsageRecord

LedgerScopeType = Literal["person", "application"]
ReservationFinalizationKind = Literal["exact_usage", "terminal_zero", "unverified_upper_bound"]
ReservationFinalizationSource = Literal[
    "apim_gateway_llm_log", "apim_gateway_log", "reservation_timeout"
]


class BudgetReservationFinalization(StrictModel):
    scope_type: LedgerScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    period_start: date
    correlation_id: str = Field(min_length=1, max_length=255)
    reservation_created_at: AwareDatetime
    reservation_tokens: int = Field(ge=0, strict=True)
    evidence_at: AwareDatetime
    status_code: int | None = Field(default=None, ge=0, le=599, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)
    finalization_kind: ReservationFinalizationKind
    source: ReservationFinalizationSource

    @model_validator(mode="after")
    def validate_evidence(self) -> BudgetReservationFinalization:
        if not self.scope_id.strip() or not self.correlation_id.strip():
            raise ValueError("scope and correlation identifiers must not be blank")
        if self.period_start.day != 1:
            raise ValueError("ledger period must start on the first day of the month")
        if self.evidence_at < self.reservation_created_at:
            raise ValueError("evidence cannot predate the reservation")
        if self.finalization_kind == "exact_usage":
            if (
                self.source != "apim_gateway_llm_log"
                or self.input_tokens is None
                or self.output_tokens is None
                or self.total_tokens != self.input_tokens + self.output_tokens
            ):
                raise ValueError("exact usage requires measured input and output from the LLM log")
        elif self.finalization_kind == "terminal_zero":
            if (
                self.source != "apim_gateway_log"
                or self.status_code is None
                or self.status_code < 400
                or (self.input_tokens, self.output_tokens, self.total_tokens) != (0, 0, 0)
            ):
                raise ValueError("terminal zero requires a failed request with zero usage")
        elif (
            self.source != "reservation_timeout"
            or self.input_tokens is not None
            or self.output_tokens is not None
            or self.status_code is not None
            or self.total_tokens != self.reservation_tokens
        ):
            raise ValueError("an unverified upper bound must retain the full reservation")
        return self


class BudgetReservationAdmission(StrictModel):
    scope_type: LedgerScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    correlation_id: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime
    reserved_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_identity(self) -> BudgetReservationAdmission:
        if not self.scope_id.strip() or not self.correlation_id.strip():
            raise ValueError("Reservation admission requires nonblank identity")
        return self


def strict_budget_evidence(admitted_at: datetime, effective_at: datetime | None) -> bool:
    if effective_at is None:
        return False
    if admitted_at.utcoffset() is None or effective_at.utcoffset() is None:
        raise ValueError("Budget evidence timestamps must include a timezone")
    return datetime.now(UTC) >= effective_at and admitted_at >= effective_at


def canonical_budget_tokens(record: TokenUsageRecord, *, reconciled: bool = False) -> int | None:
    buckets = (
        record.input_tokens,
        record.cached_tokens,
        record.cache_write_tokens,
        record.output_tokens,
    )
    if (
        record.usage_domain != "apim"
        or not record.correlation_id.strip()
        or record.ts.utcoffset() is None
        or any(type(value) is not int or value < 0 for value in buckets)
        or record.cache_write_tokens > record.cached_tokens
    ):
        return None
    if not record.estimated and record.ingest_error is None:
        return record.input_tokens + record.cached_tokens + record.output_tokens
    if reconciled and record.ingest_error == "stream_cache_usage_unavailable":
        return record.input_tokens + record.output_tokens
    return None


class LedgerProtocolConflict(ValueError):
    pass


class LedgerAdmissionDenied(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LedgerScope(StrictModel):
    scope_type: LedgerScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    period_start: date

    @model_validator(mode="after")
    def validate_scope(self) -> LedgerScope:
        if not self.scope_id.strip() or self.period_start.day != 1:
            raise ValueError("Ledger scope requires an identity and a UTC calendar month")
        if self.scope_type == "application":
            UUID(self.scope_id)
        return self

    @property
    def partition(self) -> str:
        identity = hashlib.sha256(self.scope_id.encode()).hexdigest()
        return f"budget2|{self.scope_type}|{identity}|{self.period_start:%Y-%m}"


class LedgerAttemptIdentity(StrictModel):
    scope: LedgerScope
    gateway_profile_id: UUID
    native_gateway_id: str = Field(min_length=1, max_length=80)
    correlation_id: str = Field(min_length=1, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    admitted_at: AwareDatetime

    @model_validator(mode="after")
    def validate_attempt(self) -> LedgerAttemptIdentity:
        moment = self.admitted_at.astimezone(UTC)
        if self.scope.period_start != date(moment.year, moment.month, 1):
            raise ValueError("Ledger month must be the UTC admission month")
        if not all(value.strip() for value in (
            self.native_gateway_id, self.correlation_id, self.model_id,
        )):
            raise ValueError("Ledger attempt identity cannot be blank")
        return self

    @property
    def attempt_key(self) -> str:
        values = [str(self.gateway_profile_id), self.native_gateway_id, self.correlation_id]
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    @property
    def row_key(self) -> str:
        return "A|" + self.attempt_key


class LedgerSummary(StrictModel):
    protocol_version: Literal[2] = 2
    epoch: UUID
    scope: LedgerScope
    version: int = Field(default=0, ge=0, strict=True)
    policy_revision: int = Field(default=0, ge=0, strict=True)
    token_limit: int = Field(ge=0, le=2**63 - 1, strict=True)
    enforce: bool = Field(strict=True)
    enabled: bool = Field(default=True, strict=True)
    confirmed_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    active_reserved_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    upper_bound_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    updated_at: AwareDatetime

    @property
    def available_tokens(self) -> int:
        return (self.token_limit - self.confirmed_tokens - self.active_reserved_tokens
                - self.upper_bound_tokens)


class LedgerAttempt(StrictModel):
    identity: LedgerAttemptIdentity
    epoch: UUID
    state: Literal["reserved", "dispatching", "uncertain", "settled", "upper_bound", "cancelled"]
    reserved_tokens: int = Field(ge=1, le=2**63 - 1, strict=True)
    actual_tokens: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    evidence_revision: int = Field(default=0, ge=0, strict=True)
    evidence_key: str | None = Field(default=None, min_length=1, max_length=512)
    evidence_rank: Literal[0, 2, 3] = 0
    completion_evidence: dict[str, Any] | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_charge(self) -> LedgerAttempt:
        if self.completion_evidence is not None:
            evidence = validate_completion(self.completion_evidence)
            if (self.state != "settled" or self.evidence_rank != 3
                or self.actual_tokens != evidence["total_tokens"]):
                raise ValueError("Stored completion must match the settled charge")
        if (self.state == "settled") != (self.actual_tokens is not None):
            raise ValueError("Only settled attempts have an actual charge")
        if (self.evidence_revision > 0) != (self.evidence_key is not None):
            raise ValueError("Evidence revision and identity must be recorded together")
        if self.state == "settled" and self.evidence_revision == 0:
            raise ValueError("Settlement requires durable evidence")
        if self.updated_at < self.identity.admitted_at:
            raise ValueError("Attempt update cannot precede admission")
        return self


class LedgerCharge(StrictModel):
    identity: LedgerAttemptIdentity
    epoch: UUID
    revision: int = Field(ge=1, strict=True)
    evidence_key: str = Field(min_length=1, max_length=512)
    total_tokens: int = Field(ge=0, le=2**63 - 1, strict=True)
    evidence_rank: Literal[2, 3] = 3


class LedgerAdmissionReceipt(StrictModel):
    identity: LedgerAttemptIdentity
    epoch: UUID
    reserved_tokens: int = Field(ge=1, le=2**63 - 1, strict=True)


class LedgerSettlementTask(StrictModel):
    id: int = Field(ge=1, strict=True)
    charge: LedgerCharge
    lease_token: UUID
    lease_until: AwareDatetime
    attempts: int = Field(ge=1, strict=True)
    created_at: AwareDatetime


class LedgerDispatch(StrictModel):
    owner: UUID
    caller_request_id: str = Field(default="", max_length=255)
    receipts: list[LedgerAdmissionReceipt] = Field(min_length=1, max_length=2)
    state: Literal["preparing", "dispatching", "cancelled"] = "preparing"
    recovery_complete: bool = False
    counting_basis: dict[str, Any] = Field(default_factory=dict)
    expires_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_dispatch(self) -> LedgerDispatch:
        first = self.receipts[0].identity
        if len(self.receipts) == 2 and first.scope.scope_type != "person":
            raise ValueError("Dual-scope dispatch uses the person partition as its coordinator")
        scopes = set()
        for receipt in self.receipts:
            identity = receipt.identity
            if (identity.attempt_key != first.attempt_key or identity.model_id != first.model_id
                or identity.admitted_at != first.admitted_at
                or identity.scope.scope_type in scopes):
                raise ValueError("A dispatch must bind one attempt and distinct budget scope types")
            scopes.add(identity.scope.scope_type)
        if not first.admitted_at < self.expires_at <= first.admitted_at + timedelta(seconds=60):
            raise ValueError("Dispatch preparation requires a bounded admission lease")
        if self.updated_at < first.admitted_at:
            raise ValueError("Dispatch cannot precede admission")
        return self

    @property
    def scope(self) -> LedgerScope:
        return self.receipts[0].identity.scope

    @property
    def row_key(self) -> str:
        return "D|" + self.receipts[0].identity.attempt_key


class LedgerRecoveryCursor(StrictModel):
    scope: LedgerScope
    epoch: UUID
    next_partition: str | None = None
    next_row: str | None = None
    version: int = Field(ge=0, strict=True)


class LedgerPolicyUpdate(StrictModel):
    scope: LedgerScope
    epoch: UUID
    revision: int = Field(ge=1, strict=True)
    token_limit: int = Field(ge=0, le=2**63 - 1, strict=True)
    enforce: bool
    enabled: bool


class LedgerScopeSeed(StrictModel):
    summary: LedgerSummary
    policy: LedgerPolicyUpdate
    active: bool = False

    @model_validator(mode="after")
    def validate_seed(self) -> LedgerScopeSeed:
        if (self.summary.scope != self.policy.scope or self.summary.epoch != self.policy.epoch
            or self.summary.version or self.summary.policy_revision or self.summary.enabled
            or self.summary.token_limit or self.summary.active_reserved_tokens
            or self.summary.upper_bound_tokens):
            raise ValueError(
                "A scope seed must be disabled and preserve one immutable opening balance"
            )
        return self


def _advance_summary(summary: LedgerSummary, now: datetime, **updates: Any) -> LedgerSummary:
    if now.utcoffset() is None or now < summary.updated_at:
        raise LedgerProtocolConflict("Ledger updates require nondecreasing aware timestamps")
    return LedgerSummary.model_validate({
        **summary.model_dump(), **updates, "version": summary.version + 1, "updated_at": now,
    })


def update_ledger_policy(
    summary: LedgerSummary, *, epoch: UUID, revision: int, token_limit: int,
    enforce: bool, enabled: bool, now: datetime,
) -> LedgerSummary:
    if epoch != summary.epoch or type(revision) is not int or revision < 1:
        raise LedgerProtocolConflict("Budget policy requires its current epoch and revision")
    candidate = LedgerSummary.model_validate({
        **summary.model_dump(), "policy_revision": revision, "token_limit": token_limit,
        "enforce": enforce, "enabled": enabled,
    })
    if revision < summary.policy_revision:
        return summary
    if revision == summary.policy_revision:
        if candidate != summary:
            raise LedgerProtocolConflict("A budget policy revision cannot change its contents")
        return summary
    return _advance_summary(summary, now, policy_revision=revision, token_limit=token_limit,
                            enforce=enforce, enabled=enabled)


def reserve_ledger(
    summary: LedgerSummary, identity: LedgerAttemptIdentity, reserved_tokens: int, now: datetime,
) -> tuple[LedgerSummary, LedgerAttempt]:
    if identity.scope != summary.scope:
        raise LedgerProtocolConflict("Reservation belongs to another budget scope or month")
    attempt = LedgerAttempt(identity=identity, epoch=summary.epoch, state="reserved",
                            reserved_tokens=reserved_tokens, updated_at=now)
    if not summary.enabled:
        raise LedgerAdmissionDenied("budget_consumer_disabled")
    if summary.enforce and reserved_tokens > summary.available_tokens:
        unreserved = summary.token_limit - summary.confirmed_tokens - summary.upper_bound_tokens
        raise LedgerAdmissionDenied(
            "budget_exhausted" if unreserved <= 0
            else "budget_temporarily_reserved" if reserved_tokens <= unreserved
            else "budget_request_bound_exceeds_balance"
        )
    return _advance_summary(
        summary, now, active_reserved_tokens=summary.active_reserved_tokens + reserved_tokens,
    ), attempt


def settle_ledger(
    summary: LedgerSummary, attempt: LedgerAttempt, charge: LedgerCharge, now: datetime,
) -> tuple[LedgerSummary, LedgerAttempt]:
    if (attempt.epoch != summary.epoch or charge.epoch != summary.epoch
        or attempt.identity != charge.identity or attempt.identity.scope != summary.scope):
        raise LedgerProtocolConflict("Settlement identity, epoch or scope does not match")
    if attempt.completion_evidence is not None:
        if charge.evidence_rank == 3 and charge.total_tokens != attempt.actual_tokens:
            raise LedgerProtocolConflict("Durable task conflicts with complete producer evidence")
        return summary, attempt
    if charge.revision < attempt.evidence_revision:
        return summary, attempt
    if charge.revision == attempt.evidence_revision:
        if (charge.total_tokens != attempt.actual_tokens
            or charge.evidence_key != attempt.evidence_key):
            raise LedgerProtocolConflict("Conflicting settlement at the same evidence revision")
        return summary, attempt
    active = attempt.reserved_tokens if attempt.state in {
        "reserved", "dispatching", "uncertain",
    } else 0
    upper = attempt.reserved_tokens if attempt.state == "upper_bound" else 0
    updated = _advance_summary(
        summary, now,
        confirmed_tokens=(
            summary.confirmed_tokens + charge.total_tokens - (attempt.actual_tokens or 0)
        ),
        active_reserved_tokens=summary.active_reserved_tokens - active,
        upper_bound_tokens=summary.upper_bound_tokens - upper,
    )
    receipt = LedgerAttempt.model_validate({
        **attempt.model_dump(), "state": "settled", "actual_tokens": charge.total_tokens,
        "evidence_rank": charge.evidence_rank, "evidence_revision": charge.revision,
        "evidence_key": charge.evidence_key, "updated_at": now,
    })
    return updated, receipt


def complete_ledger(
    summary: LedgerSummary, attempt: LedgerAttempt, receipt: LedgerAdmissionReceipt,
    completion: dict[str, Any], now: datetime,
) -> tuple[LedgerSummary, LedgerAttempt]:
    summary_json, attempt_json = summary.model_dump(mode="json"), attempt.model_dump(mode="json")
    try:
        updated, settled = completion_transaction(
            summary_json, attempt_json, receipt.model_dump(mode="json"), completion, now,
        )
    except ValueError as error:
        raise LedgerProtocolConflict(str(error)) from error
    if updated is summary_json:
        return summary, attempt
    return LedgerSummary.model_validate(updated), LedgerAttempt.model_validate(settled)


def transition_ledger_attempt(
    summary: LedgerSummary, attempt: LedgerAttempt,
    state: Literal["dispatching", "uncertain", "upper_bound", "cancelled"], now: datetime,
) -> tuple[LedgerSummary, LedgerAttempt]:
    if attempt.epoch != summary.epoch or attempt.identity.scope != summary.scope:
        raise LedgerProtocolConflict("Attempt does not belong to this ledger epoch")
    if state == attempt.state:
        return summary, attempt
    allowed = {"dispatching": {"reserved"}, "uncertain": {"dispatching"},
               "upper_bound": {"reserved", "dispatching", "uncertain"}, "cancelled": {"reserved"}}
    if attempt.state not in allowed[state]:
        raise LedgerProtocolConflict("Unknown dispatch outcome cannot release a reservation")
    updates = {}
    if state in {"upper_bound", "cancelled"}:
        updates["active_reserved_tokens"] = summary.active_reserved_tokens - attempt.reserved_tokens
    if state == "upper_bound":
        updates["upper_bound_tokens"] = summary.upper_bound_tokens + attempt.reserved_tokens
    updated = _advance_summary(summary, now, **updates)
    receipt = LedgerAttempt.model_validate({
        **attempt.model_dump(), "state": state, "updated_at": now,
    })
    return updated, receipt
