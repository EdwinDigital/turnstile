from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch, sentinel
from uuid import UUID

import pytest

from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    UsageApplicationAttribution,
)
from turnstile_core.domain.models import ModelIdentity, ModelPrice, ModelPriceTier, TokenUsageRecord
from turnstile_core.ingestion.processor import (
    CoefficientResolver,
    UsageProcessor,
    calculate_et,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services.application_access import ApplicationAccessService

GATEWAY_ID = UUID("10000000-0000-4000-8000-000000000001")


class RecordingRepository:
    def __init__(
        self,
        prices: dict[str, ModelPrice] | None = None,
        identities: dict[str, ModelIdentity] | None = None,
    ) -> None:
        self.records: list[TokenUsageRecord] = []
        self.prices = prices or {}
        self.identities = identities or {}
        self.price_reads = 0
        self.identity_reads = 0

    def write_token_usage(
        self, record: TokenUsageRecord, application: object | None = None
    ) -> None:
        assert application is None
        self.records.append(record)

    def model_prices(self) -> dict[str, ModelPrice]:
        self.price_reads += 1
        return self.prices

    def model_identities(self) -> dict[str, ModelIdentity]:
        self.identity_reads += 1
        return self.identities

    def gateway_application_attribution_map(
        self, gateway_profile_id: UUID
    ) -> list[dict[str, object]]:
        del gateway_profile_id
        return []


def event(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "usage-1",
        "ts": datetime(2026, 7, 17, tzinfo=UTC).isoformat(),
        "team": "platform",
        "user": "lei",
        "agent": "delivery-engineer",
        "workflow": "pull-request-review",
        "run_id": "run-1",
        "turn_index": 1,
        "provider": "aoai",
        "model": "gpt-4.1-mini",
        "input_tokens": 1000,
        "cached_tokens": 200,
        "output_tokens": 100,
        "latency_ms": 1200,
        "status": "success",
        "ingest_source": "eventhub",
    }
    value.update(overrides)
    return value


def test_calculate_et() -> None:
    assert calculate_et(1000, 200, 100, 0.5) == 710


@pytest.mark.parametrize(
    "fields",
    [
        {"input_tokens": True},
        {"input_tokens": "1000"},
        {"input_tokens": 1000.0},
        {"cache_write_tokens": 201},
        {"tokens_consumed": 9},
    ],
)
def test_invalid_usage_cannot_claim_exact_evidence(fields: dict[str, object]) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(**fields))
    assert record is not None
    assert record.estimated
    assert record.ingest_error == "usage_validation_failed"


def test_coefficient_snapshot_is_written() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"aoai/gpt-4.1-mini": 0.35}))
    assert processor.process(event()) is True
    assert repository.records[0].et_coeff_m == 0.35
    assert repository.records[0].et == 497
    assert repository.records[0].request_id == "usage-1"
    assert repository.records[0].correlation_id == "usage-1"
    assert repository.records[0].status_code == 200


def test_enterprise_attribution_and_failure_fields_are_preserved() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    raw = event(
        request_id="request-42",
        correlation_id="apim-42",
        organization="Contoso",
        organization_id="org-1",
        department="Platform",
        department_id="department-1",
        project="FinOps",
        project_id="project-1",
        agent_id="agent-1",
        user_id="user-1",
        model_id="deployment-1",
        request_source="agent-console",
        status="429",
        status_code=429,
        estimated_cost=0.0123,
        error_message="rate limited",
    )
    assert processor.process(raw) is True
    record = repository.records[0]
    assert record.organization_id == "org-1"
    assert record.department_id == "department-1"
    assert record.project_id == "project-1"
    assert record.agent_id == "agent-1"
    assert record.user_id == "user-1"
    assert record.model_id == "deployment-1"
    assert record.request_source == "agent-console"
    assert record.usage_domain == "apim"
    assert record.status_code == 429
    assert record.estimated_cost == 0.0123
    assert record.error_message == "rate limited"


def test_apim_subscription_is_resolved_to_immutable_application_attribution() -> None:
    repository = InMemoryRepository()
    ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=GATEWAY_ID,
            discovered_at=datetime(2026, 8, 26, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="outline-assistant",
                    display_name="Outline Assistant",
                    state="active",
                    scope_type="product",
                    scope_id="finops-applications",
                    scope_exists=True,
                    application_type="service",
                    system_managed=False,
                )
            ],
        ),
        "application-sync-worker",
    )
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(
        event(
            id="application-caller-request",
            request_id="application-caller-request",
            correlation_id="application-attempt",
            gateway_profile_id=str(GATEWAY_ID),
            apim_subscription_id="outline-assistant",
            application_actor_type="service",
            application_actor_id="service:outline-assistant",
            application_admission="ok",
        )
    )

    attribution = repository.usage_application_attributions["application-attempt"]
    assert attribution.application_name_snapshot == "Outline Assistant"
    assert attribution.actor_type == "service"
    assert attribution.actor_id == "service:outline-assistant"
    assert attribution.application_admission == "ok"


def test_copilot_assistant_events_are_classified_outside_the_apim_domain() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event(request_source="copilot-assistant")) is True
    assert repository.records[0].usage_domain == "github_copilot"


def test_stream_without_breakdown_is_persisted_without_inventing_a_split() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    raw = event(input_tokens=None, cached_tokens=None, output_tokens=None, tokens_consumed=1000)
    assert processor.process(raw) is True
    record = repository.records[0]
    assert record.input_tokens == 0
    assert record.cached_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated is True
    assert record.ingest_error == "stream_usage_breakdown_unavailable"


def test_apim_retries_keep_distinct_attempts_for_one_request() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    for correlation_id, status_code in (("attempt-first", 200), ("attempt-second", 429)):
        assert processor.process(
            event(
                id="caller-request",
                request_id="caller-request",
                correlation_id=correlation_id,
                status=str(status_code),
                status_code=status_code,
            )
        )

    assert [record.id for record in repository.usage_records] == [
        "attempt-first",
        "attempt-second",
    ]
    assert {record.request_id for record in repository.usage_records} == {"caller-request"}
    assert [record.status_code for record in repository.usage_records] == [200, 429]


def test_apim_redelivery_upgrades_one_attempt_despite_different_event_ids() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    identity = {"request_id": "caller-request", "correlation_id": "gateway-attempt"}

    assert processor.process(
        event(
            **identity,
            id="gateway-event",
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            budget_admission="ok",
        )
    )
    assert processor.process(event(**identity, id="observer-event", input_tokens=72))
    assert processor.process(event(**identity, id="redelivered-event", input_tokens=999))

    assert len(repository.usage_records) == 1
    record = repository.usage_records[0]
    assert record.id == "gateway-attempt"
    assert record.request_id == "caller-request"
    assert record.correlation_id == "gateway-attempt"
    assert record.estimated is False
    assert record.input_tokens == 72
    assert record.budget_admission == "ok"


@pytest.mark.parametrize("estimated", [False, True])
def test_apim_redelivery_preserves_pre_upgrade_identity(estimated: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    identity = {"request_id": "legacy-caller", "correlation_id": "legacy-attempt"}
    legacy = processor.normalize(event(**identity, id="legacy-caller", input_tokens=100))
    assert legacy is not None
    repository.write_token_usage(
        legacy.model_copy(update={"id": "legacy-caller", "estimated": estimated})
    )

    assert processor.process(event(**identity, id="replayed-event", input_tokens=72))
    assert processor.process(event(**identity, id="second-delivery", input_tokens=999))

    assert len(repository.usage_records) == 1
    stored = repository.usage_records[0]
    assert stored.id == "legacy-caller"
    assert stored.request_id == "legacy-caller"
    assert stored.correlation_id == "legacy-attempt"
    assert stored.estimated is False
    assert stored.input_tokens == (72 if estimated else 100)


@pytest.mark.parametrize("existing_id", [None, "legacy-caller"])
def test_postgres_reuses_attempt_identity_for_usage_and_attribution(
    existing_id: str | None,
) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(id="caller", correlation_id="gateway-attempt"))
    assert record is not None
    repository = object.__new__(PostgreSqlOpsDbProxy)
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = (
        [
            {
                "id": existing_id,
                "correlation_id": record.correlation_id,
                "usage_domain": "apim",
                "user_id": record.user_id,
            }
        ]
        if existing_id is not None
        else []
    )
    application = cast(UsageApplicationAttribution, sentinel.application)
    expected_id = existing_id or record.id
    with (
        patch.object(repository, "_connection") as connect,
        patch.object(repository, "_write_usage_application_attribution") as attribute,
    ):
        connect.return_value.__enter__.return_value = connection
        repository.write_token_usage(record, application)
        attribute.assert_called_once_with(connection, expected_id, application)

    assert connection.execute.call_count == 3
    lock, lookup, write = connection.execute.call_args_list
    assert lock.args[1] == (f"apim-attempt:{record.correlation_id}",)
    assert "LIMIT 2 FOR UPDATE" in lookup.args[0]
    assert lookup.args[1] == (record.correlation_id, record.id)
    assert write.args[1]["id"] == expected_id
    assert write.args[1]["request_id"] == record.request_id
    assert write.args[1]["correlation_id"] == record.correlation_id
    assert record.id == "gateway-attempt"


@pytest.mark.parametrize("observer_first", (False, True))
def test_runtime_authority_wins_both_event_orders_without_repricing(observer_first: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    logical = event(id="runtime-order", runtime="Logical primary", input_tokens=120)
    measured = event(
        id="runtime-order", runtime="Measured secondary", input_tokens=240,
        runtime_authoritative=True,
    )
    first, second = (measured, logical) if observer_first else (logical, measured)
    assert processor.process(first)
    before = repository.usage_records[-1].model_dump()
    assert processor.process(second)
    assert repository.usage_records[-1].model_dump() == {**before, "runtime": "Measured secondary"}
    assert len(repository.usage_records) == 1
    assert "runtime_authoritative" not in repository.usage_records[-1].model_dump()


@pytest.mark.parametrize("has_admission", (False, True))
def test_runtime_only_correction_keeps_every_other_exact_field(has_admission: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    logical = event(
        id="runtime-only", runtime="Logical primary", input_tokens=123,
        cache_write_tokens=50, estimated_cost=1.25,
        budget_admission="ok" if has_admission else None,
        model_admission="ok" if has_admission else None,
    )
    observed = event(
        id="runtime-only", runtime="Measured secondary", runtime_authoritative=True,
        model="different-unpriced-model", input_tokens=456, cached_tokens=600,
        cache_write_tokens=80, output_tokens=900, latency_ms=9999, estimated_cost=99.0,
    )
    assert processor.process(logical)
    expected = {**repository.usage_records[-1].model_dump(), "runtime": "Measured secondary"}
    for incoming in (observed, observed, logical):
        assert processor.process(incoming)
        assert repository.usage_records[-1].model_dump() == expected
    assert len(repository.usage_records) == 1


@pytest.mark.parametrize("overrides", (
    {"estimated": True}, {"input_tokens": None}, {"runtime": "unattributed"},
    {"runtime": ""}, {"ingest_source": "copilot_cli"}, {"ingest_source": "gateway"},
    {"request_source": "copilot-assistant-tools"},
))
def test_runtime_authority_rejects_unmeasured_or_non_apim_sources(
    overrides: dict[str, object],
) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    raw = event(runtime="Measured runtime", runtime_authoritative=True)
    raw.update(overrides)
    record = processor.normalize(raw)
    assert record is not None
    assert record.runtime_authoritative is False


@pytest.mark.parametrize("authoritative", (False, True))
def test_postgres_runtime_authority_is_a_transient_bound_parameter(authoritative: bool) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(
        runtime="Measured runtime", runtime_authoritative=authoritative,
        request_id="caller", correlation_id="attempt",
    ))
    assert record is not None
    repository = object.__new__(PostgreSqlOpsDbProxy)
    with patch.object(repository, "_connection") as connect:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [
            {
                "id": "legacy-row",
                "correlation_id": record.correlation_id,
                "usage_domain": "apim",
                "user_id": record.user_id,
            }
        ]
        repository.write_token_usage(record)
    statement, values = connection.execute.call_args.args
    assert values["id"] == "legacy-row"
    assert values["runtime_authoritative"] is authoritative
    assert values["runtime"] == record.runtime
    assert "runtime_authoritative" not in record.model_dump()
    assert "runtime_authoritative" not in statement.split(") VALUES", 1)[0]
    assert "existing.runtime IS DISTINCT FROM EXCLUDED.runtime" in statement
    assert "input_tokens = CASE WHEN existing.estimated" in statement
    assert connection.execute.call_count == 3


@pytest.mark.parametrize("failure", ["ambiguous", "identity", "attribution"])
def test_identity_failure_preserves_usage_attribution_and_receipt(failure: str) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(correlation_id="conflict", estimated=True))
    assert record is not None
    attribution = UsageApplicationAttribution(
        application_id=GATEWAY_ID,
        application_subscription_id=GATEWAY_ID,
        application_name_snapshot="Unit",
        apim_subscription_id="unit",
        actor_type="service",
        actor_id="service:unit",
    )
    repository.usage_records.append(record)
    repository.usage_application_attributions[record.id] = attribution
    incoming = record.model_copy(update={"estimated": False, "input_tokens": 90})
    if failure == "ambiguous":
        repository.usage_records.append(record.model_copy(update={"id": "duplicate"}))
    elif failure == "identity":
        incoming = incoming.model_copy(update={"user_id": "other-person"})
    else:
        attribution = attribution.model_copy(update={"application_id": UUID(int=2)})
    before = (list(repository.usage_records), dict(repository.usage_application_attributions))
    with pytest.raises(ValueError):
        repository.write_token_usage(incoming, attribution)
    assert (repository.usage_records, repository.usage_application_attributions) == before
    assert repository.budget_evidence_received_at == {}
    assert repository.budget_evidence_conflicts == set()


@pytest.mark.parametrize("legacy", [False, True])
def test_receipt_time_is_registered_once_after_identity_validation(legacy: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(correlation_id="receipt"))
    assert record is not None
    initial = record.model_copy(update={"id": "legacy"}) if legacy else record
    received = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with patch("turnstile_core.persistence.in_memory.datetime", wraps=datetime) as clock:
        clock.now.return_value = received
        repository.write_token_usage(initial)
        clock.now.return_value = datetime(2026, 9, 12, 13, tzinfo=UTC)
        repository.write_token_usage(record)
    assert repository.budget_evidence_received_at == {"usage:" + initial.id: received}
    assert repository.usage_records == [initial]


def test_copilot_identity_and_missing_correlation_fallbacks_are_preserved() -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    cases: tuple[tuple[dict[str, object], str, str], ...] = (
        ({"request_id": "caller-request"}, "caller-request", "apim"),
        ({}, "source-event", "apim"),
        (
            {"ingest_source": "copilot_cli", "correlation_id": "shared-correlation"},
            "source-event",
            "github_copilot",
        ),
        (
            {"request_source": "copilot-assistant-tools", "correlation_id": "shared-correlation"},
            "source-event",
            "github_copilot",
        ),
    )
    for overrides, expected_id, expected_domain in cases:
        record = processor.normalize(event(id="source-event", **overrides))
        assert record is not None
        assert record.id == expected_id
        assert record.usage_domain == expected_domain


def test_exact_adapter_event_upgrades_only_the_estimated_placeholder() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(
        event(
            id="shared-request",
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            tokens_consumed=1000,
            budget_admission="ok",
            model_admission="ok",
        )
    )
    assert processor.process(
        event(
            id="shared-request",
            input_tokens=72,
            cached_tokens=1128,
            cache_write_tokens=128,
            output_tokens=30,
            estimated=False,
            ingest_source="eventhub",
        )
    )
    assert processor.process(
        event(
            id="shared-request",
            input_tokens=999,
            cached_tokens=0,
            output_tokens=999,
            estimated=False,
        )
    )

    matching = [record for record in repository.usage_records if record.id == "shared-request"]
    assert len(matching) == 1
    assert matching[0].input_tokens == 72
    assert matching[0].cached_tokens == 1128
    assert matching[0].cache_write_tokens == 128
    assert matching[0].output_tokens == 30
    assert matching[0].estimated is False
    assert matching[0].budget_admission == "ok"
    assert matching[0].model_admission == "ok"


def test_later_apim_exact_event_adds_admission_without_replacing_observer_usage() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(
        event(
            id="exact-first",
            input_tokens=72,
            cached_tokens=1128,
            cache_write_tokens=128,
            output_tokens=30,
            estimated=False,
            ingest_source="eventhub",
        )
    )
    assert processor.process(
        event(
            id="exact-first",
            input_tokens=999,
            cached_tokens=0,
            output_tokens=999,
            estimated=False,
            budget_admission="ok",
            model_admission="ok",
        )
    )

    matching = [record for record in repository.usage_records if record.id == "exact-first"]
    assert len(matching) == 1
    assert matching[0].input_tokens == 72
    assert matching[0].cached_tokens == 1128
    assert matching[0].cache_write_tokens == 128
    assert matching[0].output_tokens == 30
    assert matching[0].budget_admission == "ok"
    assert matching[0].model_admission == "ok"


def test_postgres_upsert_preserves_exact_observer_usage_when_admission_arrives() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "turnstile_core/persistence/repository.py"
    ).read_text(encoding="utf-8")

    assert "input_tokens = CASE WHEN existing.estimated" in source
    assert "THEN EXCLUDED.input_tokens ELSE existing.input_tokens END" in source
    assert "runtime = CASE WHEN existing.estimated" in source
    assert "THEN EXCLUDED.runtime ELSE existing.runtime END" in source
    assert "existing.budget_admission, EXCLUDED.budget_admission" in source
    assert "existing.model_admission, EXCLUDED.model_admission" in source


def test_malformed_event_does_not_raise_or_write() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process({"id": "broken"}) is False
    assert repository.records == []


def test_missing_usage_is_persisted_and_flagged() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert (
        processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None)) is True
    )
    record = repository.records[0]
    assert record.input_tokens == 0
    assert record.cached_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated is True
    assert record.ingest_error == "usage_unavailable"


def sonnet_prices() -> dict[str, ModelPrice]:
    return {
        "gpt-4.1-mini": ModelPrice(
            input_price_per_million=3.0,
            cached_price_per_million=0.3,
            cache_write_price_per_million=3.75,
            output_price_per_million=15.0,
        )
    }


def context_price() -> ModelPrice:
    return ModelPrice(
        input_price_per_million=0, cached_price_per_million=0,
        cache_write_price_per_million=0, output_price_per_million=0,
        context_tiers=[
            ModelPriceTier(
                key="short", min_input_tokens=0, max_input_tokens=1200,
                input_price_per_million=3, cached_price_per_million=0.3,
                cache_write_price_per_million=3.75, output_price_per_million=15,
            ),
            ModelPriceTier(
                key="long", min_input_tokens=1201,
                input_price_per_million=6, cached_price_per_million=0.6,
                cache_write_price_per_million=7.5, output_price_per_million=22.5,
            ),
        ],
    )


@pytest.mark.parametrize("input_tokens,cached_tokens,key", [
    (0, 0, "short"), (1000, 200, "short"), (1001, 200, "long"),
    (0, 1201, "long"), (1201, 0, "long"),
])
def test_context_price_selects_by_total_input_and_prices_the_whole_request(
    input_tokens: int, cached_tokens: int, key: str,
) -> None:
    price = context_price()
    selected = price.for_input_tokens(input_tokens + cached_tokens)
    assert selected.selected_tier == key
    writes = min(100, cached_tokens)
    output_tokens = 9999
    expected = (
        input_tokens * selected.input_price_per_million
        + (cached_tokens - writes) * selected.cached_price_per_million
        + writes * selected.cache_write_price_per_million
        + output_tokens * selected.output_price_per_million
    ) / 1_000_000
    assert price.cost(input_tokens, cached_tokens, output_tokens, writes) == round(expected, 8)
    assert price.selected_tier is None


@pytest.mark.parametrize("minimum,maximum", [(1200, None), (1202, None), (1201, 9000)])
def test_context_price_rejects_overlapping_missing_or_unbounded_ranges(
    minimum: int, maximum: int | None,
) -> None:
    data = context_price().model_dump()
    data["context_tiers"][1].update(min_input_tokens=minimum, max_input_tokens=maximum)
    with pytest.raises(ValueError):
        ModelPrice.model_validate(data)


@pytest.mark.parametrize("input_tokens,key,rate", [(1000, "short", 3), (1001, "long", 6)])
def test_context_price_ingestion_snapshots_selected_rates_and_complete_schedule(
    input_tokens: int, key: str, rate: float,
) -> None:
    price = context_price()
    repository = RecordingRepository({"gpt-4.1-mini": price})
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(event(input_tokens=input_tokens, cache_write_tokens=150)) is True
    record = repository.records[0]
    assert record.price_context_tier == key
    assert record.input_price_per_million == rate
    assert record.price_context_snapshot == price.model_dump(mode="json")
    assert record.estimated_cost == price.cost(input_tokens, 200, 100, 150)
    price.context_tiers[0].input_price_per_million = 999
    assert record.price_context_snapshot["context_tiers"][0]["input_price_per_million"] == 3


def test_context_price_ingestion_does_not_guess_a_tier_for_missing_input_usage() -> None:
    price = context_price()
    repository = RecordingRepository({"gpt-4.1-mini": price})
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    record = repository.records[0]
    assert record.price_context_tier is None
    assert record.input_price_per_million is None
    assert record.estimated_cost == 0
    assert record.price_context_snapshot == price.model_dump(mode="json")
    assert record.estimated is True


def test_context_price_reconciliation_uses_original_rules_after_registry_price_changes() -> None:
    from turnstile_core.domain.models import ReconciledUsage

    repository = InMemoryRepository()
    prices = context_price()
    recording = RecordingRepository({"gpt-4.1-mini": prices})
    processor = UsageProcessor(recording, CoefficientResolver({"default": 1.0}))
    processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    original = recording.records[0]
    repository.write_token_usage(original)
    prices.context_tiers[1].input_price_per_million = 999
    item = ReconciledUsage(
        correlation_id=original.correlation_id, input_tokens=1001, cached_tokens=200,
        output_tokens=100,
    )
    assert repository.apply_reconciled_usage([item]) == 1
    saved = repository.usage_records[-1]
    assert saved.price_context_tier == "long"
    assert saved.input_price_per_million == 6
    assert saved.estimated_cost == context_price().cost(1001, 200, 100)
    assert saved.price_context_snapshot == original.price_context_snapshot
    assert repository.apply_reconciled_usage([item]) == 0


def test_context_price_late_formal_usage_preserves_first_price_schedule() -> None:
    repository = InMemoryRepository()
    recording = RecordingRepository({"gpt-4.1-mini": context_price()})
    processor = UsageProcessor(recording, CoefficientResolver({"default": 1.0}))
    processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    original = recording.records[0]
    repository.write_token_usage(original)
    recording.prices["gpt-4.1-mini"].context_tiers[1].input_price_per_million = 999
    processor.process(event(input_tokens=1001, cached_tokens=200, output_tokens=100))
    repository.write_token_usage(recording.records[-1])
    saved = repository.usage_records[-1]
    assert saved.price_context_tier == "long"
    assert saved.input_price_per_million == 6
    assert saved.price_context_snapshot == original.price_context_snapshot


def test_cost_is_priced_at_ingestion_with_the_cached_rate() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    # 1000 input at 3.00, 200 cached at 0.30, 100 output at 15.00.
    assert record.estimated_cost == round((1000 * 3.0 + 200 * 0.3 + 100 * 15.0) / 1_000_000, 8)
    assert record.input_price_per_million == 3.0
    assert record.cached_price_per_million == 0.3
    assert record.output_price_per_million == 15.0


def test_cache_writes_are_priced_above_cache_reads() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    # 200 cached tokens of which 150 were written, leaving 50 reads.
    assert processor.process(event(cache_write_tokens=150)) is True

    record = repository.records[0]
    assert record.cache_write_tokens == 150
    assert record.cache_write_price_per_million == 3.75
    assert record.estimated_cost == round(
        (1000 * 3.0 + 50 * 0.3 + 150 * 3.75 + 100 * 15.0) / 1_000_000, 8
    )


def test_an_unreported_split_prices_every_cached_token_as_a_read() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    assert record.cache_write_tokens == 0
    assert record.estimated_cost == round((1000 * 3.0 + 200 * 0.3 + 100 * 15.0) / 1_000_000, 8)


def test_a_write_count_above_the_cache_bucket_is_clamped() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    # The database constrains writes to the cache bucket, so a malformed event must not exceed it.
    assert processor.process(event(cache_write_tokens=9999)) is True

    record = repository.records[0]
    assert record.cache_write_tokens == 200
    assert record.estimated_cost == round((1000 * 3.0 + 200 * 3.75 + 100 * 15.0) / 1_000_000, 8)


def test_registry_id_takes_priority_over_the_model_key() -> None:
    prices = sonnet_prices()
    prices["deployment-1"] = ModelPrice(
        input_price_per_million=1.0,
        cached_price_per_million=0.5,
        cache_write_price_per_million=1.25,
        output_price_per_million=6.0,
    )
    repository = RecordingRepository(prices)
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event(model_id="deployment-1")) is True

    assert repository.records[0].input_price_per_million == 1.0


def test_unpriced_model_records_no_cost_and_no_prices() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    assert record.estimated_cost == 0
    assert record.input_price_per_million is None
    assert record.cached_price_per_million is None
    assert record.output_price_per_million is None


def test_registry_is_read_once_per_batch_not_per_event() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    for index in range(5):
        assert processor.process(event(id=f"usage-{index}")) is True

    assert repository.price_reads == 1
    assert repository.identity_reads == 1


def test_registry_uuid_and_model_key_collapse_onto_one_identity() -> None:
    identity = ModelIdentity(model_id="307940d3", display_name="Claude Sonnet 5")
    identities = {"307940d3": identity, "databricks-claude-sonnet-5": identity}
    repository = RecordingRepository(identities=identities)
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    # The dashboard BFF sends the registry UUID; an employee desktop client sends the model key.
    assert processor.process(event(id="bff", model_id="307940d3", model="gpt-name-drift")) is True
    assert processor.process(
        event(
            id="desktop",
            model_id="databricks-claude-sonnet-5",
            model="databricks-claude-sonnet-5",
        )
    ) is True

    assert [(item.model_id, item.model) for item in repository.records] == [
        ("307940d3", "Claude Sonnet 5"),
        ("307940d3", "Claude Sonnet 5"),
    ]


def test_a_model_missing_from_the_registry_keeps_the_caller_identifiers() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event(model_id="unknown-1", model="mystery-model")) is True

    record = repository.records[0]
    assert record.model_id == "unknown-1"
    assert record.model == "mystery-model"


def test_a_failing_registry_read_records_usage_without_a_cost() -> None:
    class FailingRepository(RecordingRepository):
        def model_prices(self) -> dict[str, ModelPrice]:
            raise RuntimeError("registry unavailable")

    repository = FailingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    assert record.input_tokens == 1000
    assert record.estimated_cost == 0
    assert record.input_price_per_million is None


def test_failed_request_without_usage_is_persisted() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(
        event(
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            status="502",
            status_code=502,
            error_message="upstream unavailable",
        )
    ) is True
    record = repository.records[0]
    assert record.total_tokens if hasattr(record, "total_tokens") else 0 == 0
    assert record.status_code == 502
    assert record.ingest_error == "failed_request_has_no_usage"
