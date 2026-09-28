from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from turnstile_core.domain.models import (
    ApimCacheReadBucket,
    ModelIdentity,
    ReconciledUsage,
    TokenUsageRecord,
)
from turnstile_core.integrations.reconciliation import (
    _CACHE_READ_QUERY,
    _QUERY,
    CACHE_READ_SOURCE,
    RECONCILIATION_SOURCE,
    CacheReadSyncService,
    LogAnalyticsReservationTerminalLog,
    ManagedIdentityTokenProvider,
    ReconciliationService,
    _parse_cache_read_rows,
    _parse_rows,
    _parse_terminal_rows,
)
from turnstile_core.persistence.in_memory import InMemoryRepository

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def test_managed_identity_token_cache_is_concurrent_expiring_and_identity_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("turnstile_core.integrations.reconciliation.time", lambda: clock[0])
    monkeypatch.setattr("turnstile_core.integrations.reconciliation.monotonic", lambda: clock[0])
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1/identity")
    monkeypatch.setenv("IDENTITY_HEADER", "unit-header")
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "access_token": "unit-token-" + str(len(requests)),
            "expires_on": str(int(clock[0] + 240)),
        })

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = ManagedIdentityTokenProvider(client)
        with ThreadPoolExecutor(max_workers=8) as executor:
            tokens = list(executor.map(provider.token, ["https://management.azure.com/"] * 40))
        assert len(requests) == 1 and len(set(tokens)) == 1
        assert provider.token("https://storage.azure.com/") != tokens[0]
        assert len(requests) == 2
        clock[0] += 121
        assert provider.token("https://management.azure.com/") != tokens[0]
        assert len(requests) == 3
        monkeypatch.setenv("IDENTITY_HEADER", "rotated-unit-header")
        provider.token("https://management.azure.com/")
        assert len(requests) == 4
        monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1/new-identity")
        provider.token("https://management.azure.com/")
        assert len(requests) == 5
        monkeypatch.delenv("IDENTITY_HEADER")
        with pytest.raises(RuntimeError, match="not available"):
            provider.token("https://management.azure.com/")
        assert len(requests) == 5


@pytest.mark.parametrize("expiry", [None, "invalid", True, "999999999999999999999", 1001])
def test_managed_identity_token_does_not_cache_missing_invalid_or_expiring_lifetime(
    expiry: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1/identity")
    monkeypatch.setenv("IDENTITY_HEADER", "unit-header")
    monkeypatch.setattr("turnstile_core.integrations.reconciliation.time", lambda: 1000.0)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "unit-token", "expires_on": expiry})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = ManagedIdentityTokenProvider(client)
        assert provider.token("https://management.azure.com/") == "unit-token"
        assert provider.token("https://management.azure.com/") == "unit-token"
    assert len(requests) == 2


def _terminal_payload(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "tables": [
            {
                "columns": [
                    {"name": name}
                    for name in (
                        "CorrelationId",
                        "ObservedAt",
                        "StatusCode",
                        "LastErrorReason",
                        "PromptTokens",
                        "CompletionTokens",
                        "TotalTokens",
                    )
                ],
                "rows": rows,
            }
        ]
    }


def test_terminal_evidence_preserves_exact_and_partial_usage() -> None:
    items = _parse_terminal_rows(
        _terminal_payload(
            [
                ["exact", NOW.isoformat(), 200, None, 70, 30, 100],
                ["failure", NOW.isoformat(), 429, "rate limited", None, None, None],
                ["partial", NOW.isoformat(), 500, None, 70, None, None],
                ["inconsistent", NOW.isoformat(), 200, None, 70, 30, 90],
            ]
        )
    )
    assert items[0].has_exact_usage
    assert items[1].is_terminal_zero
    assert not items[2].is_terminal_zero
    assert not items[2].has_exact_usage
    assert not items[3].has_exact_usage
    assert _parse_terminal_rows(_terminal_payload([])) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tables": []},
        {"tables": [{"columns": [], "rows": []}]},
        {"error": {"code": "PartialError"}, **_terminal_payload([])},
        _terminal_payload([["short"]]),
    ],
)
def test_terminal_evidence_invalid_results_are_not_successful_empty_queries(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError):
        _parse_terminal_rows(payload)


@pytest.mark.parametrize("kind", ["tables", "columns", "correlation", "error", "partial"])
def test_terminal_evidence_rejects_ambiguous_and_partial_results(kind: str) -> None:
    payload = _terminal_payload([["same", NOW.isoformat(), 200, None, 70, 30, 100]])
    if kind == "tables":
        payload["tables"].append(payload["tables"][0])
    elif kind == "columns":
        payload["tables"][0]["columns"].append({"name": "CorrelationId"})
    elif kind == "correlation":
        payload["tables"][0]["rows"].append(list(payload["tables"][0]["rows"][0]))
    else:
        payload["error" if kind == "error" else "partialError"] = None
    with pytest.raises(RuntimeError):
        _parse_terminal_rows(payload)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_terminal_evidence_does_not_coerce_token_values(value: object) -> None:
    with pytest.raises(ValueError):
        _parse_terminal_rows(
            _terminal_payload([["exact", NOW.isoformat(), 200, None, value, 0, 1]])
        )


def test_terminal_evidence_client_batches_and_does_not_accept_partial_http_success() -> None:
    from unittest.mock import Mock

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_terminal_payload([]))

    token_provider = Mock()
    token_provider.token.return_value = "test-token"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        log = LogAnalyticsReservationTerminalLog(
            "workspace", 'api"name', token_provider, client, max_batch_size=2
        )
        assert log.fetch_correlations(["first", "second", "third", "first"], NOW, NOW) == []
        assert (
            log.fetch_correlations(
                ["first", "second", "third", "first"], NOW - timedelta(hours=1), NOW
            )
            == []
        )
    assert len(requests) == 2
    token_provider.token.assert_called_once()
    import json

    query = json.loads(requests[0].content)["query"]
    assert 'let api_id = "api\\"name";' in query
    assert 'dynamic(["first", "second"])' in query


class RecordingUsageLog:
    def __init__(self, items: Sequence[ReconciledUsage]) -> None:
        self.items = list(items)
        self.windows: list[tuple[datetime, datetime]] = []

    def fetch(self, window_start: datetime, window_end: datetime) -> Sequence[ReconciledUsage]:
        self.windows.append((window_start, window_end))
        return self.items


class RecordingCacheReadLog:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.windows: list[tuple[str, datetime, datetime]] = []

    def fetch(
        self, api_id: str, window_start: datetime, window_end: datetime
    ) -> Sequence[ApimCacheReadBucket]:
        self.windows.append((api_id, window_start, window_end))
        return _parse_cache_read_rows(self.payload, api_id)


class ModelAliasRepository(InMemoryRepository):
    def model_identities(self) -> dict[str, ModelIdentity]:
        identity = ModelIdentity(model_id="model-uuid", display_name="Model")
        return {"model-key": identity, "model-uuid": identity}


def record(
    correlation_id: str,
    *,
    estimated: bool = True,
    status_code: int = 200,
    prices: tuple[float | None, float | None, float | None] | None = None,
) -> TokenUsageRecord:
    input_price, cached_price, output_price = prices or (None, None, None)
    return TokenUsageRecord(
        id=correlation_id,
        request_id=correlation_id,
        correlation_id=correlation_id,
        ts=NOW - timedelta(minutes=30),
        team="AI Platform",
        user="lei@contoso.com",
        agent="agent-delivery",
        workflow="unattributed",
        run_id=correlation_id,
        turn_index=1,
        provider="anthropic",
        model="databricks-claude-sonnet-5",
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        et=0.0,
        et_coeff_m=1.0,
        latency_ms=1200,
        status="200",
        status_code=status_code,
        estimated_cost=0.0,
        input_price_per_million=input_price,
        cached_price_per_million=cached_price,
        output_price_per_million=output_price,
        estimated=estimated,
        ingest_source="eventhub",
        ingest_error="stream_usage_breakdown_unavailable" if estimated else None,
    )


def test_first_run_uses_lookback_and_stops_short_of_now() -> None:
    repository = InMemoryRepository()
    usage_log = RecordingUsageLog([])
    service = ReconciliationService(repository, usage_log, lookback_hours=24, safety_lag_minutes=6)

    outcome = service.run(now=NOW)

    assert usage_log.windows == [(NOW - timedelta(hours=24), NOW - timedelta(minutes=6))]
    assert outcome.watermark == NOW - timedelta(minutes=6)
    assert repository.reconciliation_watermark(RECONCILIATION_SOURCE) == outcome.watermark


def test_second_run_starts_from_the_stored_watermark() -> None:
    repository = InMemoryRepository()
    usage_log = RecordingUsageLog([])
    service = ReconciliationService(repository, usage_log, safety_lag_minutes=6)

    service.run(now=NOW)
    service.run(now=NOW + timedelta(minutes=10))

    assert usage_log.windows[1][0] == NOW - timedelta(minutes=6)
    assert usage_log.windows[1][1] == NOW + timedelta(minutes=4)


def test_only_unmeasured_successful_rows_are_filled() -> None:
    repository = InMemoryRepository()
    repository.usage_records = [
        record("stream-1"),
        record("already-measured", estimated=False),
        record("failed-request", status_code=403),
    ]
    usage_log = RecordingUsageLog(
        [
            ReconciledUsage(correlation_id="stream-1", input_tokens=1136, output_tokens=487),
            ReconciledUsage(correlation_id="already-measured", input_tokens=999, output_tokens=999),
            ReconciledUsage(correlation_id="failed-request", input_tokens=5, output_tokens=5),
        ]
    )
    service = ReconciliationService(repository, usage_log)

    outcome = service.run(now=NOW)

    filled = repository.usage_records[0]
    assert filled.input_tokens == 1136
    assert filled.output_tokens == 487
    assert filled.cached_tokens == 0
    assert filled.estimated is True
    assert filled.ingest_error == "stream_cache_usage_unavailable"
    assert repository.usage_records[1].input_tokens == 0
    assert repository.usage_records[2].input_tokens == 0
    assert outcome.matched == 1
    assert outcome.scanned == 3


def test_rerunning_the_same_window_does_not_change_filled_rows() -> None:
    repository = InMemoryRepository()
    repository.usage_records = [record("stream-1")]
    usage_log = RecordingUsageLog(
        [ReconciledUsage(correlation_id="stream-1", input_tokens=10, output_tokens=4)]
    )
    service = ReconciliationService(repository, usage_log)

    first = service.run(now=NOW)
    second = service.run(now=NOW)

    assert first.matched == 1
    assert second.matched == 0
    assert repository.usage_records[0].input_tokens == 10


def test_window_collapses_to_a_no_op_when_the_watermark_is_current() -> None:
    repository = InMemoryRepository()
    repository.save_reconciliation_state(RECONCILIATION_SOURCE, NOW, 0, 0)
    usage_log = RecordingUsageLog([])
    service = ReconciliationService(repository, usage_log, safety_lag_minutes=6)

    outcome = service.run(now=NOW)

    assert usage_log.windows == []
    assert outcome.scanned == 0
    assert outcome.matched == 0


def test_parse_rows_skips_incomplete_records() -> None:
    payload = {
        "tables": [
            {
                "columns": [
                    {"name": "CorrelationId"},
                    {"name": "PromptTokens"},
                    {"name": "CompletionTokens"},
                    {"name": "CachedTokens"},
                ],
                "rows": [
                    ["corr-1", 14, 4, 9004],
                    ["corr-2", None, 4, 0],
                    ["", 1, 1, 0],
                ],
            }
        ]
    }

    items = _parse_rows(payload)

    assert [item.correlation_id for item in items] == ["corr-1"]
    assert items[0].input_tokens == 14
    assert items[0].output_tokens == 4
    assert items[0].cached_tokens == 9004


def test_parse_rows_preserves_unknown_cached_tokens() -> None:
    payload = {
        "tables": [
            {
                "columns": [
                    {"name": "CorrelationId"},
                    {"name": "PromptTokens"},
                    {"name": "CompletionTokens"},
                    {"name": "CachedTokens"},
                ],
                "rows": [["corr-1", 14, 4, None]],
            }
        ]
    }

    assert _parse_rows(payload)[0].cached_tokens is None


def test_gateway_usage_query_has_no_per_request_custom_metric_dependency() -> None:
    assert "ApiManagementGatewayLlmLog" in _QUERY
    assert "AppMetrics" not in _QUERY
    assert "Properties.CorrelationId" not in _QUERY


def test_cache_read_query_uses_only_the_bounded_global_metric() -> None:
    assert 'Name == "Prompt Cached Tokens"' in _CACHE_READ_QUERY
    assert 'Properties["API ID"]' in _CACHE_READ_QUERY
    # Live rollback rows carry Scope=Global; corrected source omits Scope entirely.
    assert 'Scope == "Global" or isempty(Scope)' in _CACHE_READ_QUERY
    assert "CorrelationId" not in _CACHE_READ_QUERY
    assert 'DimensionType = "global", DimensionValue = "all"' in _CACHE_READ_QUERY
    for dimension in (
        "Organization",
        "Department",
        "Project",
        "Agent",
        "User",
        "Model",
        "Runtime",
    ):
        assert f'Properties["{dimension}"]' not in _CACHE_READ_QUERY


def test_cache_read_sync_upserts_hourly_metric_buckets() -> None:
    repository = InMemoryRepository()
    payload = {
        "tables": [
            {
                "columns": [
                    {"name": "DimensionType"},
                    {"name": "DimensionValue"},
                    {"name": "BucketStart"},
                    {"name": "CacheReadTokens"},
                ],
                "rows": [["global", "all", "2026-07-25T10:00:00Z", 420000]],
            }
        ]
    }
    usage_log = RecordingCacheReadLog(payload)

    refreshed = CacheReadSyncService(repository, usage_log, "turnstile-llm").run(now=NOW)

    assert refreshed == 1
    assert usage_log.windows[0][0] == "turnstile-llm"
    assert usage_log.windows[0][1] == NOW - timedelta(days=30)
    assert usage_log.windows[0][2] == NOW - timedelta(minutes=6)
    assert repository.reconciliation_watermark(CACHE_READ_SOURCE) == (NOW - timedelta(minutes=6))
    assert repository.apim_cache_read_buckets == {
        (
            "turnstile-llm",
            "global",
            "all",
            datetime(2026, 7, 25, 10, tzinfo=UTC),
        ): 420000
    }


def test_empty_cache_read_query_does_not_erase_existing_buckets() -> None:
    repository = InMemoryRepository()
    key = (
        "turnstile-llm",
        "global",
        "all",
        datetime(2026, 7, 25, 10, tzinfo=UTC),
    )
    repository.apim_cache_read_buckets[key] = 420000
    usage_log = RecordingCacheReadLog({"tables": []})

    refreshed = CacheReadSyncService(repository, usage_log, key[0]).run(now=NOW)

    assert refreshed == 0
    assert repository.apim_cache_read_buckets[key] == 420000


def test_cache_read_sync_reuses_watermark_with_overlap() -> None:
    repository = InMemoryRepository()
    repository.save_reconciliation_state(CACHE_READ_SOURCE, NOW - timedelta(hours=2), 0, 0)
    usage_log = RecordingCacheReadLog({"tables": []})

    CacheReadSyncService(repository, usage_log, "turnstile-llm").run(now=NOW)

    assert usage_log.windows[0][1] == NOW - timedelta(hours=26)
    assert usage_log.windows[0][2] == NOW - timedelta(minutes=6)


def test_cache_read_sync_ignores_legacy_dynamic_dimension_rows() -> None:
    repository = InMemoryRepository()
    payload = {
        "tables": [
            {
                "columns": [
                    {"name": "DimensionType"},
                    {"name": "DimensionValue"},
                    {"name": "BucketStart"},
                    {"name": "CacheReadTokens"},
                ],
                "rows": [
                    ["model", "model-key", "2026-07-25T10:00:00Z", 400],
                    ["user", "person@example.com", "2026-07-25T10:00:00Z", 20],
                ],
            }
        ]
    }

    refreshed = CacheReadSyncService(
        repository, RecordingCacheReadLog(payload), "turnstile-llm"
    ).run(now=NOW)

    assert refreshed == 0
    assert repository.apim_cache_read_buckets == {}


def test_repository_never_reads_legacy_dynamic_cache_rows() -> None:
    repository = InMemoryRepository()
    bucket = datetime(2026, 7, 25, 10, tzinfo=UTC)
    repository.apim_cache_read_buckets[("turnstile-llm", "user", "person@example.com", bucket)] = (
        420
    )

    assert (
        repository.apim_cache_read_totals(
            bucket, bucket + timedelta(hours=1), "user", ("person@example.com",)
        )
        == {}
    )


def test_cache_read_sync_keeps_global_hours_separate() -> None:
    repository = InMemoryRepository()
    payload = {
        "tables": [
            {
                "columns": [
                    {"name": "DimensionType"},
                    {"name": "DimensionValue"},
                    {"name": "BucketStart"},
                    {"name": "CacheReadTokens"},
                ],
                "rows": [
                    ["global", "all", "2026-07-25T10:00:00Z", 400],
                    ["global", "all", "2026-07-25T11:00:00Z", 20],
                ],
            }
        ]
    }

    refreshed = CacheReadSyncService(
        repository, RecordingCacheReadLog(payload), "turnstile-llm"
    ).run(now=NOW)

    assert refreshed == 2
    assert (
        repository.apim_cache_read_buckets[
            (
                "turnstile-llm",
                "global",
                "all",
                datetime(2026, 7, 25, 10, tzinfo=UTC),
            )
        ]
        == 400
    )
    assert (
        repository.apim_cache_read_buckets[
            (
                "turnstile-llm",
                "global",
                "all",
                datetime(2026, 7, 25, 11, tzinfo=UTC),
            )
        ]
        == 20
    )


def test_cached_tokens_are_filled_from_the_token_metric() -> None:
    repository = InMemoryRepository()
    repository.usage_records = [record("stream-1")]
    usage_log = RecordingUsageLog(
        [
            ReconciledUsage(
                correlation_id="stream-1",
                input_tokens=9,
                output_tokens=5,
                cached_tokens=9004,
            )
        ]
    )
    service = ReconciliationService(repository, usage_log)

    service.run(now=NOW)

    filled = repository.usage_records[0]
    assert filled.input_tokens == 9
    assert filled.cached_tokens == 9004
    assert filled.output_tokens == 5
    assert filled.estimated is False


def test_parse_rows_returns_nothing_when_columns_are_missing() -> None:
    assert _parse_rows({"tables": [{"columns": [{"name": "Other"}], "rows": [["x"]]}]}) == []
    assert _parse_rows({}) == []


def test_cost_is_repriced_from_the_prices_stored_at_ingestion() -> None:
    repository = InMemoryRepository()
    repository.usage_records = [record("stream-1", prices=(3.0, 0.3, 15.0))]
    usage_log = RecordingUsageLog(
        [
            ReconciledUsage(
                correlation_id="stream-1",
                input_tokens=9,
                output_tokens=5,
                cached_tokens=9004,
            )
        ]
    )

    ReconciliationService(repository, usage_log).run(now=NOW)

    filled = repository.usage_records[0]
    assert filled.estimated_cost == round((9 * 3.0 + 9004 * 0.3 + 5 * 15.0) / 1_000_000, 8)
    # The snapshot is what was applied, so a later registry price edit cannot move this amount.
    assert filled.input_price_per_million == 3.0


def test_an_unpriced_row_keeps_its_zero_cost_after_reconciliation() -> None:
    repository = InMemoryRepository()
    repository.usage_records = [record("stream-1")]
    usage_log = RecordingUsageLog(
        [ReconciledUsage(correlation_id="stream-1", input_tokens=9, output_tokens=5)]
    )

    ReconciliationService(repository, usage_log).run(now=NOW)

    filled = repository.usage_records[0]
    assert filled.input_tokens == 9
    assert filled.estimated_cost == 0.0


def test_a_row_that_arrived_late_is_not_stepped_over_permanently() -> None:
    """The failure observed in production on 2026-08-02.

    A stalled timer let the catch-up window pass over rows whose gateway logs had not
    landed yet. The watermark advanced anyway, so every later window began after them and
    they could never be reconciled again. Starting no later than the oldest row still
    awaiting a token count makes that impossible.
    """
    repository = InMemoryRepository()
    stranded = record("late-1")
    repository.write_token_usage(stranded)

    # A run whose window covers the row but whose log query comes back empty, exactly as
    # it does while Log Analytics is still ingesting.
    empty_log = RecordingUsageLog([])
    ReconciliationService(repository, empty_log, safety_lag_minutes=6).run(now=NOW)
    watermark = repository.reconciliation_watermark(RECONCILIATION_SOURCE)
    assert watermark is not None
    assert watermark > stranded.ts

    # The log has now landed. The next run must still reach back far enough to see it.
    arrived = ReconciledUsage(
        correlation_id="late-1", input_tokens=100, output_tokens=20, cached_tokens=5
    )
    later_log = RecordingUsageLog([arrived])
    outcome = ReconciliationService(repository, later_log, safety_lag_minutes=6).run(
        now=NOW + timedelta(minutes=10)
    )

    assert later_log.windows[0][0] <= stranded.ts
    assert outcome.matched == 1
    filled = next(r for r in repository.usage_records if r.correlation_id == "late-1")
    assert filled.input_tokens == 100
    assert filled.estimated is False


def test_a_quiet_period_still_starts_from_the_watermark() -> None:
    # With nothing pending the watermark remains the fast path: no reason to rescan hours
    # of gateway logs that were already matched.
    repository = InMemoryRepository()
    usage_log = RecordingUsageLog([])
    service = ReconciliationService(repository, usage_log, safety_lag_minutes=6)

    service.run(now=NOW)
    service.run(now=NOW + timedelta(minutes=10))

    assert usage_log.windows[1][0] == NOW - timedelta(minutes=6)
