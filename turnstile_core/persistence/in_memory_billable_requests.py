from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from ..domain.application_access import UsageApplicationAttribution
from ..domain.billable_requests import (
    BillableBudgetExceeded,
    BillableOutcomeUncertain,
    BillableRequestAttempt,
    BillableRequestOutcome,
    BillableRequestPlan,
    recover_billable_attempt,
)
from ..domain.ledger import (
    BudgetReservationFinalization,
    canonical_budget_tokens,
    strict_budget_evidence,
)
from ..domain.models import TokenUsageRecord
from .in_memory_budget_evidence import InMemoryBudgetEvidenceRepositoryMixin


class InMemoryBillableRequestRepositoryMixin(InMemoryBudgetEvidenceRepositoryMixin):
    evidence_policy_effective_at: datetime | None
    reconciled_usage_ids: set[str]
    billable_requests: dict[UUID, BillableRequestAttempt]
    billable_request_lock: threading.RLock
    usage_records: list[TokenUsageRecord]
    usage_application_attributions: dict[str, UsageApplicationAttribution]
    budget_reservation_finalizations: dict[
        tuple[str, str, str, str, str, datetime], BudgetReservationFinalization
    ]
    gateway_application_budgets: dict[tuple[date, UUID], dict[str, Any]]

    def _billable_canonical_tokens(self, attempt: BillableRequestAttempt) -> int | None:
        records = [
            record
            for record in self.usage_records
            if record.request_id == str(attempt.id)
            and record.correlation_id == attempt.correlation_id
            and record.model_id == attempt.model_id
            and record.usage_domain == "apim"
            and (
                canonical_budget_tokens(
                    record, reconciled=record.id in self.reconciled_usage_ids,
                ) is not None
                if strict_budget_evidence(attempt.created_at, self.evidence_policy_effective_at)
                else not record.estimated or record.status_code >= 400
            )
            and self._billable_scope_matches(record, attempt.scope_type, attempt.scope_id)
        ]
        return (
            sum(
                record.input_tokens + record.cached_tokens + record.output_tokens
                for record in records
            )
            if records
            else None
        )

    def _billable_scope_matches(
        self, record: TokenUsageRecord, scope_type: str, scope_id: str
    ) -> bool:
        if scope_type == "person":
            return record.user_id == scope_id
        if scope_type == "system":
            return record.request_source == "gateway-publication-probe"
        attribution = self.usage_application_attributions.get(record.id)
        return attribution is not None and str(attribution.application_id) == scope_id

    def _billable_effective_tokens(self, attempt: BillableRequestAttempt) -> int | None:
        if strict_budget_evidence(attempt.created_at, self.evidence_policy_effective_at):
            selected = self._strict_budget_choices(
                attempt.scope_type, attempt.scope_id,
            ).get(str(attempt.id))
            return None if selected is None else selected[1]
        canonical = self._billable_canonical_tokens(attempt)
        if canonical is not None:
            return canonical
        if attempt.state == "exact":
            return attempt.actual_tokens
        recoveries = [
            item
            for item in self.budget_reservation_finalizations.values()
            if item.correlation_id == str(attempt.id)
            and item.scope_type == attempt.scope_type
            and item.scope_id == attempt.scope_id
            and item.period_start == attempt.period_start
            and item.finalization_kind != "unverified_upper_bound"
        ]
        if not recoveries:
            return None
        if strict_budget_evidence(attempt.created_at, self.evidence_policy_effective_at):
            return recoveries[0].total_tokens
        return max(recoveries, key=lambda item: (
            item.finalization_kind == "exact_usage", item.evidence_at,
        )).total_tokens

    def _billable_budget_adjustment(
        self, scope_type: str, scope_id: str, start: date, end: date
    ) -> int:
        attempts = {
            str(row.id): row
            for row in self.billable_requests.values()
            if row.scope_type == scope_type and row.scope_id == scope_id
            and not strict_budget_evidence(row.created_at, self.evidence_policy_effective_at)
        }
        canonical: dict[str, int] = {}
        all_correlations: set[str] = set()
        for record in self.usage_records:
            if (
                record.usage_domain != "apim"
                or not start <= record.ts.date() < end
                or not self._billable_scope_matches(record, scope_type, scope_id)
            ):
                continue
            all_correlations.add(record.correlation_id)
            attempt = attempts.get(record.request_id)
            if (
                attempt is not None and record.model_id == attempt.model_id
                and record.correlation_id == attempt.correlation_id
            ):
                canonical[record.correlation_id] = (
                    record.input_tokens + record.cached_tokens + record.output_tokens
                )
        recovered: dict[str, BudgetReservationFinalization] = {}
        for item in self.budget_reservation_finalizations.values():
            if (
                item.scope_type != scope_type
                or item.scope_id != scope_id
                or item.period_start != start
                or item.correlation_id not in attempts
                or item.correlation_id in all_correlations
                or item.finalization_kind == "unverified_upper_bound"
            ):
                continue
            prior = recovered.get(item.correlation_id)
            if prior is None or (
                item.finalization_kind == "exact_usage" and prior.finalization_kind != "exact_usage"
            ):
                recovered[item.correlation_id] = item
        exact = sum(
            self._billable_effective_tokens(row) or 0
            for row in attempts.values()
            if row.period_start == start
        )
        return (
            exact - sum(canonical.values()) - sum(item.total_tokens for item in recovered.values())
        )

    def person_budget_state(
        self, user_id: str, period_start: date, period_end: date
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def budget_scope_confirmed_tokens(
        self, scope_type: str, scope_id: str, start: date, end: date
    ) -> int:
        raise NotImplementedError

    def begin_billable_request(self, plan: BillableRequestPlan) -> BillableRequestAttempt:
        with self.billable_request_lock:
            previous = [
                row
                for row in self.billable_requests.values()
                if row.operation_key == plan.operation_key
            ]
            for row in previous:
                if (
                    row.plan_sha256 == plan.plan_sha256 and row.state == "exact"
                    and row.evidence.get("admission_authority", "postgresql")
                    == plan.admission_authority
                    and all(key in row.evidence and row.evidence[key] == value
                            for key, value in plan.reuse_requires.items())
                ):
                    return row
            if len(previous) >= plan.attempt_limit or any(
                row.authorization_id == plan.authorization_id for row in previous
            ):
                raise BillableOutcomeUncertain(
                    "Billable attempt already exists; new authorization required"
                )
            end = date(
                plan.period_start.year + (plan.period_start.month == 12),
                1 if plan.period_start.month == 12 else plan.period_start.month + 1,
                1,
            )
            quota = (
                self.person_budget_state(plan.scope_id, plan.period_start, end)
                if plan.scope_type == "person" and plan.admission_authority == "postgresql"
                else None
            )
            if plan.scope_type == "application" and plan.admission_authority == "postgresql":
                budget = self.gateway_application_budgets.get(
                    (plan.period_start, UUID(plan.scope_id))
                )
                if budget is not None:
                    quota = {
                        "mode": "block" if budget["enforce"] else "audit",
                        "token_limit": budget["token_limit"],
                        "used_tokens": self.budget_scope_confirmed_tokens(
                            "application", plan.scope_id, plan.period_start, end
                        ),
                    }
            pending = sum(
                row.reserved_tokens
                for row in self.billable_requests.values()
                if row.scope_type == plan.scope_type
                and row.evidence.get("admission_authority") != "apim_execution"
                and row.scope_id == plan.scope_id
                and row.period_start == plan.period_start
                and row.state != "exact"
                and self._billable_effective_tokens(row) is None
            )
            if (
                quota
                and quota["mode"] == "block"
                and quota["used_tokens"] + pending + plan.reserved_tokens > quota["token_limit"]
            ):
                raise BillableBudgetExceeded("The remaining budget cannot cover this request")
            now = datetime.now(UTC)
            row = BillableRequestAttempt(
                **plan.model_dump(exclude={
                    "attempt_limit", "reuse_requires", "admission_authority",
                }),
                evidence={"admission_authority": "apim_execution"}
                if plan.admission_authority == "apim_execution" else {},
                id=uuid4(),
                attempt_index=len(previous) + 1,
                state="started",
                created_at=now,
                updated_at=now,
            )
            self.billable_requests[row.id] = row
            return row

    def recover_billable_request(self, plan: BillableRequestPlan) -> BillableRequestAttempt:
        with self.billable_request_lock:
            return recover_billable_attempt(plan, [
                row for row in self.billable_requests.values()
                if row.operation_key == plan.operation_key
            ])

    def finish_billable_request(
        self,
        request_id: UUID,
        *,
        actual_tokens: int | None,
        correlation_id: str | None,
        evidence: dict[str, Any],
    ) -> None:
        outcome = BillableRequestOutcome(
            actual_tokens=actual_tokens, correlation_id=correlation_id, evidence=evidence
        )
        with self.billable_request_lock:
            row = self.billable_requests[request_id]
            if row.state == "exact":
                if actual_tokens is not None and row.actual_tokens != actual_tokens:
                    raise ValueError("An exact acknowledgement cannot be changed")
                return
            self.billable_requests[request_id] = BillableRequestAttempt.model_validate(
                {
                    **row.model_dump(),
                    **outcome.model_dump(),
                    "evidence": {**{key: value for key, value in outcome.evidence.items()
                                     if key != "admission_authority"},
                                 **({"admission_authority": row.evidence["admission_authority"]}
                                    if "admission_authority" in row.evidence else {})},
                    "state": "exact" if actual_tokens is not None else "uncertain",
                    "correlation_id": correlation_id or row.correlation_id,
                    "updated_at": datetime.now(UTC),
                }
            )

    def pending_billable_requests(self) -> list[dict[str, Any]]:
        return [
            {
                **row.model_dump(),
                "finalization_kind": next(
                    (
                        item.finalization_kind
                        for item in self.budget_reservation_finalizations.values()
                        if item.correlation_id == str(row.id)
                        and item.finalization_kind == "unverified_upper_bound"
                    ),
                    None,
                ),
            }
            for row in self.billable_requests.values()
            if row.scope_type != "system"
            and row.evidence.get("admission_authority") != "apim_execution"
            and row.state != "exact"
            and self._billable_effective_tokens(row) is None
        ]

    def settled_billable_requests(
        self,
        request_ids: list[str],
        scope_type: str,
        scope_id: str | None,
    ) -> set[str]:
        return {
            str(row.id)
            for row in self.billable_requests.values()
            if str(row.id) in request_ids
            and row.scope_type == scope_type
            and row.scope_id == scope_id
            and self._billable_effective_tokens(row) is not None
        }
