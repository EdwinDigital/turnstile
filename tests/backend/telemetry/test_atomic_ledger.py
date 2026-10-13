from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from typing import Any, Literal
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

import httpx
import pytest

from turnstile_core.domain.budget_completion import (
    exact_usd_amount,
    normalize_terminal_rejection,
    rejection_completion,
    usd_completion_charge,
    utc_time,
    validate_completion,
)
from turnstile_core.domain.ledger import (
    BudgetPriceProjection,
    BudgetReservationResolution,
    BudgetReservationResolutionWrite,
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
    resolve_ledger,
    settle_ledger,
    transition_ledger_attempt,
    update_ledger_policy,
)
from turnstile_core.domain.models import ModelPrice
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
    BudgetReservationService,
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


@pytest.mark.parametrize("value", [0, "0", "0.00000000000001", Decimal("0.00515"),
                                  "9999999999999999.99999999999999"])
def test_exact_usd_amount_retains_numeric_precision(value: Any) -> None:
    assert exact_usd_amount(value) == Decimal(value)


@pytest.mark.parametrize("value", [True, False, 0.1, -1, "NaN", "Infinity", "1e16",
                                  "0.000000000000001", "invalid"])
def test_exact_usd_amount_rejects_float_boolean_and_out_of_range(value: Any) -> None:
    with pytest.raises(ValueError):
        exact_usd_amount(value)


def usd_projection(price: dict[str, Any] | None = None) -> dict[str, Any]:
    price = price or {
        "input_price_per_million": 10, "cached_price_per_million": 1,
        "cache_write_price_per_million": 20, "output_price_per_million": 30,
    }
    rate_fields = (
        "input_rate_units", "cached_rate_units", "cache_write_rate_units", "output_rate_units",
    )
    schedules = [{
        "min_input_tokens": tier.get("min_input_tokens", 0),
        "max_input_tokens": tier.get("max_input_tokens"),
        **dict(zip(rate_fields, [int(Decimal(str(tier[field])) * 100_000_000) for field in (
            "input_price_per_million", "cached_price_per_million",
            "cache_write_price_per_million", "output_price_per_million",
        )], strict=True)),
    } for tier in price.get("context_tiers", [price])]
    content = {"price": price, "rate_schedules": schedules}
    return {"version": 1, "model_id": "unit-model", **content,
            "price_sha256": hashlib.sha256(json.dumps(
                content, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()}


def test_usd_completion_charge_uses_each_cache_bucket_exactly_once() -> None:
    projection = usd_projection()
    evidence = completion(400, input_tokens=100, cached_tokens=200,
                          cache_write_tokens=50, output_tokens=100)
    assert usd_completion_charge(projection, validate_completion(evidence)) == Decimal("0.00515")
    changed = {**projection, "price": {**projection["price"], "input_price_per_million": 99}}
    with pytest.raises(ValueError, match="digest differs"):
        usd_completion_charge(changed, evidence)
    mismatched = {**projection, "rate_schedules": [
        {**projection["rate_schedules"][0], "input_rate_units": 1},
    ]}
    content = {name: mismatched[name] for name in ("price", "rate_schedules")}
    mismatched["price_sha256"] = hashlib.sha256(json.dumps(
        content, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    with pytest.raises(ValueError, match="rate schedule differs"):
        usd_completion_charge(mismatched, evidence)


def test_model_price_decimal_cost_matches_admission_snapshot_without_display_rounding() -> None:
    projection = usd_projection()
    price = ModelPrice.model_validate(projection["price"])
    actual = price.cost_decimal(100, 200, 100, 50)
    assert actual == Decimal("0.00515")
    assert actual == usd_completion_charge(projection, completion(
        400, input_tokens=100, cached_tokens=200, cache_write_tokens=50, output_tokens=100,
    ))
    precise = ModelPrice(input_price_per_million=0.00000001, cached_price_per_million=0,
                         cache_write_price_per_million=0, output_price_per_million=0)
    assert precise.cost_decimal(1, 0, 0) == Decimal("0.00000000000001")
    assert precise.cost(1, 0, 0) == 0


def test_usd_ledger_reserves_and_settles_exact_cache_buckets_once() -> None:
    model_id = UUID("90000000-0000-4000-8000-000000000003")
    price = ModelPrice(input_price_per_million=10, cached_price_per_million=1,
                       cache_write_price_per_million=20, output_price_per_million=30)
    projection = BudgetPriceProjection.create(model_id, price, NOW)
    state = summary(limit=0, budget_mode="usd", usd_limit=Decimal("0.05"))
    request = identity("usd-boundary", model_id=str(model_id))
    admitted, attempt = reserve_ledger(
        state, request, 2_000, NOW, reserved_usd=Decimal("0.05"), price_snapshot=projection,
    )
    assert admitted.available_usd == 0
    with pytest.raises(LedgerAdmissionDenied, match="budget_request_bound_exceeds_balance"):
        reserve_ledger(
            state, identity("usd-over", model_id=str(model_id)), 2_001, NOW,
            reserved_usd=Decimal("0.05000000000001"), price_snapshot=projection,
        )
    actual = Decimal("0.00515")
    task = charge(400, identity=request, budget_mode="usd", actual_usd=actual,
                  price_snapshot=projection)
    settled, completed = settle_ledger(admitted, attempt, task, NOW + timedelta(seconds=1))
    assert settled.confirmed_usd == actual
    assert settled.active_reserved_usd == 0
    assert settled.confirmed_tokens == 0
    assert completed.actual_tokens == 400 and completed.actual_usd == actual
    duplicate, duplicate_attempt = settle_ledger(settled, completed, task, NOW)
    assert duplicate is settled and duplicate_attempt is completed


@pytest.mark.parametrize("state", ["reserved", "dispatching", "uncertain", "upper_bound"])
def test_usd_complete_receipt_uses_original_price_and_clears_only_its_hold(state: str) -> None:
    model_id = UUID("90000000-0000-4000-8000-000000000004")
    price = ModelPrice(input_price_per_million=10, cached_price_per_million=1,
                       cache_write_price_per_million=20, output_price_per_million=30)
    projection = BudgetPriceProjection.create(model_id, price, NOW)
    request = identity("usd-complete", model_id=str(model_id))
    current, attempt = reserve_ledger(
        summary(limit=0, budget_mode="usd", usd_limit=Decimal("1")), request, 500, NOW,
        reserved_usd=Decimal("0.5"), price_snapshot=projection,
    )
    if state in {"dispatching", "uncertain"}:
        current, attempt = transition_ledger_attempt(current, attempt, "dispatching", NOW)
    if state == "uncertain":
        current, attempt = transition_ledger_attempt(current, attempt, "uncertain", NOW)
    if state == "upper_bound":
        current, attempt = transition_ledger_attempt(current, attempt, "upper_bound", NOW)
        assert current.upper_bound_usd == Decimal("0.5")
        assert current.available_usd == Decimal("0.5")
    receipt = LedgerAdmissionReceipt(identity=request, epoch=EPOCH, reserved_tokens=500,
                                     budget_mode="usd", reserved_usd=Decimal("0.5"),
                                     price_snapshot=projection)
    evidence = completion(400, input_tokens=100, cached_tokens=200,
                          cache_write_tokens=50, output_tokens=100)
    settled, retained = complete_ledger(current, attempt, receipt, evidence, NOW)
    assert settled.confirmed_usd == Decimal("0.00515")
    assert settled.active_reserved_usd == settled.upper_bound_usd == 0
    assert retained.price_snapshot == projection
    duplicate, duplicate_attempt = complete_ledger(
        settled, retained, receipt, {**evidence, "source": "observer"}, NOW,
    )
    assert duplicate is settled and duplicate_attempt is retained


@pytest.mark.parametrize("mode", ["tokens", "usd"])
def test_approved_resolution_preserves_unknown_usage_and_late_exact_charge(
    mode: Literal["tokens", "usd"],
) -> None:
    model_id = UUID("90000000-0000-4000-8000-000000000005")
    projection = BudgetPriceProjection.create(
        model_id, ModelPrice(input_price_per_million=1, cached_price_per_million=1,
                            cache_write_price_per_million=1, output_price_per_million=1), NOW,
    )
    request = identity("approved-resolution", model_id=str(model_id))
    settings: dict[str, Any] = {"budget_mode": "usd", "usd_limit": Decimal("1")} \
        if mode == "usd" else {}
    reservation: dict[str, Any] = {"reserved_usd": Decimal("0.5"), "price_snapshot": projection} \
        if mode == "usd" else {}
    current, attempt = reserve_ledger(summary(**settings), request, 500, NOW, **reservation)
    current, attempt = transition_ledger_attempt(current, attempt, "upper_bound", NOW)
    receipt = LedgerAdmissionReceipt(identity=request, epoch=EPOCH, reserved_tokens=500,
                                     budget_mode=mode, **reservation)
    approved = BudgetReservationResolution(
        id=UUID("90000000-0000-4000-8000-000000000006"), receipt=receipt,
        actor="owner@example.test", reason="provider_result_unavailable",
        evidence_sha256="a" * 64, approved_at=NOW,
    ).model_dump(mode="json")
    released, retained = resolve_ledger(current, attempt, approved, NOW)
    assert retained.state == "released"
    assert retained.actual_tokens is None
    assert retained.actual_usd is None
    assert retained.evidence_rank == 2
    assert released.active_reserved_tokens == released.upper_bound_tokens == 0
    assert released.active_reserved_usd == released.upper_bound_usd == 0
    duplicate, duplicate_attempt = resolve_ledger(released, retained, approved, NOW)
    assert duplicate is released and duplicate_attempt is retained
    settled, completed = complete_ledger(released, retained, receipt, completion(100), NOW)
    assert completed.actual_tokens == 100 and completed.evidence_rank == 3
    if mode == "usd":
        assert completed.actual_usd == settled.confirmed_usd == Decimal("0.0001")
    else:
        assert settled.confirmed_tokens == 100


def test_native_rejection_requires_exact_declared_rule_before_zero_charge() -> None:
    observed = normalize_terminal_rejection("microsoft-decision", 400, "application/json", {
        "error": {"type": "invalid_request_error", "code": "question_invalid", "param": None},
    })
    assert observed is not None
    rule = {key: observed[key] for key in (
        "status_code", "error_type", "error_code", "error_param",
    )}
    policy_document = {"version": 2, "operation": "microsoft-decision",
                       "evidence_sha256": "a" * 64, "verified_at": NOW.isoformat(),
                       "rules": [rule]}
    assert rejection_completion(None, observed, source="apim", observed_at=NOW,
                                admitted_at=NOW) is None
    evidence = rejection_completion(policy_document, observed, source="apim", observed_at=NOW,
                                    admitted_at=NOW)
    assert evidence is not None and evidence["total_tokens"] == 0
    assert evidence["rejection"]["policy_evidence_sha256"] == "a" * 64
    assert normalize_terminal_rejection("microsoft-decision", 408, "application/json", {
        "error": {"code": "timeout"},
    }) is None


def test_token_ledger_json_shape_does_not_add_usd_fields() -> None:
    state, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    assert "budget_mode" not in state.model_dump(mode="json")
    assert "usd_limit" not in state.model_dump(mode="json")
    assert "confirmed_usd" not in state.model_dump(mode="json")
    assert "budget_mode" not in attempt.model_dump(mode="json")
    assert "price_snapshot" not in attempt.model_dump(mode="json")


@pytest.mark.parametrize("cached_tokens,output_tokens,expected", [
    (200, 100, "0.00515"), (201, 100, "0.010302"), (200, 1000, "0.03215"),
])
def test_usd_completion_charge_selects_tier_by_input_and_cache_only(
    cached_tokens: int, output_tokens: int, expected: str,
) -> None:
    price = {
        "context_tiers": [{
            "min_input_tokens": 0, "max_input_tokens": 300,
            "input_price_per_million": 10, "cached_price_per_million": 1,
            "cache_write_price_per_million": 20, "output_price_per_million": 30,
        }, {
            "min_input_tokens": 301, "max_input_tokens": None,
            "input_price_per_million": 20, "cached_price_per_million": 2,
            "cache_write_price_per_million": 40, "output_price_per_million": 60,
        }],
    }
    evidence = completion(100 + cached_tokens + output_tokens, input_tokens=100,
                          cached_tokens=cached_tokens, cache_write_tokens=50,
                          output_tokens=output_tokens)
    assert usd_completion_charge(usd_projection(price), evidence) == Decimal(expected)


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


def test_atomic_usd_replay_binds_amount_and_immutable_price() -> None:
    model_id = UUID("90000000-0000-4000-8000-000000000003")
    projection = BudgetPriceProjection.create(model_id, ModelPrice.model_validate(
        usd_projection()["price"],
    ), NOW)
    attempt_id = identity("unit-table-usd", model_id=str(model_id))
    state, attempt = reserve_ledger(
        summary(0, budget_mode="usd", usd_limit=Decimal("1")), attempt_id, 500, NOW,
        reserved_usd=Decimal("0.5"), price_snapshot=projection,
    )
    stored = {
        "S": state.model_dump(mode="json"), attempt_id.row_key: attempt.model_dump(mode="json"),
    }
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"ETag": 'W/"unit"'}, json={"Document": json.dumps(
            stored["S" if "RowKey='S'" in str(request.url) else attempt_id.row_key],
        )},
    ))) as client:
        token = Mock()
        token.token.return_value = "unit-only"
        store = AtomicTableLedger("https://ledger.example", "Unit", token, client)
        replay = store.reserve(attempt_id, 500, epoch=EPOCH,
                               reserved_usd=Decimal("0.5"), price_snapshot=projection)
        assert replay.created is False and replay.summary.active_reserved_usd == Decimal("0.5")
        with pytest.raises(LedgerProtocolConflict):
            store.reserve(attempt_id, 500, epoch=EPOCH,
                          reserved_usd=Decimal("0.4"), price_snapshot=projection)
        receipt = LedgerAdmissionReceipt(identity=attempt_id, epoch=EPOCH, reserved_tokens=500,
                                         budget_mode="usd", reserved_usd=Decimal("0.5"),
                                         price_snapshot=projection)
        assert store.read_attempt(receipt) == attempt
        with pytest.raises(LedgerProtocolConflict):
            store.read_attempt(receipt.model_copy(update={"reserved_usd": Decimal("0.4")}))


def test_atomic_price_projection_rejects_equal_time_change_and_older_writer() -> None:
    model_id = UUID("90000000-0000-4000-8000-000000000003")
    projection = BudgetPriceProjection.create(model_id, ModelPrice.model_validate(
        usd_projection()["price"],
    ), NOW)
    changed = BudgetPriceProjection.create(model_id, ModelPrice(
        input_price_per_million=11, cached_price_per_million=1,
        cache_write_price_per_million=20, output_price_per_million=30,
    ), NOW)
    with httpx.Client() as client:
        store = AtomicTableLedger("https://ledger.example", "Unit", Mock(), client)
        with patch.object(store, "_read", return_value=(projection.model_dump(), 'W/"unit"')), \
                patch.object(store, "replace_if_match") as writer:
            assert store.read_model_price(model_id) == projection
            assert store.project_model_price(projection) is False
            assert store.project_model_price(changed, create_only=True) is False
            with pytest.raises(LedgerProtocolConflict, match="timestamp"):
                store.project_model_price(changed)
            assert store.project_model_price(changed.model_copy(update={
                "updated_at": NOW - timedelta(seconds=1),
            })) is False
            writer.assert_not_called()


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
    from turnstile_core.domain.gateway_targets import canonical_usage_id_v2

    current, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    service_id = (
        "/subscriptions/unit/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit"
    )
    usage_id = canonical_usage_id_v2(
        TARGET, service_id, receipt.identity.native_gateway_id, receipt.identity.correlation_id,
    )
    event = {
        **receipt.identity.model_dump(mode="json"),
        "event_schema_version": 2,
        "apim_service_id": service_id,
        "id": usage_id,
        "usage_id": usage_id,
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
    assert parameters == ((None, None, 32) if operation == "pending_budget_completions" else (32,))
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


@pytest.mark.parametrize("budget_mode", ["tokens", "usd"])
def test_scope_seed_reuses_saved_opening_balance_and_never_selects_infinity(
    budget_mode: str,
) -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [
        {
            **SCOPE.model_dump(),
            "epoch": EPOCH,
            "state": "prepared",
            "budget_mode": budget_mode,
            "seed_confirmed_tokens": 125 if budget_mode == "tokens" else None,
            "seed_confirmed_usd": Decimal("0.00000000000001") if budget_mode == "usd" else None,
            "seed_checked_at": NOW,
            "policy_revision": 1,
            "desired_limit": 1000 if budget_mode == "tokens" else 0,
            "desired_usd_limit": Decimal("1") if budget_mode == "usd" else None,
            "desired_enforce": True,
            "desired_enabled": True,
        }
    ]
    repository = PostgreSqlBudgetExecutionMixin()
    with patch.object(repository, "_connection", return_value=context):
        seeds = repository.budget_scope_seed_candidates(limit=999)
    assert len(seeds) == 1
    assert seeds[0].summary.confirmed_tokens == (125 if budget_mode == "tokens" else 0)
    assert seeds[0].summary.confirmed_usd == (Decimal("0.00000000000001")
                                            if budget_mode == "usd" else 0)
    assert seeds[0].summary.budget_mode == seeds[0].policy.budget_mode == budget_mode
    assert not seeds[0].summary.enabled
    statement, parameters = connection.execute.call_args.args
    assert "FOR UPDATE OF scope SKIP LOCKED" in statement and parameters[-1] == 32
    assert "recovery_checked_at" not in statement.split("FROM budget_execution_scope")[0]
    assert connection.execute.call_count == 1


@pytest.mark.parametrize("settled", [False, True])
def test_owner_resolution_service_never_waives_a_settlement_decision(settled: bool) -> None:
    _, attempt = reserve_ledger(summary(), identity(), 500, NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    write = BudgetReservationResolutionWrite(
        id=UUID(int=88), receipt=receipt, reason="provider_result_unavailable",
        evidence_sha256="a" * 64, accepts_late_usage=True,
    )
    approved = BudgetReservationResolution(
        id=write.id, receipt=receipt, actor="owner@example.test", reason=write.reason,
        evidence_sha256=write.evidence_sha256, approved_at=NOW,
    )
    repository, ledger = Mock(), Mock(spec=AtomicTableLedger)
    repository.get_budget_resolution.return_value = None
    repository.approve_budget_resolution.return_value = approved
    ledger.read_attempt.return_value = attempt.model_copy(update={"evidence_rank": 3}) \
        if settled else attempt
    service = BudgetReservationService(repository, ledger)
    if settled:
        with pytest.raises(LedgerProtocolConflict, match="settlement decision"):
            service.approve(write, "owner@example.test")
        repository.approve_budget_resolution.assert_not_called()
    else:
        assert service.approve(write, "owner@example.test") == approved
        repository.approve_budget_resolution.assert_called_once_with(write, "owner@example.test")
        repository.get_budget_resolution.return_value = approved
        ledger.read_attempt.reset_mock()
        assert service.approve(write, "owner@example.test") == approved
        ledger.read_attempt.assert_not_called()
        with pytest.raises(LedgerProtocolConflict, match="idempotency"):
            service.approve(write.model_copy(update={"evidence_sha256": "b" * 64}),
                            "owner@example.test")


@pytest.mark.parametrize("condition", ["ready", "held", "unpriced", "drifted", "stale"])
def test_policy_unit_switch_requires_drained_actual_usage_not_converted_consumption(
    condition: str,
) -> None:
    update = LedgerPolicyUpdate(scope=SCOPE, epoch=EPOCH, revision=2, token_limit=0,
                                budget_mode="usd", usd_limit=Decimal("10"),
                                enforce=True, enabled=True)
    current = summary(1000, confirmed_tokens=500, policy_revision=3 if condition == "stale" else 1)
    repository, ledger = Mock(), Mock(spec=AtomicTableLedger)
    repository.pending_budget_policies.return_value = [update]
    repository.registry.return_value = {"models": []}
    repository.budget_switch_usage.return_value = {
        "tokens": 501 if condition == "drifted" else 500,
        "usd": None if condition == "unpriced" else Decimal("0.12"),
    }
    ledger.read_summary.return_value = current
    ledger.quiesce_budget_scope.return_value = current.model_copy(update={
        "enabled": False, "active_reserved_tokens": 1 if condition == "held" else 0,
    })
    ledger.pending_dispatch_page.return_value = ([], None, None)
    ledger.write_policy.return_value = summary(0, budget_mode="usd", usd_limit=Decimal("10"),
                                              policy_revision=2, confirmed_usd=Decimal("0.12"))
    applied = BudgetRecoveryService(repository, ledger).policies()
    if condition == "ready":
        assert applied == 1
        assert ledger.write_policy.call_args.kwargs["opening_balance"] == Decimal("0.12")
        assert ledger.write_policy.call_args.kwargs["budget_mode"] == "usd"
        repository.complete_budget_policy.assert_called_once_with(update)
    else:
        assert applied == 0
        ledger.write_policy.assert_not_called()
        repository.complete_budget_policy.assert_not_called()
    if condition == "stale":
        ledger.quiesce_budget_scope.assert_not_called()


def test_switch_balance_sql_binds_actual_usage_and_applied_request_evidence() -> None:
    from psycopg._queries import PostgresQuery
    from psycopg.adapt import Transformer

    update = LedgerPolicyUpdate(scope=SCOPE, epoch=EPOCH, revision=2, token_limit=0,
                                budget_mode="usd", usd_limit=Decimal("1"),
                                enforce=True, enabled=True)
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"tokens": 500, "usd": Decimal("0.12")}
    repository = PostgreSqlBudgetExecutionMixin()
    with patch.object(repository, "_connection", return_value=context):
        assert repository.budget_switch_usage(update) == {"tokens": 500, "usd": Decimal("0.12")}
    query, parameters = connection.execute.call_args.args
    prepared = PostgresQuery(Transformer())
    prepared.convert(query, parameters)
    assert "budget_scope_confirmed_tokens(" in query and "budget_usage_usd_cost(" in query
    assert "count(*) FILTER (WHERE cost IS NULL) > 0 THEN NULL" in query
    assert "budget_pre_dispatch_denial" in query and "dispatched.dispatch_id IS NOT NULL" in query
    assert "task.applied_revision < task.revision" in query
    assert b"NOT LIKE 'rejection:%'" in prepared.query
    assert parameters[-2:] == (2, "usd")


def test_completion_reader_retains_usd_amount_and_original_admission_price() -> None:
    model_id = UUID("90000000-0000-4000-8000-000000000003")
    projection = BudgetPriceProjection.create(model_id, ModelPrice.model_validate(
        usd_projection()["price"],
    ), NOW)
    expected = LedgerAdmissionReceipt(identity=identity(model_id=str(model_id)), epoch=EPOCH,
                                      reserved_tokens=500, budget_mode="usd",
                                      reserved_usd=Decimal("0.00000000000001"),
                                      price_snapshot=projection)
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [expected.model_dump()]
    repository = PostgreSqlBudgetExecutionMixin()
    with patch.object(repository, "_connection", return_value=context):
        result = repository.pending_budget_completions(999, usage_id="unit-usage")
    assert result == [expected]
    statement, parameters = connection.execute.call_args.args
    assert "execution.price_snapshot" in statement
    assert parameters == ("unit-usage", "unit-usage", 32)
