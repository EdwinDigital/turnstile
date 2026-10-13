from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.parse import quote
from uuid import uuid4

MAX_BATCH_BYTES = 4 * 1024 * 1024


class TableBatchUnavailable(RuntimeError):
    pass


class TableBatchConflict(TableBatchUnavailable):
    pass


def exact_etag(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value == "*"
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("Ledger updates require an exact ETag")
    return value


def entity_url(endpoint: str, table: str, partition: str, row_key: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/{table}(PartitionKey='{quote(partition, safe='')}',"
        f"RowKey='{quote(row_key, safe='')}')"
    )


def conditional_batch(
    endpoint: str,
    table: str,
    partition: str,
    entities: Sequence[tuple[str, Mapping[str, Any], str | None]],
) -> tuple[str, bytes]:
    if not entities or len(entities) > 100:
        raise ValueError("A ledger transaction must contain 1 to 100 operations")
    if len({row for row, _, _ in entities}) != len(entities):
        raise ValueError("An entity can occur only once in a ledger transaction")
    boundary, changeset = "batch_" + uuid4().hex, "changeset_" + uuid4().hex
    lines = [f"--{boundary}", f"Content-Type: multipart/mixed; boundary={changeset}", ""]
    for index, (row_key, values, etag) in enumerate(entities, 1):
        if etag is not None:
            exact_etag(etag)
        if (
            values.get("PartitionKey", partition) != partition
            or values.get("RowKey", row_key) != row_key
        ):
            raise ValueError("All transaction entities must use the bound partition and row key")
        address = (
            entity_url(endpoint, table, partition, row_key)
            if etag is not None
            else f"{endpoint}/{table}"
        )
        document = json.dumps(
            {**values, "PartitionKey": partition, "RowKey": row_key}, separators=(",", ":")
        )
        lines.extend(
            [
                f"--{changeset}",
                "Content-Type: application/http",
                "Content-Transfer-Encoding: binary",
                f"Content-ID: {index}",
                "",
                f"{'PUT' if etag is not None else 'POST'} {address} HTTP/1.1",
                "Content-Type: application/json",
                "Accept: application/json;odata=nometadata",
                "Prefer: return-no-content",
                "DataServiceVersion: 3.0;",
            ]
        )
        if etag is not None:
            lines.append("If-Match: " + etag)
        lines.extend(["", document])
    lines.extend([f"--{changeset}--", f"--{boundary}--", ""])
    payload = "\r\n".join(lines).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError("Ledger transaction exceeds the 4 MiB limit")
    return f"multipart/mixed; boundary={boundary}", payload


def validate_batch_response(status: int, content_type: str, content: bytes, expected: int) -> None:
    if status != 202 or not content_type.lower().startswith("multipart/mixed"):
        raise TableBatchUnavailable("Ledger batch did not return a multipart transaction result")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + content,
    )
    outcomes = []
    for part in message.walk():
        if part.get_content_type() != "application/http":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise TableBatchUnavailable("Ledger batch returned an invalid operation result")
        first = payload.lstrip().split(b"\r\n", 1)[0].split()
        if len(first) < 2 or first[0] != b"HTTP/1.1" or not first[1].isdigit():
            raise TableBatchUnavailable("Ledger batch returned an invalid HTTP status")
        outcomes.append(int(first[1]))
    if any(code in {409, 412} for code in outcomes):
        raise TableBatchConflict("Ledger changed during the conditional transaction")
    if len(outcomes) != expected or any(code != 204 for code in outcomes):
        raise TableBatchUnavailable("Ledger batch did not confirm every operation")
