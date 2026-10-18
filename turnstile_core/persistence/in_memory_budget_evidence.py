from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from ..domain.application_access import UsageApplicationAttribution
from ..domain.billable_requests import BillableRequestAttempt
from ..domain.ledger import (
    BudgetReservationAdmission,
    BudgetReservationFinalization,
    canonical_budget_tokens,
    strict_budget_evidence,
)
from ..domain.models import TokenUsageRecord


class InMemoryBudgetEvidenceRepositoryMixin:
    evidence_policy_effective_at: datetime | None
    budget_reservation_admissions: dict[tuple[str, str, str], BudgetReservationAdmission]
    budget_evidence_conflicts: set[tuple[str, str, str, int, int]]
    budget_evidence_received_at: dict[str, datetime]
    reconciled_usage_ids: set[str]
    billable_requests: dict[UUID, BillableRequestAttempt]
    usage_records: list[TokenUsageRecord]
    usage_application_attributions: dict[str, UsageApplicationAttribution]
    budget_reservation_finalizations: dict[
        tuple[str, str, str, str, str, datetime], BudgetReservationFinalization
    ]

    def budget_evidence_effective_at(self) -> datetime | None:
        return self.evidence_policy_effective_at

    def reservation_evidence_correlations(
        self, scope_type: str, scope_id: str, request_ids: Sequence[str],
    ) -> dict[str, str]:
        return {
            str(attempt.id): attempt.correlation_id
            for attempt in self.billable_requests.values()
            if str(attempt.id) in request_ids and attempt.scope_type == scope_type
            and attempt.scope_id == scope_id and attempt.correlation_id is not None
            and strict_budget_evidence(attempt.created_at, self.evidence_policy_effective_at)
        }

    def register_budget_reservations(self, items: Sequence[BudgetReservationAdmission]) -> None:
        pending: dict[tuple[str, str, str], BudgetReservationAdmission] = {}
        for item in items:
            key = (item.scope_type, item.scope_id, item.correlation_id)
            existing = pending.get(key, self.budget_reservation_admissions.get(key))
            if existing is not None and existing != item:
                raise ValueError("Budget reservation admission is immutable")
            pending[key] = item
        self.budget_reservation_admissions.update(pending)

    def _budget_bound_attempt(
        self, scope_type: str, scope_id: str, correlation_id: str,
    ) -> BillableRequestAttempt | None:
        candidates = [
            attempt for attempt in self.billable_requests.values()
            if attempt.scope_type == scope_type and attempt.scope_id == scope_id
            and (str(attempt.id) == correlation_id or (
                attempt.correlation_id == correlation_id
                and strict_budget_evidence(attempt.created_at, self.evidence_policy_effective_at)
            ))
        ]
        if len(candidates) > 1:
            raise ValueError("Budget reservation attempt binding is ambiguous")
        return candidates[0] if candidates else None

    def _budget_admitted_at(
        self, scope_type: str, scope_id: str, correlation_id: str, fallback: datetime,
    ) -> datetime:
        attempt = self._budget_bound_attempt(scope_type, scope_id, correlation_id)
        if attempt is not None:
            return attempt.created_at
        admission = self.budget_reservation_admissions.get((scope_type, scope_id, correlation_id))
        if admission is not None:
            return admission.created_at
        return next((
            item.reservation_created_at for item in self.budget_reservation_finalizations.values()
            if item.scope_type == scope_type and item.scope_id == scope_id
            and item.correlation_id == correlation_id
        ), fallback)

    def _budget_record_identity(
        self, record: TokenUsageRecord, scope_type: str, scope_id: str,
    ) -> tuple[str, datetime]:
        for attempt in self.billable_requests.values():
            if (
                record.request_id == str(attempt.id) and record.model_id == attempt.model_id
                and record.correlation_id == attempt.correlation_id
                and attempt.scope_type == scope_type and attempt.scope_id == scope_id
            ):
                return str(attempt.id), attempt.created_at
        return record.correlation_id, self._budget_admitted_at(
            scope_type, scope_id, record.correlation_id, record.ts,
        )

    def _strict_budget_record(
        self, record: TokenUsageRecord, scope_type: str, scope_id: str,
    ) -> bool:
        return strict_budget_evidence(
            self._budget_record_identity(record, scope_type, scope_id)[1],
            self.evidence_policy_effective_at,
        )

    def _strict_budget_choices(
        self, scope_type: str, scope_id: str,
    ) -> dict[str, tuple[datetime, int, int]]:
        choices: dict[str, tuple[datetime, int, int]] = {}
        received: dict[str, datetime] = {}

        def accept(
            identity: str, admitted: datetime, tokens: int, rank: int, received_at: datetime,
        ) -> None:
            if not strict_budget_evidence(admitted, self.evidence_policy_effective_at):
                return
            previous = choices.get(identity)
            if previous is not None and previous[1] != tokens:
                self.budget_evidence_conflicts.add((
                    scope_type, scope_id, identity, previous[1], tokens,
                ))
            if previous is None or rank > previous[2] or (
                rank == previous[2] and received_at < received[identity]
            ):
                choices[identity] = (admitted, tokens, rank)
                received[identity] = received_at

        for attempt in self.billable_requests.values():
            if (
                attempt.scope_type == scope_type and attempt.scope_id == scope_id
                and attempt.actual_tokens is not None
            ):
                accept(
                    str(attempt.id), attempt.created_at, attempt.actual_tokens,
                    2, attempt.updated_at,
                )
        for item in self.budget_reservation_finalizations.values():
            if (
                item.scope_type == scope_type and item.scope_id == scope_id
                and item.finalization_kind != "unverified_upper_bound"
            ):
                bound_attempt = self._budget_bound_attempt(
                    scope_type, scope_id, item.correlation_id,
                )
                identity = (
                    str(bound_attempt.id) if bound_attempt is not None else item.correlation_id
                )
                accept(identity, self._budget_admitted_at(
                    scope_type, scope_id, item.correlation_id, item.reservation_created_at,
                ), item.total_tokens, 1, self.budget_evidence_received_at[
                    self._finalization_evidence_key(item)
                ])
        for record in self.usage_records:
            attribution = self.usage_application_attributions.get(record.id)
            matches = (
                record.user_id == scope_id if scope_type == "person"
                else attribution is not None and str(attribution.application_id) == scope_id
            )
            if scope_type == "system":
                matches = record.request_source == "gateway-publication-probe" and any(
                    record.request_id == str(attempt.id) and record.model_id == attempt.model_id
                    and record.correlation_id == attempt.correlation_id
                    and attempt.scope_type == scope_type and attempt.scope_id == scope_id
                    for attempt in self.billable_requests.values()
                )
            if not matches:
                continue
            tokens = canonical_budget_tokens(
                record, reconciled=record.id in self.reconciled_usage_ids,
            )
            if tokens is not None:
                identity, admitted = self._budget_record_identity(record, scope_type, scope_id)
                rank = 3 if not record.estimated and record.ingest_error is None else 1
                evidence_key = ("usage:" if rank == 3 else "reconciled:") + record.id
                accept(identity, admitted, tokens, rank,
                       self.budget_evidence_received_at.get(evidence_key, record.ts))
        return choices

    def _finalization_evidence_key(self, item: BudgetReservationFinalization) -> str:
        key = (
            f"{item.scope_type}:{item.scope_id}:{item.correlation_id}:"
            f"{item.finalization_kind}:{item.source}:{item.evidence_at.isoformat()}"
        )
        self.budget_evidence_received_at.setdefault(key, datetime.now(UTC))
        return key