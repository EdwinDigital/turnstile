from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from tests.backend.telemetry.test_ledger import (
    PERIOD_START,
    USER,
    FakeLedgerStore,
    _finalization,
    _usage,
)
from turnstile_core.domain.billable_requests import (
    BillableBudgetExceeded,
    BillableOutcomeUncertain,
    BillableRequestAttempt,
    BillableRequestOutcome,
    BillableRequestPlan,
)
from turnstile_core.domain.ledger import BudgetReservationAdmission
from turnstile_core.domain.models import ReconciledUsage
from turnstile_core.integrations.ledger import LedgerSyncService, partition_key, reservation_row_key
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_billable_requests import (
    PostgreSqlBillableRequestRepositoryMixin,
)


def _plan(**overrides: Any) -> BillableRequestPlan:
    return BillableRequestPlan.model_validate(
        {
            "operation_key": "unit-operation",
            "plan_sha256": "a" * 64,
            "scope_type": "person",
            "scope_id": "unit-person",
            "period_start": date(2026, 9, 1),
            "model_id": "unit-model-id",
            "model_key": "unit-model",
            "reserved_tokens": 100,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"period_start": date(2026, 9, 2)},
        {"reserved_tokens": 0},
        {"attempt_limit": 0},
        {"attempt_limit": 33},
        {"plan_sha256": "unbound"},
    ],
)
def test_billable_plan_rejects_invalid_admission(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _plan(**change)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_billable_acknowledgement_requires_nonnegative_integer(value: object) -> None:
    with pytest.raises(ValueError):
        BillableRequestOutcome.model_validate({"actual_tokens": value})


@pytest.mark.parametrize(
    "state,tokens",
    [
        ("exact", None),
        ("started", 0),
        ("uncertain", 1),
    ],
)
def test_attempt_exact_state_requires_measured_total(state: str, tokens: int | None) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        BillableRequestAttempt.model_validate(
            {
                **_plan().model_dump(exclude={
                    "attempt_limit", "reuse_requires", "admission_authority",
                }),
                "id": uuid4(),
                "attempt_index": 1,
                "state": state,
                "actual_tokens": tokens,
                "created_at": now,
                "updated_at": now,
            }
        )


def test_uncertain_and_measured_zero_are_distinct() -> None:
    assert BillableRequestOutcome(actual_tokens=None).actual_tokens is None
    assert BillableRequestOutcome(actual_tokens=0).actual_tokens == 0


def test_billable_attempt_requires_fresh_authorization_and_preserves_exact_result() -> None:
    repository = InMemoryRepository()
    plan = _plan(scope_type="system")
    first = repository.begin_billable_request(plan)
    with pytest.raises(BillableOutcomeUncertain):
        repository.begin_billable_request(plan)
    repository.finish_billable_request(
        first.id,
        actual_tokens=None,
        correlation_id="gateway-first",
        evidence={"status": 500},
    )
    retry = repository.begin_billable_request(
        _plan(
            scope_type="system",
            authorization_id=uuid4(),
            attempt_limit=2,
        )
    )
    assert retry.id != first.id and retry.attempt_index == 2
    repository.finish_billable_request(
        retry.id,
        actual_tokens=30,
        correlation_id="gateway-second",
        evidence={"verified": True},
    )
    stored = repository.billable_requests[retry.id]
    assert (
        repository.begin_billable_request(
            plan.model_copy(update={"reuse_requires": {"verified": True}})
        )
        == stored
    )
    with pytest.raises(ValueError, match="cannot be changed"):
        repository.finish_billable_request(
            retry.id,
            actual_tokens=0,
            correlation_id="other",
            evidence={},
        )
    assert repository.billable_requests[retry.id] == stored


@pytest.mark.parametrize("authority", ["postgresql", "apim_execution"])
def test_billable_recovery_is_readonly_and_requires_exact_original_authority(
    authority: str,
) -> None:
    repository = InMemoryRepository()
    plan = _plan(scope_type="system", authorization_id=uuid4(), admission_authority=authority,
                 reuse_requires={"route_validated": True})
    with pytest.raises(BillableOutcomeUncertain):
        repository.recover_billable_request(plan)
    assert repository.billable_requests == {}
    attempt = repository.begin_billable_request(plan)
    with pytest.raises(BillableOutcomeUncertain):
        repository.recover_billable_request(plan)
    repository.finish_billable_request(
        attempt.id, actual_tokens=30, correlation_id="unit-correlation",
        evidence={"route_validated": True, "admission_authority": "forged"},
    )
    original = repository.billable_requests[attempt.id]
    assert repository.recover_billable_request(plan) == original
    assert original.evidence.get("admission_authority", "postgresql") == authority
    for changes in ({"model_id": "other-model"}, {"authorization_id": uuid4()},
                    {"plan_sha256": "b" * 64}, {"reuse_requires": {"route_validated": False}},
                    {"admission_authority": "apim_execution" if authority == "postgresql"
                                              else "postgresql"}):
        with pytest.raises(BillableOutcomeUncertain):
            repository.recover_billable_request(plan.model_copy(update=changes))
    assert repository.billable_requests == {attempt.id: original}

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [original.model_dump()]
    sql = PostgreSqlBillableRequestRepositoryMixin()
    with patch.object(sql, "_connection", return_value=connection):
        assert sql.recover_billable_request(plan) == original
    statement, parameters = connection.execute.call_args.args
    assert statement.strip().startswith("SELECT") and "LIMIT 33" in statement
    assert parameters == (plan.operation_key,) and connection.execute.call_count == 1


def test_application_budget_includes_unsettled_attempts_before_dispatch() -> None:
    repository = InMemoryRepository()
    application_id = uuid4()
    plan = _plan(scope_type="application", scope_id=str(application_id))
    repository.gateway_application_budgets[(plan.period_start, application_id)] = {
        "token_limit": 150,
        "enforce": True,
    }
    first = repository.begin_billable_request(plan)
    second = plan.model_copy(update={"operation_key": "second", "reserved_tokens": 60})
    with pytest.raises(BillableBudgetExceeded):
        repository.begin_billable_request(second)
    assert len(repository.billable_requests) == 1
    repository.finish_billable_request(
        first.id, actual_tokens=30, correlation_id="first", evidence={}
    )
    assert repository.begin_billable_request(second).state == "started"
    assert len(repository.pending_billable_requests()) == 1


@pytest.mark.parametrize("status_code", [200, 400, 500])
def test_v2_exact_recovery_cannot_be_replaced_by_estimated_error_zero(status_code: int) -> None:
    repository = InMemoryRepository()
    repository.evidence_policy_effective_at = datetime(2026, 6, 1, tzinfo=UTC)
    exact = _finalization(
        finalization_kind="exact_usage",
        source="apim_gateway_llm_log",
        input_tokens=80,
        output_tokens=40,
        total_tokens=120,
    )
    repository.save_budget_reservation_finalizations([exact])
    late = _usage(exact.correlation_id, PERIOD_START, 0, estimated=True).model_copy(
        update={
            "correlation_id": exact.correlation_id,
            "status_code": status_code,
            "ingest_error": "failed_request_has_no_usage",
        }
    )
    repository.write_token_usage(late)
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 120
    )
    assert repository.usage_records == [late]


def test_v2_preserves_admission_month_and_first_equal_rank_recovery() -> None:
    repository = InMemoryRepository()
    repository.evidence_policy_effective_at = datetime(2026, 6, 1, tzinfo=UTC)
    first = _finalization(
        finalization_kind="exact_usage",
        source="apim_gateway_llm_log",
        input_tokens=80,
        output_tokens=40,
        total_tokens=120,
    )
    second = first.model_copy(
        update={
            "evidence_at": first.evidence_at + timedelta(days=1),
            "input_tokens": 100,
            "total_tokens": 140,
        }
    )
    repository.save_budget_reservation_finalizations([first, second])
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 120
    )
    canonical = _usage("formal", datetime(2026, 8, 2, tzinfo=UTC), 160, estimated=False).model_copy(
        update={"correlation_id": first.correlation_id}
    )
    repository.write_token_usage(canonical)
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 160
    )
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 8, 1), date(2026, 9, 1))
        == 0
    )
    assert repository.usage_records == [canonical]
    assert repository.budget_evidence_conflicts
    scoped = {
        (row["scope_type"], row["scope_id"]): row["used_tokens"]
        for row in repository.token_usage_by_budget_scope(
            PERIOD_START, datetime(2026, 8, 1, tzinfo=UTC)
        )
    }
    assert scoped[("user", USER)] == 160
    following_period = repository.token_usage_by_budget_scope(
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert following_period and all(row["used_tokens"] == 0 for row in following_period)
    assert all(row["unpriced_usage_count"] == 1 for row in following_period)


@pytest.mark.parametrize("effective_at", [None, datetime(2099, 1, 1, tzinfo=UTC)])
def test_disabled_or_future_v2_preserves_public_legacy_error_settlement(
    effective_at: datetime | None,
) -> None:
    repository = InMemoryRepository()
    repository.evidence_policy_effective_at = effective_at
    failed = _usage("legacy-error", PERIOD_START, 0, estimated=True).model_copy(
        update={"status_code": 400}
    )
    repository.write_token_usage(failed)
    assert repository.settled_reservation_correlations(
        [failed.correlation_id], scope_type="person", scope_id=USER
    ) == {failed.correlation_id}


def test_v2_unknown_error_remains_unsettled_and_admission_is_immutable() -> None:
    repository = InMemoryRepository()
    repository.evidence_policy_effective_at = datetime(2026, 6, 1, tzinfo=UTC)
    admission = BudgetReservationAdmission(
        scope_type="person",
        scope_id=USER,
        correlation_id="unknown",
        created_at=PERIOD_START,
        reserved_tokens=500,
    )
    repository.register_budget_reservations([admission])
    repository.register_budget_reservations([admission])
    with pytest.raises(ValueError, match="immutable"):
        repository.register_budget_reservations(
            [admission.model_copy(update={"reserved_tokens": 1})]
        )
    failed = _usage("unknown", PERIOD_START, 0, estimated=True).model_copy(
        update={"status_code": 500, "correlation_id": "unknown"}
    )
    repository.write_token_usage(failed)
    assert (
        repository.settled_reservation_correlations(["unknown"], scope_type="person", scope_id=USER)
        == set()
    )


def test_v2_diagnostic_total_does_not_add_old_estimated_cache_bucket() -> None:
    repository = InMemoryRepository()
    repository.evidence_policy_effective_at = datetime(2026, 6, 1, tzinfo=UTC)
    record = _usage("stream", PERIOD_START, 0, estimated=True).model_copy(
        update={"cached_tokens": 99}
    )
    repository.write_token_usage(record)
    repository.apply_reconciled_usage(
        [
            ReconciledUsage(
                correlation_id=record.correlation_id,
                input_tokens=70,
                output_tokens=30,
                cached_tokens=None,
            )
        ]
    )
    assert (
        repository.budget_scope_confirmed_tokens("person", USER, date(2026, 7, 1), date(2026, 8, 1))
        == 100
    )
    assert repository.usage_records[0].cached_tokens == 99


@pytest.mark.parametrize("cutover", [None, datetime(2026, 6, 1, tzinfo=UTC)])
def test_platform_and_gateway_reservations_settle_once_without_rewriting_ack(
    cutover: datetime | None,
) -> None:
    repository = InMemoryRepository()
    repository.evidence_policy_effective_at = cutover
    attempt = BillableRequestAttempt.model_validate(
        {
            **_plan(scope_id=USER, period_start=PERIOD_START.date()).model_dump(
                exclude={"attempt_limit", "reuse_requires", "admission_authority"}
            ),
            "id": uuid4(),
            "attempt_index": 1,
            "state": "exact",
            "actual_tokens": 120,
            "correlation_id": "unit-gateway-attempt",
            "created_at": PERIOD_START,
            "updated_at": PERIOD_START + timedelta(seconds=10),
        }
    )
    repository.billable_requests[attempt.id] = attempt
    store = FakeLedgerStore()
    partition = partition_key(USER, "2026-07")
    for identity in (str(attempt.id), str(attempt.correlation_id)):
        store.entities[(partition, reservation_row_key(PERIOD_START, identity))] = {"Reserved": 900}
    usage = _usage(
        "formal-image", PERIOD_START + timedelta(seconds=20), 160, estimated=False
    ).model_copy(
        update={
            "request_id": str(attempt.id),
            "model_id": attempt.model_id,
            "correlation_id": attempt.correlation_id,
        }
    )
    repository.write_token_usage(usage)
    LedgerSyncService(repository, store).run(PERIOD_START + timedelta(minutes=1))
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 160}
    assert store.list_reservations(partition) == []
    assert repository.billable_requests[attempt.id] == attempt
    LedgerSyncService(repository, store).run(PERIOD_START + timedelta(minutes=2))
    assert store.entities[(partition, "C")] == {"ConfirmedUsed": 160}
