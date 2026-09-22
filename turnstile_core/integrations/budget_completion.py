# ruff: noqa: UP017
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..domain.budget_completion import (
    completion_transaction,
    normalized_identity,
    validate_completion,
)
from .table_batch import (
    TableBatchConflict,
    TableBatchUnavailable,
    conditional_batch,
    entity_url,
    exact_etag,
    validate_batch_response,
)

UTC = timezone.utc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Any, response: Any, code: int, message: str, headers: Any, target: str
    ) -> None:
        return None


def completion_evidence(event: dict[str, Any]) -> dict[str, Any]:
    if (
        event.get("usage_complete") is not True
        or event.get("estimated") is not False
        or event.get("ingest_error") is not None
        or event.get("ingest_source") != "eventhub"
    ):
        raise ValueError("Completion requires a complete trusted usage event")
    return validate_completion(
        {
            "source": event["usage_event_source"],
            "observed_at": datetime.now(UTC).isoformat(),
            "input_tokens": event["input_tokens"],
            "cached_tokens": event["cached_tokens"],
            "cache_write_tokens": event["cache_write_tokens"],
            "output_tokens": event["output_tokens"],
            "total_tokens": event["tokens_consumed"],
        }
    )


class BudgetCompletionClient:
    def __init__(self, endpoint: str, table: str, token: Callable[[], str]) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", table)
        ):
            raise ValueError("Completion storage requires an explicit HTTPS Table endpoint")
        self._endpoint, self._table, self._token = endpoint.rstrip("/"), table, token
        self._opener = build_opener(_NoRedirect())

    def _request(
        self,
        method: str,
        address: str,
        *,
        content_type: str | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "Authorization": "Bearer " + self._token(),
            "x-ms-version": "2020-12-06",
            "Accept": "application/json;odata=nometadata",
            "DataServiceVersion": "3.0;",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = Request(address, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=2) as response:
                payload = response.read(131073)
                if len(payload) > 131072:
                    raise TableBatchUnavailable(
                        "Completion read exceeded its bounded response size"
                    )
                return (
                    int(response.status),
                    {key.lower(): value for key, value in response.headers.items()},
                    payload,
                )
        except HTTPError as error:
            error.close()
            raise TableBatchUnavailable(f"Completion storage returned HTTP {error.code}") from None

    def _read(self, partition: str, row: str) -> tuple[dict[str, Any], str]:
        status, headers, payload = self._request(
            "GET",
            entity_url(
                self._endpoint,
                self._table,
                partition,
                row,
            ),
        )
        if status != 200:
            raise TableBatchUnavailable("Completion storage did not return the required entity")
        entity = json.loads(payload)
        etag = exact_etag(headers.get("etag") or entity.get("odata.etag"))
        document = json.loads(entity["Document"])
        if not isinstance(document, dict):
            raise TableBatchUnavailable("Completion storage returned a malformed document")
        return document, etag

    def complete_event(self, event: dict[str, Any]) -> int:
        evidence = completion_evidence(event)
        receipts = event["budget_execution"]
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= 2:
            raise ValueError("Completion requires bounded admission receipts")
        completed, seen = 0, set()
        for receipt in receipts:
            identity = normalized_identity(receipt["identity"])
            scope = identity["scope"]
            if (
                any(
                    identity[field] != event.get(field)
                    for field in (
                        "gateway_profile_id",
                        "native_gateway_id",
                        "correlation_id",
                        "model_id",
                    )
                )
                or scope["scope_type"] in seen
            ):
                raise ValueError("Completion receipt does not match the trusted response")
            if scope["scope_type"] == "person" and scope["scope_id"] != event.get("user_id"):
                raise ValueError("Completion person does not match verified attribution")
            seen.add(scope["scope_type"])
            scope_hash = hashlib.sha256(scope["scope_id"].encode()).hexdigest()
            partition = f"budget2|{scope['scope_type']}|{scope_hash}|{scope['period_start'][:7]}"
            parts = [
                identity[field]
                for field in (
                    "gateway_profile_id",
                    "native_gateway_id",
                    "correlation_id",
                )
            ]
            key = hashlib.sha256(
                json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
            for retry in range(3):
                summary, summary_etag = self._read(partition, "S")
                attempt, attempt_etag = self._read(partition, "A|" + key)
                updated, settled = completion_transaction(
                    summary, attempt, receipt, evidence, datetime.now(UTC)
                )
                if updated is summary:
                    completed += 1
                    break
                entities = [
                    ("S", {"Document": json.dumps(updated, separators=(",", ":"))}, summary_etag),
                    (
                        "A|" + key,
                        {"Document": json.dumps(settled, separators=(",", ":"))},
                        attempt_etag,
                    ),
                ]
                content_type, body = conditional_batch(
                    self._endpoint, self._table, partition, entities
                )
                try:
                    status, headers, content = self._request(
                        "POST", self._endpoint + "/$batch", content_type=content_type, body=body
                    )
                    validate_batch_response(status, headers.get("content-type", ""), content, 2)
                except (TableBatchConflict, OSError):
                    if retry == 2:
                        raise TableBatchUnavailable(
                            "Completion exhausted its bounded retries"
                        ) from None
                    continue
                completed += 1
                break
        return completed
