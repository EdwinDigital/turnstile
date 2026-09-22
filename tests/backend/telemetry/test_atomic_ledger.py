from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from typing import Any
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

import httpx
import pytest

from turnstile_core.domain.budget_completion import utc_time, validate_completion
from turnstile_core.domain.ledger import (
    LedgerAdmissionDenied,
    LedgerAdmissionReceipt,
    LedgerAttemptIdentity,
    LedgerCharge,
    LedgerPolicyUpdate,
    LedgerProtocolConflict,
    LedgerScope,
    LedgerScopeSeed,
    LedgerSettlementTask,
    LedgerSummary,
    complete_ledger,
    reserve_ledger,
    settle_ledger,
    transition_ledger_attempt,
    update_ledger_policy,
)
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger, LedgerUnavailable
from turnstile_core.integrations.budget_completion import BudgetCompletionClient
from turnstile_core.integrations.table_batch import (
    TableBatchConflict,
    TableBatchUnavailable,
    conditional_batch,
    validate_batch_response,
)
from turnstile_core.persistence.repository_budget_execution import PostgreSqlBudgetExecutionMixin
from turnstile_core.services.budget_settlement import (
    BudgetRecoveryService,
    BudgetScopeService,
    BudgetSettlementService,
    execution_budget_view,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)
SCOPE = LedgerScope(scope_type="person", scope_id="unit-person", period_start=date(2026, 9, 1))
EPOCH = UUID("90000000-0000-4000-8000-000000000001")
TARGET = UUID("90000000-0000-4000-8000-000000000002")


def summary(limit: int = 1000, **values: Any) -> LedgerSummary:
    return LedgerSummary.model_validate(
        {
            "scope": SCOPE,
            "epoch": EPOCH,
            "token_limit": limit,
            "enforce": True,
            "updated_at": NOW,
            **values,
        }
    )


def identity(correlation: str = "unit-attempt", **values: Any) -> LedgerAttemptIdentity:
    return LedgerAttemptIdentity.model_validate(
        {
            "scope": SCOPE,
            "gateway_profile_id": TARGET,
            "native_gateway_id": "managed",
            "correlation_id": correlation,
            "model_id": "unit-model",
            "admitted_at": NOW,
            **values,
        }
    )


def charge(total: int, revision: int = 1, **values: Any) -> LedgerCharge:
    return LedgerCharge.model_validate(
        {
            "identity": identity(),
            "epoch": EPOCH,
            "revision": revision,
            "evidence_key": f"unit-evidence-{revision}",
            "total_tokens": total,
            **values,
        }
    )


def completion(total: int = 100, **values: Any) -> dict[str, Any]:
    return {
        "source": "apim",
        "input_tokens": total,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "total_tokens": total,
        "observed_at": NOW.isoformat(),
        **values,
    }


@pytest.mark.parametrize(
    "state",
    [
        "reserved",
        "dispatching",
        "uncertain",
        "upper_bound",
        "cancelled",
    ],
)
@pytest.mark.parametrize("actual", [0, 100, 1200])
def test_complete_response_settles_once_before_durable_task(state: str, actual: int) -> None:
    current, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    if state in {"dispatching", "uncertain"}:
        current, attempt = transition_ledger_attempt(current, attempt, "dispatching", NOW)
    if state == "uncertain":
        current, attempt = transition_ledger_attempt(current, attempt, "uncertain", NOW)
    elif state == "upper_bound":
        current, attempt = transition_ledger_attempt(current, attempt, "upper_bound", NOW)
    elif state == "cancelled":
        current, attempt = transition_ledger_attempt(current, attempt, "cancelled", NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    settled, retained = complete_ledger(current, attempt, receipt, completion(actual), NOW)
    assert settled.confirmed_tokens == actual
    assert settled.active_reserved_tokens == settled.upper_bound_tokens == 0
    duplicate, duplicate_attempt = complete_ledger(
        settled,
        retained,
        receipt,
        completion(actual, source="observer"),
        NOW,
    )
    assert duplicate is settled and duplicate_attempt is retained
    delayed, delayed_attempt = settle_ledger(settled, retained, charge(actual), NOW)
    assert delayed is settled and delayed_attempt is retained


@pytest.mark.parametrize("ack_first", [False, True])
def test_complete_response_dominates_acknowledgement(ack_first: bool) -> None:
    current, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    acknowledgement = charge(90, evidence_rank=2)
    if ack_first:
        current, attempt = settle_ledger(current, attempt, acknowledgement, NOW)
    current, attempt = complete_ledger(current, attempt, receipt, completion(100), NOW)
    current, attempt = settle_ledger(current, attempt, acknowledgement, NOW)
    assert current.confirmed_tokens == 100 and current.active_reserved_tokens == 0
    with pytest.raises(LedgerProtocolConflict):
        settle_ledger(current, attempt, charge(101, revision=2), NOW)


def test_unknown_execution_cannot_release_reservation() -> None:
    current, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    current, attempt = transition_ledger_attempt(current, attempt, "dispatching", NOW)
    current, attempt = transition_ledger_attempt(current, attempt, "uncertain", NOW)
    with pytest.raises(LedgerProtocolConflict):
        transition_ledger_attempt(current, attempt, "cancelled", NOW)
    upper, retained = transition_ledger_attempt(current, attempt, "upper_bound", NOW)
    assert upper.available_tokens == current.available_tokens == 500
    assert upper.upper_bound_tokens == retained.reserved_tokens == 500


@pytest.mark.parametrize(
    "values,reservation,code",
    [
        ({"enabled": False}, 1, "budget_consumer_disabled"),
        ({"confirmed_tokens": 1000}, 1, "budget_exhausted"),
        ({"active_reserved_tokens": 900}, 200, "budget_temporarily_reserved"),
        ({}, 1001, "budget_request_bound_exceeds_balance"),
    ],
)
def test_reservation_denial_preserves_error_semantics(
    values: dict[str, Any],
    reservation: int,
    code: str,
) -> None:
    with pytest.raises(LedgerAdmissionDenied) as failure:
        reserve_ledger(summary(**values), identity(), reservation, NOW)
    assert failure.value.code == code


@pytest.mark.parametrize(
    "change",
    [
        {"input_tokens": True},
        {"input_tokens": "100"},
        {"cached_tokens": 2},
        {"cache_write_tokens": 1},
        {"total_tokens": 2**63},
        {"source": "client"},
        {"observed_at": "2026-09-22T00:00:00"},
        {"extra": "business-value"},
    ],
)
def test_completion_requires_complete_exact_producer_evidence(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_completion(completion(**change))


def test_policy_updates_do_not_reset_financial_state() -> None:
    current = summary(confirmed_tokens=100, active_reserved_tokens=200, upper_bound_tokens=50)
    updated = update_ledger_policy(
        current,
        epoch=EPOCH,
        revision=1,
        token_limit=900,
        enforce=True,
        enabled=True,
        now=NOW,
    )
    assert (
        updated.confirmed_tokens,
        updated.active_reserved_tokens,
        updated.upper_bound_tokens,
    ) == (100, 200, 50)
    assert (
        update_ledger_policy(
            updated,
            epoch=EPOCH,
            revision=1,
            token_limit=900,
            enforce=True,
            enabled=True,
            now=NOW,
        )
        is updated
    )
    with pytest.raises(LedgerProtocolConflict):
        update_ledger_policy(
            updated,
            epoch=EPOCH,
            revision=1,
            token_limit=999,
            enforce=True,
            enabled=True,
            now=NOW,
        )


def test_completion_timestamp_accepts_apim_precision() -> None:
    assert utc_time("2026-09-22T00:00:00.1234567Z") == NOW.replace(microsecond=123456)


def batch_response(statuses: list[int]) -> bytes:
    return (
        "--unit\r\nContent-Type: multipart/mixed; boundary=change\r\n\r\n"
        + "".join(
            "--change\r\nContent-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n\r\n"
            f"HTTP/1.1 {status} Result\r\n\r\n"
            for status in statuses
        )
        + "--change--\r\n--unit--\r\n"
    ).encode()


@pytest.mark.parametrize("statuses", [[], [204], [204, 500], [204, 204, 204]])
def test_batch_requires_every_operation_to_succeed(statuses: list[int]) -> None:
    with pytest.raises(TableBatchUnavailable):
        validate_batch_response(202, "multipart/mixed; boundary=unit", batch_response(statuses), 2)


@pytest.mark.parametrize("status", [409, 412])
def test_batch_conflicts_are_distinct_from_transport_failure(status: int) -> None:
    with pytest.raises(TableBatchConflict):
        validate_batch_response(202, "multipart/mixed; boundary=unit", batch_response([status]), 2)
    validate_batch_response(202, "multipart/mixed; boundary=unit", batch_response([204, 204]), 2)


@pytest.mark.parametrize("etag", ["*", "", "\r\n", 'W/"unit"\nInjected: value'])
def test_batch_rejects_inexact_or_injected_concurrency_tokens(etag: str) -> None:
    with pytest.raises(ValueError):
        conditional_batch("https://ledger.example", "Unit", "partition", [("S", {}, etag)])


def test_conditional_batch_binds_one_partition_and_preserves_compare_and_insert() -> None:
    content_type, content = conditional_batch(
        "https://ledger.example",
        "Unit",
        "partition",
        [
            ("S", {"Document": "{}"}, 'W/"unit"'),
            ("A|unit", {"Document": "{}"}, None),
        ],
    )
    assert content_type.startswith("multipart/mixed; boundary=batch_")
    assert content.count(b"If-Match:") == 1 and b'If-Match: W/"unit"' in content
    assert b"PUT https://ledger.example/Unit(" in content
    assert b"POST https://ledger.example/Unit HTTP/1.1" in content
    with pytest.raises(ValueError):
        conditional_batch(
            "https://ledger.example",
            "Unit",
            "partition",
            [
                ("S", {"PartitionKey": "different"}, 'W/"unit"'),
            ],
        )


@pytest.mark.parametrize("lost_response", [False, True])
def test_atomic_reservation_retries_do_not_duplicate_financial_writes(lost_response: bool) -> None:
    import json

    documents = {"S": summary().model_dump(mode="json")}
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "GET":
            key = "S" if "RowKey='S'" in str(request.url) else identity().row_key
            if key not in documents:
                return httpx.Response(404)
            return httpx.Response(
                200, headers={"ETag": 'W/"unit"'}, json={"Document": json.dumps(documents[key])}
            )
        assert request.method == "POST" and request.url.path == "/$batch"
        writes += 1
        envelope = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
            + request.content,
        )
        for part in envelope.walk():
            if part.get_content_type() == "application/http":
                payload = part.get_payload(decode=True)
                assert isinstance(payload, bytes)
                entity = json.loads(payload.split(b"\r\n\r\n", 1)[1])
                documents[entity["RowKey"]] = json.loads(entity["Document"])
        if lost_response and writes == 1:
            raise httpx.ReadTimeout("response lost after commit", request=request)
        return httpx.Response(
            202,
            headers={"Content-Type": "multipart/mixed; boundary=unit"},
            content=batch_response([204, 204]),
        )

    token = Mock()
    token.token.return_value = "unit-only"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = AtomicTableLedger("https://ledger.example", "Unit", token, client)
        first = store.reserve(identity(), 500, epoch=EPOCH)
        second = store.reserve(identity(), 500, epoch=EPOCH)
        assert first.summary.active_reserved_tokens == second.summary.active_reserved_tokens == 500
        assert second.created is False and writes == 1
        with pytest.raises(LedgerProtocolConflict):
            store.reserve(identity(), 501, epoch=EPOCH)
        with pytest.raises(LedgerProtocolConflict):
            store.transition(identity(), "cancelled")


def test_atomic_storage_rejects_wildcard_read_etag() -> None:
    token = Mock()
    token.token.return_value = "unit-only"
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"ETag": "*"},
                json={"Document": "{}"},
            )
        )
    ) as client:
        store = AtomicTableLedger("https://ledger.example", "Unit", token, client)
        with pytest.raises(LedgerUnavailable):
            store.read_summary(SCOPE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("gateway_profile_id", "different"),
        ("native_gateway_id", "different"),
        ("correlation_id", "different"),
        ("model_id", "different"),
        ("user_id", "different"),
        ("usage_complete", False),
        ("estimated", True),
        ("tokens_consumed", "100"),
    ],
)
def test_completion_client_rejects_untrusted_identity_before_storage(
    field: str, value: Any
) -> None:
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    event = {
        **receipt.identity.model_dump(mode="json"),
        "user_id": SCOPE.scope_id,
        "usage_complete": True,
        "estimated": False,
        "ingest_source": "eventhub",
        "usage_event_source": "observer",
        "ingest_error": None,
        "input_tokens": 100,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "tokens_consumed": 100,
        "budget_execution": [receipt.model_dump(mode="json")],
        field: value,
    }
    client = BudgetCompletionClient("https://ledger.example", "Unit", lambda: "unit-only")
    with patch.object(client, "_request") as request, pytest.raises(ValueError):
        client.complete_event(event)
    request.assert_not_called()


def test_completion_client_uses_original_receipt_and_conditionally_settles_once() -> None:
    current, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    event = {
        **receipt.identity.model_dump(mode="json"),
        "user_id": SCOPE.scope_id,
        "usage_complete": True,
        "estimated": False,
        "ingest_source": "eventhub",
        "usage_event_source": "observer",
        "ingest_error": None,
        "input_tokens": 100,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "tokens_consumed": 100,
        "budget_execution": [receipt.model_dump(mode="json")],
    }
    client = BudgetCompletionClient("https://ledger.example", "Unit", lambda: "unit-only")
    with (
        patch.object(
            client,
            "_read",
            side_effect=[
                (current.model_dump(mode="json"), 'W/"summary"'),
                (attempt.model_dump(mode="json"), 'W/"attempt"'),
            ],
        ),
        patch.object(
            client,
            "_request",
            return_value=(
                202,
                {"content-type": "multipart/mixed; boundary=unit"},
                batch_response([204, 204]),
            ),
        ) as request,
    ):
        assert client.complete_event(event) == 1
    body = request.call_args.kwargs["body"]
    assert b'If-Match: W/"summary"' in body and b'If-Match: W/"attempt"' in body


@pytest.mark.parametrize("operation", ["complete_budget_settlement", "retry_budget_settlement"])
def test_sql_settlement_completion_is_bound_to_current_lease(operation: str) -> None:
    from turnstile_core.domain.ledger import LedgerSettlementTask

    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = None
    context = MagicMock()
    context.__enter__.return_value = connection
    task = LedgerSettlementTask(
        id=1, charge=charge(100), lease_token=TARGET, lease_until=NOW, attempts=1, created_at=NOW
    )
    repository = PostgreSqlBudgetExecutionMixin()
    with patch.object(repository, "_connection", return_value=context):
        result = (
            repository.complete_budget_settlement(task)
            if operation == "complete_budget_settlement"
            else repository.retry_budget_settlement(task, "unit_failure")
        )
    assert result is False
    statement, parameters = connection.execute.call_args.args
    assert "lease_token = %s" in statement and "lease_until > clock_timestamp()" in statement
    assert TARGET in parameters and "RETURNING id" in statement


@pytest.mark.parametrize("operation", ["pending_budget_policies", "pending_budget_completions"])
def test_execution_queries_keep_bounded_fair_order_and_avoid_infinity_fields(
    operation: str,
) -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlBudgetExecutionMixin()
    with patch.object(repository, "_connection", return_value=context):
        assert getattr(repository, operation)(limit=999) == []
    statement, parameters = connection.execute.call_args.args
    assert "ORDER BY" in statement and "LIMIT %s" in statement
    assert parameters == (32,)
    assert "SELECT *" not in statement


def test_scope_service_requires_durable_active_rollout_and_no_legacy_charges() -> None:
    repository, ledger = Mock(), Mock()
    service = BudgetScopeService(repository, ledger)
    repository.active_budget_execution_rollout.return_value = None
    assert service.prepare(now=NOW) == 0
    assert not ledger.mock_calls
    seed = LedgerScopeSeed(
        summary=LedgerSummary(
            scope=SCOPE,
            epoch=EPOCH,
            token_limit=0,
            enabled=False,
            enforce=True,
            confirmed_tokens=125,
            updated_at=NOW,
        ),
        policy=LedgerPolicyUpdate(
            scope=SCOPE, epoch=EPOCH, revision=1, token_limit=1000, enforce=True, enabled=True
        ),
    )
    repository.active_budget_execution_rollout.return_value = {
        "epoch": EPOCH,
        "gateway_profile_ids": [TARGET],
        "prepared_period": SCOPE.period_start,
    }
    repository.budget_scope_seed_candidates.return_value = [seed]
    ledger.list_reservations.return_value = [object()]
    assert service.prepare(now=NOW) == 0
    ledger.initialize.assert_not_called()
    ledger.write_policy.assert_not_called()
    repository.activate_budget_scope_seed.assert_not_called()
    repository.defer_budget_scope_seed.assert_called_once_with(SCOPE)
    ledger.list_reservations.return_value = []
    assert service.prepare(now=NOW) == 1
    ledger.initialize.assert_called_once_with(seed.summary)
    repository.activate_budget_scope_seed.assert_called_once_with(seed)
    repository.complete_budget_policy.assert_called_once_with(seed.policy)
    repository.budget_scope_seed_candidates.return_value = [
        seed.model_copy(update={"active": True})
    ]
    assert service.prepare(now=NOW) == 1
    ledger.initialize.assert_called_once()


@pytest.mark.parametrize("failure", ["transport", "expired", "lease_lost", "none"])
def test_settlement_service_only_acknowledges_confirmed_current_claims(failure: str) -> None:
    repository, ledger = Mock(), Mock()
    moment = datetime.now(UTC)
    task = LedgerSettlementTask(
        id=1,
        charge=charge(100),
        lease_token=TARGET,
        lease_until=moment + timedelta(seconds=-1 if failure == "expired" else 120),
        attempts=1,
        created_at=moment,
    )
    repository.claim_budget_settlement.side_effect = [task, None]
    repository.complete_budget_settlement.return_value = failure != "lease_lost"
    if failure == "transport":
        ledger.settle.side_effect = TimeoutError("sensitive details are not logged")
    assert BudgetSettlementService(repository, ledger).drain("unit-usage") == int(failure == "none")
    if failure in {"expired", "transport"}:
        repository.complete_budget_settlement.assert_not_called()
        repository.retry_budget_settlement.assert_called_once_with(
            task,
            "RuntimeError" if failure == "expired" else "TimeoutError",
        )
    else:
        repository.complete_budget_settlement.assert_called_once_with(task)
        repository.retry_budget_settlement.assert_not_called()
    if failure == "expired":
        ledger.settle.assert_not_called()


def test_completion_recovery_failure_does_not_invent_evidence() -> None:
    repository, ledger = Mock(), Mock()
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=1000)
    repository.pending_budget_completions.return_value = [receipt]
    ledger.read_attempt.side_effect = TimeoutError()
    assert BudgetRecoveryService(repository, ledger).completions() == 0
    repository.record_budget_completion.assert_called_once_with(receipt, None)


def test_execution_balance_does_not_fall_back_to_legacy_for_wrong_epoch() -> None:
    repository, ledger = Mock(), Mock()
    repository.budget_execution_status.return_value = {
        "state": "active",
        "epoch": TARGET,
        "policy_revision": 1,
    }
    ledger.read_summary.return_value = summary()
    result = execution_budget_view(repository, ledger, SCOPE)
    assert result is not None
    assert result["ledger_status"] == "unavailable" and result["available_tokens"] is None


def test_scope_seed_reuses_saved_opening_balance_and_never_selects_infinity() -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [
        {
            **SCOPE.model_dump(),
            "epoch": EPOCH,
            "state": "prepared",
            "seed_confirmed_tokens": 125,
            "seed_checked_at": NOW,
            "policy_revision": 1,
            "desired_limit": 1000,
            "desired_enforce": True,
            "desired_enabled": True,
        }
    ]
    repository = PostgreSqlBudgetExecutionMixin()
    with patch.object(repository, "_connection", return_value=context):
        seeds = repository.budget_scope_seed_candidates(limit=999)
    assert len(seeds) == 1 and seeds[0].summary.confirmed_tokens == 125
    assert not seeds[0].summary.enabled
    statement, parameters = connection.execute.call_args.args
    assert "FOR UPDATE OF scope SKIP LOCKED" in statement and parameters[-1] == 32
    assert "recovery_checked_at" not in statement.split("FROM budget_execution_scope")[0]
    assert connection.execute.call_count == 1
