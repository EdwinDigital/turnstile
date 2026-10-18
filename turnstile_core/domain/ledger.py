from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .application_access import GatewayApplicationLedgerState
from .budget_completion import (
    completion_transaction,
    utc_time,
    validate_completion,
    validate_rejection_evidence,
    validate_rejection_policy,
    validate_resolution,
)
from .models import ModelPrice, StrictModel, TokenUsageRecord

LedgerScopeType = Literal["person", "application"]
ReservationFinalizationKind = Literal[
    "exact_usage",
    "terminal_zero",
    "unverified_upper_bound",
]
ReservationFinalizationSource = Literal[
    "apim_gateway_llm_log",
    "apim_gateway_log",
    "reservation_timeout",
]


class BudgetReservationFinalization(StrictModel):
    """Append-only evidence that advances one Ledger reservation toward finality.

    Several records may exist for one correlation. A conservative timeout can be
    followed by stronger APIM evidence, while canonical Event Hub usage remains the
    highest-authority source and is stored outside this sidecar.
    """

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
    def validate_finalization(self) -> BudgetReservationFinalization:
        if not self.scope_id.strip() or not self.correlation_id.strip():
            raise ValueError("finalization requires nonblank scope and correlation")
        if self.period_start.day != 1:
            raise ValueError("finalization period must start on the first day of the month")
        if self.evidence_at < self.reservation_created_at:
            raise ValueError("finalization evidence cannot predate the reservation")
        if self.finalization_kind == "exact_usage":
            if self.input_tokens is None or self.output_tokens is None:
                raise ValueError("exact usage requires input and output tokens")
            if self.input_tokens + self.output_tokens != self.total_tokens:
                raise ValueError("exact usage total must equal input plus output")
            if self.source != "apim_gateway_llm_log":
                raise ValueError("exact usage must come from the APIM gateway LLM log")
        elif self.finalization_kind == "terminal_zero":
            if (
                self.input_tokens != 0
                or self.output_tokens != 0
                or self.total_tokens != 0
                or self.status_code is None
                or self.status_code < 400
            ):
                raise ValueError("terminal-zero finalization requires an HTTP failure at zero")
            if self.source != "apim_gateway_log":
                raise ValueError("terminal-zero finalization must come from the APIM gateway log")
        else:
            if self.input_tokens is not None or self.output_tokens is not None:
                raise ValueError("an unverified upper bound has no token classification")
            if self.total_tokens != self.reservation_tokens:
                raise ValueError("an unverified upper bound must retain the full reservation")
            if self.status_code is not None or self.source != "reservation_timeout":
                raise ValueError("an unverified upper bound must come from reservation timeout")
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
        record.input_tokens, record.cached_tokens, record.cache_write_tokens, record.output_tokens,
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


def validate_application_snapshot(
    states: Sequence[GatewayApplicationLedgerState], sidecars: Sequence[Mapping[str, Any]],
) -> None:
    keyed = {(state.period_start, state.application_id): state for state in states}
    if len(keyed) != len(states) or len(sidecars) != len(states):
        raise ValueError("Application ledger snapshots must be complete unique pairs")
    seen = set()
    for sidecar in sidecars:
        key = (sidecar["period_start"], sidecar["application_id"])
        state = keyed.get(key)
        if state is None or key in seen or sidecar["snapshot_at"] != state.snapshot_at:
            raise ValueError("Application ledger snapshot timestamps and identities must match")
        seen.add(key)
        if state.snapshot_at.utcoffset() is None or state.period_start.day != 1:
            raise ValueError("Application ledger snapshot requires a UTC month and aware timestamp")
        for field in ("finalized_upper_bound_tokens", "finalized_upper_bound_count"):
            if type(sidecar[field]) is not int or sidecar[field] < 0:
                raise ValueError("Application ledger upper bounds must be nonnegative integers")
        if state.budget_mode == "tokens":
            if state.token_limit is None or state.available_tokens != max(
                state.token_limit - state.confirmed_tokens - state.pending_reserved_tokens, 0,
            ):
                raise ValueError(
                    "Application ledger snapshot balance does not match its components"
                )
        elif state.available_usd is None or state.available_usd < 0:
            raise ValueError("Application USD ledger balance does not match its components")


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


class BudgetPriceProjection(StrictModel):
    """Current accepted model price projected separately from route publications."""

    version: Literal[1] = 1
    model_id: UUID
    price_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price: ModelPrice | None
    rate_schedules: list[dict[str, int | None]] = Field(default_factory=list)
    updated_at: AwareDatetime

    @staticmethod
    def _rate_units(value: float) -> int:
        scaled = Decimal(str(value)) * Decimal(100_000_000)
        if scaled != scaled.to_integral_value() or scaled < 0:
            raise ValueError("Budget price must have at most eight decimal places")
        return int(scaled)

    @staticmethod
    def _schedules(price: ModelPrice | None) -> list[dict[str, int | None]]:
        if price is None:
            return []
        tiers: list[tuple[int, int | None, float, float, float, float]]
        if price.context_tiers:
            tiers = [(
                tier.min_input_tokens, tier.max_input_tokens, tier.input_price_per_million,
                tier.cached_price_per_million, tier.cache_write_price_per_million,
                tier.output_price_per_million,
            ) for tier in price.context_tiers]
        else:
            tiers = [(
                0, None, price.input_price_per_million, price.cached_price_per_million,
                price.cache_write_price_per_million, price.output_price_per_million,
            )]
        return [{
            "min_input_tokens": minimum, "max_input_tokens": maximum,
            "input_rate_units": BudgetPriceProjection._rate_units(input_rate),
            "cached_rate_units": BudgetPriceProjection._rate_units(cached_rate),
            "cache_write_rate_units": BudgetPriceProjection._rate_units(write_rate),
            "output_rate_units": BudgetPriceProjection._rate_units(output_rate),
        } for minimum, maximum, input_rate, cached_rate, write_rate, output_rate in tiers]

    @staticmethod
    def digest(price: ModelPrice | None, rate_schedules: list[dict[str, int | None]]) -> str:
        encoded = json.dumps(
            {"price": price.model_dump(mode="json") if price is not None else None,
             "rate_schedules": rate_schedules},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def create(
        cls, model_id: UUID, price: ModelPrice | None, updated_at: datetime,
    ) -> BudgetPriceProjection:
        schedules = cls._schedules(price)
        return cls(
            model_id=model_id, price_sha256=cls.digest(price, schedules),
            price=price, rate_schedules=schedules, updated_at=updated_at,
        )

    @model_validator(mode="after")
    def verify_price_digest(self) -> BudgetPriceProjection:
        schedules = self._schedules(self.price)
        if (self.rate_schedules != schedules
            or self.price_sha256 != self.digest(self.price, schedules)):
            raise ValueError("Projected price digest does not match its price snapshot")
        return self


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
    budget_mode: Literal["tokens", "usd"] = Field(
        default="tokens", exclude_if=lambda value: value == "tokens",
    )
    usd_limit: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    enforce: bool = Field(strict=True)
    enabled: bool = Field(default=True, strict=True)
    confirmed_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    active_reserved_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    upper_bound_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    confirmed_usd: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value == 0,
    )
    active_reserved_usd: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value == 0,
    )
    upper_bound_usd: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value == 0,
    )
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_budget_mode(self) -> LedgerSummary:
        if self.budget_mode == "tokens" and self.usd_limit is not None:
            raise ValueError("Token ledger summaries cannot carry a USD limit")
        if self.budget_mode == "usd" and self.usd_limit is None:
            raise ValueError("USD ledger summaries require a USD limit")
        if self.budget_mode == "tokens" and any((
            self.confirmed_usd, self.active_reserved_usd, self.upper_bound_usd,
        )):
            raise ValueError("Token ledger summaries cannot carry USD balances")
        return self

    @property
    def available_tokens(self) -> int:
        return (
            self.token_limit - self.confirmed_tokens
            - self.active_reserved_tokens - self.upper_bound_tokens
        )

    @property
    def available_usd(self) -> Decimal | None:
        if self.budget_mode != "usd" or self.usd_limit is None:
            return None
        return self.usd_limit - self.confirmed_usd - self.active_reserved_usd - self.upper_bound_usd


class LedgerAttempt(StrictModel):
    identity: LedgerAttemptIdentity
    epoch: UUID
    state: Literal[
        "reserved", "dispatching", "uncertain", "settled", "upper_bound", "cancelled", "released",
    ]
    reserved_tokens: int = Field(ge=1, le=2**63 - 1, strict=True)
    budget_mode: Literal["tokens", "usd"] = Field(
        default="tokens", exclude_if=lambda value: value == "tokens",
    )
    reserved_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    actual_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    price_snapshot: BudgetPriceProjection | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    price_etag: str | None = Field(
        default=None, min_length=1, max_length=255, exclude_if=lambda value: value is None,
    )
    actual_tokens: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    evidence_revision: int = Field(default=0, ge=0, strict=True)
    evidence_key: str | None = Field(default=None, min_length=1, max_length=512)
    evidence_rank: Literal[0, 2, 3] = 0
    completion_evidence: dict[str, Any] | None = None
    rejection_policy: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    rejection_evidence: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    resolution_evidence: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_charge(self) -> LedgerAttempt:
        if self.resolution_evidence is not None:
            resolution = validate_resolution(self.resolution_evidence)
            expected = LedgerAdmissionReceipt(
                identity=self.identity, epoch=self.epoch, reserved_tokens=self.reserved_tokens,
                budget_mode=self.budget_mode, reserved_usd=self.reserved_usd,
                price_snapshot=self.price_snapshot,
            )
            if LedgerAdmissionReceipt.model_validate(resolution["receipt"]) != expected or (
                (self.state, self.evidence_rank) not in {("released", 2), ("settled", 3)}
            ):
                raise ValueError("Approved resolution must retain its original admission")
        if self.state == "released" and (
            self.resolution_evidence is None or self.actual_tokens is not None
            or self.evidence_revision == 0
        ):
            raise ValueError("Released reservations retain approval, not fabricated actual usage")
        if self.rejection_policy is not None:
            validate_rejection_policy(self.rejection_policy)
        if self.rejection_evidence is not None:
            validate_rejection_evidence(
                self.rejection_policy, self.rejection_evidence, self.identity.admitted_at,
            )
            if self.state != "settled" or self.evidence_rank not in {2, 3} or (
                self.evidence_rank == 2 and self.actual_tokens != 0
            ):
                raise ValueError("Retained rejection must match its budget decision")
        if self.completion_evidence is not None:
            evidence = validate_completion(self.completion_evidence)
            if ("rejection" in evidence or self.state != "settled" or self.evidence_rank != 3
                or self.actual_tokens != evidence["total_tokens"]):
                raise ValueError("Stored completion must match the settled charge")
        if (self.state == "settled") != (self.actual_tokens is not None):
            raise ValueError("Only settled attempts have an actual charge")
        if self.budget_mode == "usd":
            if self.reserved_usd is None or self.price_snapshot is None:
                raise ValueError("USD attempts require an amount reservation and price snapshot")
            if (self.state == "settled") != (self.actual_usd is not None):
                raise ValueError("Settled USD attempts require an exact amount")
        elif any(value is not None for value in (self.reserved_usd, self.actual_usd,
                             self.price_snapshot, self.price_etag)):
            raise ValueError("Token attempts cannot carry USD evidence")
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
    budget_mode: Literal["tokens", "usd"] = Field(
        default="tokens", exclude_if=lambda value: value == "tokens",
    )
    actual_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    price_snapshot: BudgetPriceProjection | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    evidence_rank: Literal[2, 3] = 3
    resolution_evidence: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_resolution_charge(self) -> LedgerCharge:
        if self.budget_mode == "usd":
            if self.price_snapshot is None or (
                self.resolution_evidence is None and self.actual_usd is None
            ):
                raise ValueError("USD settlement requires measured cost and price evidence")
        elif self.actual_usd is not None or self.price_snapshot is not None:
            raise ValueError("Token settlement cannot carry USD evidence")
        if self.evidence_key.startswith("resolution:") != (self.resolution_evidence is not None):
            raise ValueError("Reservation resolution tasks require their immutable approval")
        if self.resolution_evidence is not None:
            approved = validate_resolution(self.resolution_evidence)
            receipt = LedgerAdmissionReceipt.model_validate(approved["receipt"])
            if (
                self.evidence_rank != 2 or self.total_tokens != 0
                or self.actual_usd is not None
                or self.evidence_key != "resolution:" + approved["id"]
                or receipt.epoch != self.epoch or receipt.identity != self.identity
                or receipt.budget_mode != self.budget_mode
                or receipt.price_snapshot != self.price_snapshot
            ):
                raise ValueError("Resolution task cannot change an approved identity or amount")
        return self


class LedgerAdmissionReceipt(StrictModel):
    identity: LedgerAttemptIdentity
    epoch: UUID
    reserved_tokens: int = Field(ge=1, le=2**63 - 1, strict=True)
    budget_mode: Literal["tokens", "usd"] = Field(
        default="tokens", exclude_if=lambda value: value == "tokens",
    )
    reserved_usd: Decimal | None = Field(
        default=None, ge=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    price_snapshot: BudgetPriceProjection | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_budget_mode(self) -> LedgerAdmissionReceipt:
        if self.budget_mode == "usd":
            if self.reserved_usd is None or self.price_snapshot is None:
                raise ValueError("USD admission receipts require reservation and price evidence")
            if self.price_snapshot.model_id != UUID(self.identity.model_id):
                raise ValueError("USD admission price belongs to another model")
        elif self.reserved_usd is not None or self.price_snapshot is not None:
            raise ValueError("Token admission receipts cannot carry USD evidence")
        return self


class BudgetReservationResolutionWrite(StrictModel):
    id: UUID
    receipt: LedgerAdmissionReceipt
    reason: Literal[
        "gateway_validation_failure", "provider_result_unavailable", "evidence_irrecoverable",
    ]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepts_late_usage: Literal[True]


class BudgetReservationResolution(StrictModel):
    version: Literal[1] = 1
    id: UUID
    receipt: LedgerAdmissionReceipt
    decision: Literal["release_unverified"] = "release_unverified"
    actor: str = Field(min_length=1, max_length=255)
    reason: Literal[
        "gateway_validation_failure", "provider_result_unavailable", "evidence_irrecoverable",
    ]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_at: AwareDatetime

    @model_validator(mode="after")
    def validate_approval(self) -> BudgetReservationResolution:
        validate_resolution(self.model_dump(mode="json"))
        return self


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
            if (
                identity.attempt_key != first.attempt_key
                or identity.model_id != first.model_id
                or identity.admitted_at != first.admitted_at
                or identity.scope.scope_type in scopes
            ):
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
    budget_mode: Literal["tokens", "usd"] = Field(
        default="tokens", exclude_if=lambda value: value == "tokens",
    )
    usd_limit: Decimal | None = Field(
        default=None, gt=0, max_digits=30, decimal_places=14,
        exclude_if=lambda value: value is None,
    )
    enforce: bool
    enabled: bool

    @model_validator(mode="after")
    def validate_mode_limit(self) -> LedgerPolicyUpdate:
        if (self.budget_mode == "tokens") != (self.usd_limit is None):
            raise ValueError("Budget policy limit does not match its mode")
        if self.budget_mode == "usd" and self.token_limit != 0:
            raise ValueError("USD policy cannot carry a token limit")
        return self


class LedgerScopeSeed(StrictModel):
    summary: LedgerSummary
    policy: LedgerPolicyUpdate
    active: bool = False

    @model_validator(mode="after")
    def validate_seed(self) -> LedgerScopeSeed:
        if (
            self.summary.scope != self.policy.scope or self.summary.epoch != self.policy.epoch
            or self.summary.budget_mode != self.policy.budget_mode
            or self.summary.version or self.summary.policy_revision or self.summary.enabled
            or self.summary.token_limit or self.summary.active_reserved_tokens
            or self.summary.upper_bound_tokens
            or self.summary.active_reserved_usd or self.summary.upper_bound_usd
        ):
            raise ValueError(
                "A scope seed must be disabled and preserve one immutable opening balance"
            )
        return self


def update_ledger_policy(
    summary: LedgerSummary, *, epoch: UUID, revision: int, token_limit: int,
    enforce: bool, enabled: bool, now: datetime,
    budget_mode: Literal["tokens", "usd"] = "tokens",
    usd_limit: Decimal | None = None,
) -> LedgerSummary:
    if epoch != summary.epoch or type(revision) is not int or revision < 1:
        raise LedgerProtocolConflict("Budget policy requires its current epoch and revision")
    candidate = LedgerSummary.model_validate({
        **summary.model_dump(), "policy_revision": revision, "token_limit": token_limit,
        "budget_mode": budget_mode, "usd_limit": usd_limit,
        "enforce": enforce, "enabled": enabled,
    })
    if revision < summary.policy_revision:
        return summary
    if revision == summary.policy_revision:
        if candidate != summary:
            raise LedgerProtocolConflict("A budget policy revision cannot change its contents")
        return summary
    return _advance_summary(
        summary, now, policy_revision=revision, token_limit=token_limit,
        budget_mode=budget_mode, usd_limit=usd_limit, enforce=enforce, enabled=enabled,
    )


def _advance_summary(summary: LedgerSummary, now: datetime, **updates: Any) -> LedgerSummary:
    if now.utcoffset() is None or now < summary.updated_at:
        raise LedgerProtocolConflict("Ledger updates require nondecreasing aware timestamps")
    return LedgerSummary.model_validate({
        **summary.model_dump(), **updates, "version": summary.version + 1, "updated_at": now,
    })


def reserve_ledger(
    summary: LedgerSummary, identity: LedgerAttemptIdentity, reserved_tokens: int, now: datetime,
    *, reserved_usd: Decimal | None = None, price_snapshot: BudgetPriceProjection | None = None,
) -> tuple[LedgerSummary, LedgerAttempt]:
    if identity.scope != summary.scope:
        raise LedgerProtocolConflict("Reservation belongs to another budget scope or month")
    if not summary.enabled:
        raise LedgerAdmissionDenied("budget_consumer_disabled")
    if summary.budget_mode == "usd":
        if reserved_usd is None or price_snapshot is None:
            raise LedgerProtocolConflict("USD admission requires a current price snapshot")
        if price_snapshot.model_id != UUID(identity.model_id):
            raise LedgerProtocolConflict("USD price projection belongs to another model")
        if price_snapshot.updated_at > now:
            raise LedgerProtocolConflict("USD price projection is from the future")
        amount = Decimal(reserved_usd)
        if amount < 0:
            raise LedgerProtocolConflict("USD reservation is outside fixed precision")
        with localcontext() as context:
            context.prec = 50
            if amount.quantize(Decimal("0.00000000000001")) != amount:
                raise LedgerProtocolConflict("USD reservation is outside fixed precision")
        if summary.enforce and amount > (summary.available_usd or Decimal(0)):
            unreserved_usd = (
                (summary.usd_limit or Decimal(0)) - summary.confirmed_usd
                - summary.upper_bound_usd
            )
            raise LedgerAdmissionDenied(
                "budget_exhausted" if unreserved_usd <= 0
                else "budget_temporarily_reserved" if amount <= unreserved_usd
                else "budget_request_bound_exceeds_balance"
            )
        attempt = LedgerAttempt(
            identity=identity, epoch=summary.epoch, state="reserved",
            reserved_tokens=reserved_tokens, budget_mode="usd", reserved_usd=amount,
            price_snapshot=price_snapshot, updated_at=now,
        )
        return _advance_summary(
            summary, now, active_reserved_usd=summary.active_reserved_usd + amount,
        ), attempt
    if reserved_usd is not None or price_snapshot is not None:
        raise LedgerProtocolConflict("Token admission cannot carry USD evidence")
    attempt = LedgerAttempt(
        identity=identity, epoch=summary.epoch, state="reserved",
        reserved_tokens=reserved_tokens, updated_at=now,
    )
    if summary.enforce and reserved_tokens > summary.available_tokens:
        unreserved_tokens = (
            summary.token_limit - summary.confirmed_tokens - summary.upper_bound_tokens
        )
        raise LedgerAdmissionDenied(
            "budget_exhausted" if unreserved_tokens <= 0
            else "budget_temporarily_reserved" if reserved_tokens <= unreserved_tokens
            else "budget_request_bound_exceeds_balance"
        )
    return _advance_summary(
        summary, now, active_reserved_tokens=summary.active_reserved_tokens + reserved_tokens,
    ), attempt


def resolve_ledger(
    summary: LedgerSummary, attempt: LedgerAttempt, resolution: dict[str, Any], now: datetime,
) -> tuple[LedgerSummary, LedgerAttempt]:
    try:
        approved = validate_resolution(resolution)
    except (ValueError, TypeError, KeyError) as error:
        raise LedgerProtocolConflict("Reservation resolution evidence is invalid") from error
    expected = LedgerAdmissionReceipt(
        identity=attempt.identity, epoch=attempt.epoch, reserved_tokens=attempt.reserved_tokens,
        budget_mode=attempt.budget_mode, reserved_usd=attempt.reserved_usd,
        price_snapshot=attempt.price_snapshot,
    )
    if (
        LedgerAdmissionReceipt.model_validate(approved["receipt"]) != expected
        or attempt.identity.scope != summary.scope or attempt.epoch != summary.epoch
        or attempt.budget_mode != summary.budget_mode
        or utc_time(approved["approved_at"]) > now
    ):
        raise LedgerProtocolConflict("Reservation resolution does not match its approved admission")
    if attempt.resolution_evidence is not None:
        if validate_resolution(attempt.resolution_evidence) != approved:
            raise LedgerProtocolConflict("An earlier resolution already owns this reservation")
        return summary, attempt
    if attempt.state in {"settled", "cancelled"}:
        return summary, attempt
    if attempt.state not in {"reserved", "dispatching", "uncertain", "upper_bound"}:
        raise LedgerProtocolConflict("Reservation state cannot be resolved")
    active = attempt.state != "upper_bound"
    updates: dict[str, Any]
    if attempt.budget_mode == "usd":
        held = attempt.reserved_usd or Decimal(0)
        with localcontext() as context:
            context.prec = 50
            updates = {
                "active_reserved_usd": summary.active_reserved_usd - (held if active else 0),
                "upper_bound_usd": summary.upper_bound_usd - (0 if active else held),
            }
    else:
        updates = {
            "active_reserved_tokens": summary.active_reserved_tokens
            - (attempt.reserved_tokens if active else 0),
            "upper_bound_tokens": summary.upper_bound_tokens
            - (0 if active else attempt.reserved_tokens),
        }
    updated = _advance_summary(
        summary, now, **updates,
    )
    retained = LedgerAttempt.model_validate({
        **attempt.model_dump(), "state": "released", "actual_tokens": None,
        "evidence_rank": 2, "evidence_revision": max(attempt.evidence_revision, 1),
        "evidence_key": "resolution:" + approved["id"], "resolution_evidence": approved,
        "updated_at": now,
    })
    return updated, retained


def settle_ledger(
    summary: LedgerSummary, attempt: LedgerAttempt, charge: LedgerCharge, now: datetime,
) -> tuple[LedgerSummary, LedgerAttempt]:
    if charge.resolution_evidence is not None:
        return resolve_ledger(summary, attempt, charge.resolution_evidence, now)
    if (
        attempt.epoch != summary.epoch or charge.epoch != summary.epoch
        or attempt.identity != charge.identity or attempt.identity.scope != summary.scope
    ):
        raise LedgerProtocolConflict("Settlement identity, epoch or scope does not match")
    if summary.budget_mode != attempt.budget_mode and attempt.state == "settled":
        if (attempt.evidence_rank != 3 or charge.evidence_rank != 3
            or charge.budget_mode != attempt.budget_mode
            or charge.total_tokens != attempt.actual_tokens
            or charge.actual_usd != attempt.actual_usd
            or charge.price_snapshot != attempt.price_snapshot):
            raise LedgerProtocolConflict(
                "Late settlement conflicts with the original confirmed usage"
            )
        return summary, attempt
    if attempt.budget_mode != summary.budget_mode or charge.budget_mode != summary.budget_mode:
        raise LedgerProtocolConflict("Settlement budget mode changed after admission")
    if summary.budget_mode == "usd" and (
        charge.actual_usd is None or charge.price_snapshot != attempt.price_snapshot
        or attempt.reserved_usd is None
    ):
        raise LedgerProtocolConflict("USD settlement requires the admitted price snapshot")
    if (summary.budget_mode == "usd" and attempt.evidence_rank == 3
        and charge.evidence_rank == 3 and charge.actual_usd != attempt.actual_usd):
        raise LedgerProtocolConflict("Confirmed USD settlement has a conflicting amount")
    if attempt.completion_evidence is not None:
        if charge.evidence_rank == 3 and charge.total_tokens != attempt.actual_tokens:
            raise LedgerProtocolConflict("Durable task conflicts with complete producer evidence")
        return summary, attempt
    if charge.evidence_rank < attempt.evidence_rank:
        return summary, attempt
    if attempt.resolution_evidence is not None and charge.evidence_rank == 2:
        if charge.total_tokens:
            raise LedgerProtocolConflict("A released reservation requires stronger usage evidence")
        return summary, attempt
    if attempt.rejection_evidence is not None and charge.evidence_rank == 2:
        if charge.total_tokens:
            raise LedgerProtocolConflict("Conflicting charge requires stronger usage evidence")
        return summary, attempt
    stronger = charge.evidence_rank > attempt.evidence_rank
    if not stronger and charge.revision < attempt.evidence_revision:
        return summary, attempt
    if not stronger and charge.revision == attempt.evidence_revision:
        if (
            charge.total_tokens != attempt.actual_tokens
            or charge.evidence_key != attempt.evidence_key
        ):
            raise LedgerProtocolConflict("Conflicting settlement at the same evidence revision")
        return summary, attempt
    active = attempt.reserved_tokens if attempt.budget_mode == "tokens" and attempt.state in {
        "reserved", "dispatching", "uncertain",
    } else 0
    upper = (attempt.reserved_tokens if attempt.state == "upper_bound" else 0) \
        if attempt.budget_mode == "tokens" else 0
    updated = _advance_summary(
        summary, now,
        confirmed_tokens=(
            summary.confirmed_tokens + charge.total_tokens - (attempt.actual_tokens or 0)
            if summary.budget_mode == "tokens" else summary.confirmed_tokens
        ),
        active_reserved_tokens=summary.active_reserved_tokens - active,
        upper_bound_tokens=summary.upper_bound_tokens - upper,
        confirmed_usd=(summary.confirmed_usd + (charge.actual_usd or Decimal(0))
                   - (attempt.actual_usd or Decimal(0))
                   if summary.budget_mode == "usd" else summary.confirmed_usd),
        active_reserved_usd=(summary.active_reserved_usd - (
            attempt.reserved_usd or Decimal(0)
            if attempt.state in {"reserved", "dispatching", "uncertain"} else Decimal(0)
        )),
        upper_bound_usd=(summary.upper_bound_usd - (
            attempt.reserved_usd or Decimal(0)
            if attempt.state == "upper_bound" else Decimal(0)
        )),
    )
    receipt = LedgerAttempt.model_validate({
        **attempt.model_dump(), "state": "settled", "actual_tokens": charge.total_tokens,
        **({"actual_usd": charge.actual_usd} if summary.budget_mode == "usd" else {}),
        "evidence_rank": charge.evidence_rank,
        "evidence_revision": max(charge.revision, attempt.evidence_revision),
        "evidence_key": charge.evidence_key,
        "updated_at": now,
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
    allowed = {
        "dispatching": {"reserved"},
        "uncertain": {"dispatching"},
        "upper_bound": {"reserved", "dispatching", "uncertain"},
        "cancelled": {"reserved"},
    }
    if attempt.state not in allowed[state]:
        raise LedgerProtocolConflict("Unknown dispatch outcome cannot release a reservation")
    updates: dict[str, Any] = {}
    if state in {"upper_bound", "cancelled"}:
        if attempt.budget_mode == "tokens":
            updates["active_reserved_tokens"] = (
                summary.active_reserved_tokens - attempt.reserved_tokens
            )
        if attempt.budget_mode == "usd":
            updates["active_reserved_usd"] = summary.active_reserved_usd - (
                attempt.reserved_usd or Decimal(0)
            )
    if state == "upper_bound":
        if attempt.budget_mode == "tokens":
            updates["upper_bound_tokens"] = summary.upper_bound_tokens + attempt.reserved_tokens
        if attempt.budget_mode == "usd":
            updates["upper_bound_usd"] = summary.upper_bound_usd + (
                attempt.reserved_usd or Decimal(0)
            )
    updated = _advance_summary(summary, now, **updates)
    receipt = LedgerAttempt.model_validate({
        **attempt.model_dump(), "state": state, "updated_at": now,
    })
    return updated, receipt
