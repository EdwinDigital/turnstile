"""Scheduled reconciliation of streamed request usage.

APIM cannot expose a provider token breakdown to policy expressions while a response is still
streaming, so those requests land in PostgreSQL with zeroed counts and an explicit ingest_error.
The gateway LLM diagnostic log does record the real per-request prompt and completion counts and
is joinable on the APIM correlation ID, so a scheduled job fills the gap after the fact.

This mirrors the two-path ingestion design used by the official AI Hub Gateway accelerator:
Event Hub for non-streamed requests, an Azure Monitor query on a schedule for streamed ones.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic, time
from typing import Any, Protocol

import httpx

from ..domain.models import (
    ApimCacheReadBucket,
    ReconciledUsage,
    ReconciliationOutcome,
    ReservationTerminalEvidence,
)
from ..persistence.repository import QueryRepository

logger = logging.getLogger(__name__)

RECONCILIATION_SOURCE = "apim_gateway_llm_log"
CACHE_READ_SOURCE = "apim_cache_read_metric"

_LOG_ANALYTICS_RESOURCE = "https://api.loganalytics.io"

# One row per request. `max` collapses the duplicate rows APIM can emit for a single correlation
# ID without inventing a total, because every duplicate reports the same final usage.
#
# The gateway LLM log has no cached-token column. Custom metrics cannot safely carry a unique
# request ID as a dimension, so streamed cache usage remains unknown rather than being invented
# as zero. Prompt and completion remain exactly joinable through the gateway log CorrelationId.
_QUERY = """
ApiManagementGatewayLlmLog
| where isnotempty(CorrelationId)
| where isnotnull(PromptTokens) and isnotnull(CompletionTokens)
| summarize PromptTokens = max(PromptTokens), CompletionTokens = max(CompletionTokens)
    by CorrelationId
| project CorrelationId, PromptTokens, CompletionTokens
"""

_RESERVATION_TERMINAL_QUERY = """
let wanted = dynamic(__CORRELATION_IDS__);
let gateway = ApiManagementGatewayLogs
| where ApiId == "__API_ID__"
| where CorrelationId in (wanted)
| summarize arg_max(TimeGenerated, ResponseCode, LastErrorReason) by CorrelationId
| project CorrelationId,
          GatewayObservedAt = TimeGenerated,
          StatusCode = toint(ResponseCode),
          LastErrorReason = tostring(LastErrorReason);
let llm = ApiManagementGatewayLlmLog
| where CorrelationId in (wanted)
| where isnotnull(PromptTokens)
    and isnotnull(CompletionTokens)
    and isnotnull(TotalTokens)
| summarize LlmObservedAt = max(TimeGenerated),
            PromptTokens = max(tolong(PromptTokens)),
            CompletionTokens = max(tolong(CompletionTokens)),
            TotalTokens = max(tolong(TotalTokens))
    by CorrelationId;
gateway
| join kind=fullouter llm on CorrelationId
| project CorrelationId = coalesce(CorrelationId, CorrelationId1),
          ObservedAt = coalesce(LlmObservedAt, GatewayObservedAt),
          StatusCode, LastErrorReason,
          PromptTokens, CompletionTokens, TotalTokens
"""

def _cache_read_query() -> str:
    base = """let base = AppMetrics
| where Name == "Prompt Cached Tokens"
| where tostring(Properties["API ID"]) == api_id
| extend Scope = tostring(Properties.Scope);"""
    return (
        base
        + "\nbase\n"
        + '| where Scope == "Global" or isempty(Scope)\n'
        + "| summarize CacheReadTokens = tolong(sum(Sum)) "
        + "by BucketStart = bin(TimeGenerated, 1h)\n"
        + '| extend DimensionType = "global", DimensionValue = "all"'
        + "\n| project DimensionType, DimensionValue, BucketStart, CacheReadTokens"
        + "\n| order by BucketStart asc"
    )


_CACHE_READ_QUERY = _cache_read_query()


class GatewayUsageLog(Protocol):
    def fetch(
        self, window_start: datetime, window_end: datetime
    ) -> Sequence[ReconciledUsage]: ...


class CacheReadLog(Protocol):
    def fetch(
        self, api_id: str, window_start: datetime, window_end: datetime
    ) -> Sequence[ApimCacheReadBucket]: ...


class ReservationTerminalLog(Protocol):
    def fetch_correlations(
        self,
        correlation_ids: Sequence[str],
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[ReservationTerminalEvidence]: ...


class ManagedIdentityTokenProvider:
    """Reads a token from the App Service / Functions managed identity endpoint.

    Used instead of azure-identity so the prebuilt Function package gains no new dependency.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._token_lock = Lock()
        self._identity: tuple[str, str] | None = None
        self._tokens: dict[str, tuple[str, float]] = {}

    def token(self, resource: str) -> str:
        with self._token_lock:
            endpoint = os.environ.get("IDENTITY_ENDPOINT")
            header = os.environ.get("IDENTITY_HEADER")
            if not endpoint or not header:
                self._tokens.clear()
                raise RuntimeError("Managed identity endpoint is not available in this environment")
            identity = (endpoint, header)
            if identity != self._identity:
                self._tokens.clear()
                self._identity = identity
            cached = self._tokens.get(resource)
            if cached is not None and monotonic() < cached[1]:
                return cached[0]
            client = self._client or httpx.Client(timeout=10.0)
            try:
                response = client.get(
                    endpoint,
                    params={"resource": resource, "api-version": "2019-08-01"},
                    headers={"X-IDENTITY-HEADER": header},
                )
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
            finally:
                if self._client is None:
                    client.close()
            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise RuntimeError("Managed identity response did not contain an access token")
            expiry = payload.get("expires_on")
            if isinstance(expiry, str) and expiry.isascii() and expiry.isdecimal():
                expiry = int(expiry) if len(expiry) <= 12 else None
            if type(expiry) is int:
                lifetime = min(300.0, expiry - time() - 120.0)
                if lifetime > 0:
                    if len(self._tokens) >= 8:
                        self._tokens.clear()
                    self._tokens[resource] = (access_token, monotonic() + lifetime)
            return access_token


class LogAnalyticsGatewayUsageLog:
    def __init__(
        self,
        workspace_id: str,
        token_provider: ManagedIdentityTokenProvider | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._workspace_id = workspace_id
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client
        self._timeout = timeout

    def fetch(self, window_start: datetime, window_end: datetime) -> Sequence[ReconciledUsage]:
        token = self._token_provider.token(_LOG_ANALYTICS_RESOURCE)
        body = {
            "query": _QUERY,
            "timespan": f"{_isoformat(window_start)}/{_isoformat(window_end)}",
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(
                f"{_LOG_ANALYTICS_RESOURCE}/v1/workspaces/{self._workspace_id}/query",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        finally:
            if self._client is None:
                client.close()
        return _parse_rows(payload)


class LogAnalyticsReservationTerminalLog:
    def __init__(
        self,
        workspace_id: str,
        api_id: str,
        token_provider: ManagedIdentityTokenProvider | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_batch_size: int = 500,
    ) -> None:
        self._workspace_id = workspace_id
        self._api_id = api_id
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client
        self._timeout = timeout
        self._max_batch_size = max_batch_size

    def fetch_correlations(
        self,
        correlation_ids: Sequence[str],
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[ReservationTerminalEvidence]:
        wanted = sorted({value for value in correlation_ids if value})
        if not wanted or window_start >= window_end:
            return []
        token = self._token_provider.token(_LOG_ANALYTICS_RESOURCE)
        client = self._client or httpx.Client(timeout=self._timeout)
        items: list[ReservationTerminalEvidence] = []
        try:
            for offset in range(0, len(wanted), self._max_batch_size):
                chunk = wanted[offset : offset + self._max_batch_size]
                query = _RESERVATION_TERMINAL_QUERY.replace(
                    "__CORRELATION_IDS__", json.dumps(chunk)
                ).replace("__API_ID__", _kql_string(self._api_id))
                response = client.post(
                    f"{_LOG_ANALYTICS_RESOURCE}/v1/workspaces/"
                    f"{self._workspace_id}/query",
                    json={
                        "query": query,
                        "timespan": (
                            f"{_isoformat(window_start)}/{_isoformat(window_end)}"
                        ),
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                batch = _parse_terminal_rows(response.json())
                if any(item.correlation_id not in chunk for item in batch):
                    raise RuntimeError("Terminal evidence contains an unexpected correlation")
                items.extend(batch)
        finally:
            if self._client is None:
                client.close()
        return items


class LogAnalyticsCacheReadLog:
    def __init__(
        self,
        workspace_id: str,
        token_provider: ManagedIdentityTokenProvider | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._workspace_id = workspace_id
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client
        self._timeout = timeout

    def fetch(
        self, api_id: str, window_start: datetime, window_end: datetime
    ) -> Sequence[ApimCacheReadBucket]:
        token = self._token_provider.token(_LOG_ANALYTICS_RESOURCE)
        query = f'let api_id = "{_kql_string(api_id)}";\n{_CACHE_READ_QUERY}'
        body = {
            "query": query,
            "timespan": f"{_isoformat(window_start)}/{_isoformat(window_end)}",
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(
                f"{_LOG_ANALYTICS_RESOURCE}/v1/workspaces/{self._workspace_id}/query",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        finally:
            if self._client is None:
                client.close()
        return _parse_cache_read_rows(payload, api_id)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_cache_read_rows(
    payload: dict[str, Any], api_id: str
) -> list[ApimCacheReadBucket]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    columns = [column.get("name") for column in table.get("columns") or []]
    try:
        dimension_type_index = columns.index("DimensionType")
        dimension_value_index = columns.index("DimensionValue")
        bucket_index = columns.index("BucketStart")
        cache_index = columns.index("CacheReadTokens")
    except ValueError:
        logger.warning("Cache-read metric response is missing expected columns: %s", columns)
        return []
    return [
        ApimCacheReadBucket.model_validate(
            {
                "api_id": api_id,
                "dimension_type": row[dimension_type_index],
                "dimension_value": row[dimension_value_index],
                "bucket_start": row[bucket_index],
                "cache_read_tokens": row[cache_index],
            }
        )
        for row in table.get("rows") or []
        if row[dimension_type_index]
        and row[dimension_value_index]
        and row[bucket_index] is not None
        and row[cache_index] is not None
    ]


class CacheReadSyncService:
    def __init__(
        self,
        repository: QueryRepository,
        usage_log: CacheReadLog,
        api_id: str,
        backfill_hours: int = 24 * 30,
        overlap_hours: int = 24,
        safety_lag_minutes: int = 6,
    ) -> None:
        self._repository = repository
        self._usage_log = usage_log
        self._api_id = api_id
        self._backfill_hours = backfill_hours
        self._overlap_hours = overlap_hours
        self._safety_lag_minutes = safety_lag_minutes

    def run(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        window_end = current - timedelta(minutes=self._safety_lag_minutes)
        earliest = current - timedelta(hours=self._backfill_hours)
        watermark = self._repository.reconciliation_watermark(CACHE_READ_SOURCE)
        window_start = earliest if watermark is None else max(
            watermark - timedelta(hours=self._overlap_hours), earliest
        )
        window_start = window_start.replace(minute=0, second=0, microsecond=0)
        if window_start >= window_end:
            return 0
        items = [
            item
            for item in self._usage_log.fetch(self._api_id, window_start, window_end)
            if item.dimension_type == "global" and item.dimension_value == "all"
        ]
        refreshed = self._repository.upsert_apim_cache_read_buckets(
            self._api_id, window_start, window_end, items
        )
        self._repository.save_reconciliation_state(
            CACHE_READ_SOURCE, window_end, refreshed, len(items)
        )
        return refreshed


def _parse_rows(payload: dict[str, Any]) -> list[ReconciledUsage]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    columns = [column.get("name") for column in table.get("columns") or []]
    try:
        correlation_index = columns.index("CorrelationId")
        prompt_index = columns.index("PromptTokens")
        completion_index = columns.index("CompletionTokens")
    except ValueError:
        logger.warning("Gateway LLM log response is missing expected columns: %s", columns)
        return []
    # Some historical fixtures and old query responses carry a measured cached column. New
    # gateway-log-only reconciliation deliberately leaves it absent, which means unknown.
    cached_index = columns.index("CachedTokens") if "CachedTokens" in columns else None

    items: list[ReconciledUsage] = []
    for row in table.get("rows") or []:
        correlation_id = row[correlation_index]
        prompt_tokens = row[prompt_index]
        completion_tokens = row[completion_index]
        if not correlation_id or prompt_tokens is None or completion_tokens is None:
            continue
        raw_cached_tokens = None if cached_index is None else row[cached_index]
        cached_tokens = raw_cached_tokens
        items.append(
            ReconciledUsage(
                correlation_id=str(correlation_id),
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                cached_tokens=cached_tokens,
            )
        )
    return items


def _parse_terminal_rows(
    payload: dict[str, Any],
) -> list[ReservationTerminalEvidence]:
    if not isinstance(payload, dict) or "error" in payload or "partialError" in payload:
        raise RuntimeError("Terminal evidence query did not return a complete result")
    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
        raise RuntimeError("Terminal evidence result table is missing or ambiguous")
    table = tables[0]
    raw_columns = table.get("columns")
    if not isinstance(raw_columns, list) or any(not isinstance(item, dict) for item in raw_columns):
        raise RuntimeError("Terminal evidence columns are invalid")
    columns = [column.get("name") for column in raw_columns]
    required = (
        "CorrelationId", "ObservedAt", "StatusCode", "LastErrorReason",
        "PromptTokens", "CompletionTokens", "TotalTokens",
    )
    if (
        any(not isinstance(name, str) for name in columns)
        or len(columns) != len(set(columns))
        or any(name not in columns for name in required)
        or not isinstance(table.get("rows"), list)
    ):
        raise RuntimeError("Terminal evidence result schema is incomplete")
    indexes = {name: columns.index(name) for name in columns}

    def value(row: Sequence[Any], name: str) -> Any:
        index = indexes.get(name)
        return None if index is None else row[index]

    items: list[ReservationTerminalEvidence] = []
    correlations: set[str] = set()
    for row in table["rows"]:
        if not isinstance(row, list) or len(row) != len(columns):
            raise RuntimeError("Terminal evidence result row is incomplete")
        correlation_id = value(row, "CorrelationId")
        observed_at = value(row, "ObservedAt")
        if not isinstance(correlation_id, str) or not correlation_id.strip() or observed_at is None:
            raise RuntimeError("Terminal evidence requires a correlation and timestamp")
        if correlation_id in correlations:
            raise RuntimeError("Terminal evidence correlation is ambiguous")
        correlations.add(correlation_id)
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise RuntimeError("Terminal evidence requires an explicit timezone")
        items.append(
            ReservationTerminalEvidence.model_validate(
                {
                    "correlation_id": correlation_id,
                    "observed_at": observed_at,
                    "status_code": value(row, "StatusCode"),
                    "last_error_reason": value(row, "LastErrorReason") or None,
                    "prompt_tokens": value(row, "PromptTokens"),
                    "completion_tokens": value(row, "CompletionTokens"),
                    "total_tokens": value(row, "TotalTokens"),
                }
            )
        )
        if items[-1].observed_at.utcoffset() is None:
            raise RuntimeError("Terminal evidence requires an explicit timezone")
    return items


class ReconciliationService:
    def __init__(
        self,
        repository: QueryRepository,
        usage_log: GatewayUsageLog,
        lookback_hours: int = 24,
        safety_lag_minutes: int = 6,
    ) -> None:
        self._repository = repository
        self._usage_log = usage_log
        self._lookback_hours = lookback_hours
        self._safety_lag_minutes = safety_lag_minutes

    def run(self, now: datetime | None = None) -> ReconciliationOutcome:
        current = now or datetime.now(UTC)
        # Measured p95 ingestion lag for the gateway LLM log is ~4 minutes, so the window stops
        # short of the present. Querying newer rows would only return gaps.
        window_end = current - timedelta(minutes=self._safety_lag_minutes)
        oldest_allowed = current - timedelta(hours=self._lookback_hours)
        watermark = self._repository.reconciliation_watermark(RECONCILIATION_SOURCE)
        window_start = oldest_allowed if watermark is None else max(watermark, oldest_allowed)

        # The watermark alone is not safe to start from. It advances to `window_end` on
        # every run whether or not the rows in that window had arrived yet, so anything
        # that reached Log Analytics late was skipped and then permanently excluded,
        # because every later window began after it. Observed on 2026-08-02: a restart
        # stalled this timer for 27 minutes, the catch-up run matched 15 of 34, and the
        # other 19 were stranded with the watermark already past them.
        #
        # Starting no later than the oldest row that still needs a token count makes that
        # impossible by construction: a row cannot be stepped over while it is still the
        # reason the scan reaches back. The watermark stays as the fast path for the
        # common case where nothing is pending, and `oldest_allowed` still bounds the
        # scan so a permanently unmatchable row cannot widen the window forever.
        pending = self._repository.oldest_unreconciled_usage(oldest_allowed)
        if pending is not None:
            window_start = max(min(window_start, pending), oldest_allowed)

        if window_start >= window_end:
            return ReconciliationOutcome(
                source=RECONCILIATION_SOURCE,
                window_start=window_start,
                window_end=window_end,
                scanned=0,
                matched=0,
                watermark=window_start,
            )

        items = self._usage_log.fetch(window_start, window_end)
        matched = self._repository.apply_reconciled_usage(items)
        self._repository.save_reconciliation_state(
            RECONCILIATION_SOURCE, window_end, matched, len(items)
        )
        logger.info(
            "Reconciled %s of %s gateway usage rows between %s and %s",
            matched,
            len(items),
            _isoformat(window_start),
            _isoformat(window_end),
        )
        return ReconciliationOutcome(
            source=RECONCILIATION_SOURCE,
            window_start=window_start,
            window_end=window_end,
            scanned=len(items),
            matched=matched,
            watermark=window_end,
        )
