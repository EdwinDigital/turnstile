from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from ..domain.enterprise import enterprise_catalog
from ..domain.ledger import (
    LedgerAdmissionReceipt,
    LedgerAttempt,
    LedgerAttemptIdentity,
    LedgerCharge,
    LedgerDispatch,
    LedgerPolicyUpdate,
    LedgerRecoveryCursor,
    LedgerScope,
    LedgerScopeSeed,
    LedgerSettlementTask,
    LedgerSummary,
)
from ..integrations.atomic_ledger import AtomicTableLedger
from ..integrations.ledger import ROLL_FORWARD_ACTOR, application_partition_key, partition_key

logger = logging.getLogger(__name__)


@runtime_checkable
class BudgetScopeRepository(Protocol):
    def active_budget_execution_rollout(self) -> dict[str, Any] | None: ...
    def roll_forward_budgets(self, period_start: date, changed_by: str) -> object: ...
    def roll_forward_gateway_application_budgets(self, period_start: date, actor: str) -> int: ...
    def prepare_budget_execution_period(
        self,
        epoch: UUID,
        period: date,
        known_people: Sequence[str],
    ) -> None: ...
    def budget_scope_seed_candidates(
        self,
        *,
        limit: int = 16,
        application_id: UUID | None = None,
    ) -> list[LedgerScopeSeed]: ...
    def activate_budget_scope_seed(self, seed: LedgerScopeSeed) -> None: ...
    def defer_budget_scope_seed(self, scope: LedgerScope) -> None: ...
    def complete_budget_policy(self, update: LedgerPolicyUpdate) -> None: ...


class BudgetScopeService:
    def __init__(self, repository: BudgetScopeRepository, ledger: AtomicTableLedger) -> None:
        self._repository, self._ledger = repository, ledger

    def prepare(
        self,
        *,
        application_id: UUID | None = None,
        gateway_profile_id: UUID | None = None,
        now: datetime | None = None,
    ) -> int:
        rollout = self._repository.active_budget_execution_rollout()
        if rollout is None:
            return 0
        if (
            gateway_profile_id is not None
            and gateway_profile_id not in rollout["gateway_profile_ids"]
        ):
            return 0
        period = (now or datetime.now(UTC)).astimezone(UTC).date().replace(day=1)
        if rollout["prepared_period"] != period:
            self._repository.roll_forward_budgets(period, ROLL_FORWARD_ACTOR)
            self._repository.roll_forward_gateway_application_budgets(period, ROLL_FORWARD_ACTOR)
            self._repository.prepare_budget_execution_period(
                rollout["epoch"],
                period,
                [person.id for person in enterprise_catalog().users],
            )
        candidates = self._repository.budget_scope_seed_candidates(application_id=application_id)
        if application_id is not None and not candidates:
            raise RuntimeError("Application execution scope is not yet prepared")
        prepared = 0
        for seed in candidates:
            try:
                scope = seed.summary.scope
                if not seed.active:
                    legacy = (
                        application_partition_key(scope.scope_id, f"{scope.period_start:%Y-%m}")
                        if scope.scope_type == "application"
                        else partition_key(scope.scope_id, f"{scope.period_start:%Y-%m}")
                    )
                    if self._ledger.list_reservations(legacy):
                        raise ValueError("Legacy outstanding charges prevent scope initialization")
                    self._ledger.initialize(seed.summary)
                    self._repository.activate_budget_scope_seed(seed)
                policy = seed.policy
                self._ledger.write_policy(
                    policy.scope,
                    epoch=policy.epoch,
                    revision=policy.revision,
                    token_limit=policy.token_limit,
                    enforce=policy.enforce,
                    enabled=policy.enabled,
                )
                self._repository.complete_budget_policy(policy)
                prepared += 1
            except Exception as error:
                self._repository.defer_budget_scope_seed(seed.summary.scope)
                if application_id is not None:
                    raise
                logger.warning(
                    "Budget scope preparation deferred error_type=%s", type(error).__name__
                )
        return prepared


@runtime_checkable
class BudgetStatusRepository(Protocol):
    def budget_execution_status(self, scope: LedgerScope) -> dict[str, Any] | None: ...


def execution_budget_view(
    repository: BudgetStatusRepository,
    store: AtomicTableLedger,
    scope: LedgerScope,
) -> dict[str, Any] | None:
    unknown: dict[str, Any] = {
        "ledger_source": "execution",
        "ledger_status": "unavailable",
        "ledger_snapshot_at": None,
        "ledger_read_at": None,
        "ledger_version": None,
        "available_tokens": None,
        "ledger_balance_tokens": None,
        "pending_reserved_tokens": None,
        "pending_reservation_count": None,
        "finalized_upper_bound_tokens": None,
        "finalized_upper_bound_count": None,
        "stale_reservation_count": None,
        "oldest_reservation_at": None,
    }
    try:
        status = repository.budget_execution_status(scope)
        if status is None:
            return None
        if status["state"] == "prepared":
            return {**unknown, "ledger_status": "prepared"}
        summary = store.read_summary(scope)
        if summary.epoch != status["epoch"]:
            raise ValueError("Execution ledger epoch does not match the registered scope")
        return {
            **unknown,
            "ledger_status": "current",
            "ledger_read_at": datetime.now(UTC),
            "ledger_snapshot_at": summary.updated_at,
            "ledger_version": summary.version,
            "token_limit": summary.token_limit,
            "enforce": summary.enforce,
            "used_tokens": summary.confirmed_tokens,
            "remaining_tokens": max(summary.token_limit - summary.confirmed_tokens, 0),
            "usage_percent": (
                100 * summary.confirmed_tokens / summary.token_limit if summary.token_limit else 0
            ),
            "available_tokens": max(summary.available_tokens, 0),
            "ledger_balance_tokens": summary.available_tokens,
            "pending_reserved_tokens": summary.active_reserved_tokens,
            "finalized_upper_bound_tokens": summary.upper_bound_tokens,
            "policy_pending": summary.policy_revision < status["policy_revision"],
        }
    except Exception as error:
        logger.warning("Execution balance read failed error_type=%s", type(error).__name__)
        return unknown


@runtime_checkable
class BudgetSettlementRepository(Protocol):
    def claim_budget_settlement(
        self,
        *,
        usage_id: str | None = None,
        lease_seconds: int = 30,
    ) -> LedgerSettlementTask | None: ...
    def complete_budget_settlement(self, task: LedgerSettlementTask) -> bool: ...
    def retry_budget_settlement(self, task: LedgerSettlementTask, error_code: str) -> bool: ...
    def budget_settlement_health(self) -> dict[str, int | float]: ...


@runtime_checkable
class BudgetRecoveryRepository(Protocol):
    def budget_recovery_scopes(self, limit: int = 8) -> list[LedgerRecoveryCursor]: ...
    def advance_budget_recovery(
        self,
        cursor: LedgerRecoveryCursor,
        next_partition: str | None,
        next_row: str | None,
    ) -> bool: ...
    def register_budget_dispatch(self, dispatch: LedgerDispatch) -> None: ...
    def pending_budget_completions(self, limit: int = 8) -> list[LedgerAdmissionReceipt]: ...
    def record_budget_completion(
        self,
        receipt: LedgerAdmissionReceipt,
        completion: dict[str, Any] | None,
    ) -> None: ...
    def pending_budget_policies(self, limit: int = 16) -> list[LedgerPolicyUpdate]: ...
    def complete_budget_policy(self, update: LedgerPolicyUpdate) -> None: ...
    def unverified_execution_attempts(self, limit: int = 16) -> list[LedgerAttemptIdentity]: ...
    def complete_execution_finalization(self, identity: LedgerAttemptIdentity) -> None: ...


class BudgetRecoveryService:
    def __init__(self, repository: BudgetRecoveryRepository, ledger: AtomicTableLedger) -> None:
        self._repository, self._ledger = repository, ledger

    def recover(self, *, scopes: int = 8, seconds: float = 20) -> int:
        recovered, started = 0, time.monotonic()
        for cursor in self._repository.budget_recovery_scopes(scopes):
            if time.monotonic() - started >= seconds:
                break
            try:
                plans, next_partition, next_row = self._ledger.pending_dispatch_page(cursor)
                for plan in plans:
                    if time.monotonic() - started >= seconds:
                        self._repository.advance_budget_recovery(
                            cursor,
                            cursor.next_partition,
                            cursor.next_row,
                        )
                        return recovered
                    if plan.state == "preparing" and plan.expires_at > datetime.now(UTC):
                        continue
                    if plan.state in {"preparing", "cancelled"}:
                        if not self._ledger.cancel_dispatch(plan, recovery=True):
                            continue
                        self._ledger.compensate_dispatch(plan)
                    self._repository.register_budget_dispatch(plan)
                    self._ledger.finish_dispatch_recovery(plan)
                    recovered += 1
                self._repository.advance_budget_recovery(cursor, next_partition, next_row)
            except Exception as error:
                logger.warning(
                    "Budget dispatch recovery deferred error_type=%s", type(error).__name__
                )
                self._repository.advance_budget_recovery(
                    cursor, cursor.next_partition, cursor.next_row
                )
        return recovered

    def completions(self, *, limit: int = 8, seconds: float = 10) -> int:
        recovered, started = 0, time.monotonic()
        for receipt in self._repository.pending_budget_completions(limit):
            if time.monotonic() - started >= seconds:
                break
            try:
                attempt = self._ledger.read_attempt(receipt)
                self._repository.record_budget_completion(receipt, attempt.completion_evidence)
                recovered += int(attempt.completion_evidence is not None)
            except Exception as error:
                logger.warning(
                    "Completion evidence recovery deferred error_type=%s", type(error).__name__
                )
                self._repository.record_budget_completion(receipt, None)
        return recovered

    def policies(self, *, limit: int = 8) -> int:
        applied = 0
        for update in self._repository.pending_budget_policies(limit):
            try:
                self._ledger.write_policy(
                    update.scope,
                    epoch=update.epoch,
                    revision=update.revision,
                    token_limit=update.token_limit,
                    enforce=update.enforce,
                    enabled=update.enabled,
                )
                self._repository.complete_budget_policy(update)
                applied += 1
            except Exception as error:
                logger.warning(
                    "Budget policy delivery deferred error_type=%s", type(error).__name__
                )
        return applied

    def finalize_uncertain(self, *, limit: int = 8) -> int:
        finalized = 0
        for identity in self._repository.unverified_execution_attempts(limit):
            try:
                self._ledger.finalize_uncertain(identity)
                self._repository.complete_execution_finalization(identity)
                finalized += 1
            except Exception as error:
                logger.warning("Budget finalization deferred error_type=%s", type(error).__name__)
        return finalized


class SettlementLedger(Protocol):
    def settle(self, charge: LedgerCharge) -> tuple[LedgerSummary, LedgerAttempt]: ...


class BudgetSettlementService:
    def __init__(self, repository: BudgetSettlementRepository, ledger: SettlementLedger) -> None:
        self._repository, self._ledger = repository, ledger

    def drain(self, usage_id: str | None = None, *, limit: int = 32, seconds: float = 20) -> int:
        started, completed = time.monotonic(), 0
        for _ in range(max(1, min(limit, 100))):
            if time.monotonic() - started >= seconds:
                break
            task = self._repository.claim_budget_settlement(usage_id=usage_id, lease_seconds=120)
            if task is None:
                break
            try:
                if task.lease_until <= datetime.now(UTC):
                    raise RuntimeError("Settlement claim expired before execution")
                self._ledger.settle(task.charge)
                if self._repository.complete_budget_settlement(task):
                    completed += 1
                    logger.info(
                        "Budget settlement applied age_seconds=%.3f attempt=%d",
                        max(0, (datetime.now(UTC) - task.created_at).total_seconds()),
                        task.attempts,
                    )
                else:
                    logger.warning("Budget settlement acknowledgement fenced by an expired claim")
            except Exception as error:
                self._repository.retry_budget_settlement(task, type(error).__name__)
                logger.warning("Budget settlement deferred error_type=%s", type(error).__name__)
        return completed
