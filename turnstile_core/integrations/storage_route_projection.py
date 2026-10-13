from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid5

import httpx

from ..domain.storage_routes import (
    MAX_HEAD_BYTES,
    MAX_MEMBER_BYTES,
    MAX_ROUTE_BYTES,
    ActiveRouteHead,
    FixedProgramInstallation,
    FixedProgramUpgrade,
    ModelRouteAddition,
    ModelRouteHead,
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteApprovalRenewal,
    RouteCandidateProbe,
    RouteContent,
    RouteMember,
    RouteModel,
    RoutePendingActivation,
    RouteScope,
    RouteSnapshot,
    RouteTransitionReceipt,
    RouteWriterReceipt,
    RouteWriterState,
    canonical_document,
    catalog_entry,
    catalog_partition,
    content_partition,
    document_digest,
    member_partition,
    member_row,
    parse_document,
    text_digest,
    unique_object,
)
from ..domain.storage_routes import (
    require_route_storage as require_route_storage,
)
from .ledger import StorageTokenProvider, TableStorageLedger
from .table_batch import (
    TableBatchConflict,
    TableBatchUnavailable,
    conditional_batch,
    entity_url,
    exact_etag,
    validate_batch_response,
)

TABLE_METADATA = frozenset(
    {"Timestamp", "odata.etag", "odata.metadata", "odata.id", "odata.editLink"}
)


class RouteProjectionConflict(ValueError):
    pass


class RouteProjectionUnknown(RuntimeError):
    pass


def require_route_batch(
    endpoint: str, table: str, scope: RouteScope, content_type: str, body: bytes,
    expected: Sequence[tuple[str, RouteModel, str | None]],
) -> None:
    require_route_storage(endpoint, table)
    if (
        type(body) is not bytes or len(body) > 65536
        or not isinstance(content_type, str)
        or not re.fullmatch(r"multipart/mixed; boundary=batch_[0-9a-f]{32}", content_type)
    ):
        raise ValueError("ROUTE_ACTIVATION_BATCH_INVALID")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
    )
    children = list(message.iter_parts())
    if (
        message.defects or message.preamble or message.epilogue
        or len(children) != 1 or children[0].get_content_type() != "multipart/mixed"
        or any(part.defects for part in message.walk())
    ):
        raise ValueError("ROUTE_ACTIVATION_BATCH_INVALID")
    changeset = children[0]
    operations = list(changeset.iter_parts())
    if len(operations) != len(expected) or changeset.preamble or changeset.epilogue:
        raise ValueError("ROUTE_ACTIVATION_BATCH_INVALID")
    for index, (part, (row, document, etag)) in enumerate(
        zip(operations, expected, strict=True), 1,
    ):
        if dict(part.items()) != {
            "Content-Type": "application/http", "Content-Transfer-Encoding": "binary",
            "Content-ID": str(index),
        } or len(part.items()) != 3:
            raise ValueError("ROUTE_ACTIVATION_OPERATION_INVALID")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise ValueError("ROUTE_ACTIVATION_OPERATION_INVALID")
        request_line, separator, rest = payload.partition(b"\r\n")
        address = (
            entity_url(endpoint, table, scope.partition, row)
            if etag is not None else endpoint + "/" + table
        )
        if not separator or request_line != (
            f"{'PUT' if etag is not None else 'POST'} {address} HTTP/1.1".encode()
        ):
            raise ValueError("ROUTE_ACTIVATION_TARGET_CHANGED")
        request = BytesParser(policy=policy.default).parsebytes(rest)
        headers = {
            "Content-Type": "application/json", "Accept": "application/json;odata=nometadata",
            "Prefer": "return-no-content", "DataServiceVersion": "3.0;",
        }
        if etag is not None:
            headers["If-Match"] = exact_etag(etag)
        encoded = request.get_payload(decode=True)
        if (request.defects or dict(request.items()) != headers
                or len(request.items()) != len(headers) or not isinstance(encoded, bytes)):
            raise ValueError("ROUTE_ACTIVATION_CONDITION_CHANGED")
        values = json.loads(encoded, object_pairs_hook=unique_object)
        if canonical_document(values) != canonical_document(route_entity(
            scope, scope.partition, row, canonical_document(document),
        )):
            raise ValueError("ROUTE_ACTIVATION_ENTITY_CHANGED")


@dataclass(frozen=True)
class RouteActivationIntent:
    receipt: RouteTransitionReceipt
    head: ActiveRouteHead
    content_type: str
    body: bytes
    body_sha256: str
    endpoint: str
    table: str
    writer_before: RouteWriterState | None = None
    writer_after: RouteWriterState | None = None
    writer_etag: str | None = None

    def _require_valid_body(self) -> None:
        expected: list[tuple[str, RouteModel, str | None]] = [
            ("active", self.head, self.receipt.before_etag),
            ("transition:" + self.receipt.operation_id, self.receipt, None),
        ]
        if self.writer_before is not None or self.writer_after is not None:
            if (
                self.writer_before is None or self.writer_after is None or self.writer_etag is None
            ):
                raise ValueError("ROUTE_ACTIVATION_WRITER_INCOMPLETE")
            before = parse_document(
                RouteWriterState, canonical_document(self.writer_before), MAX_HEAD_BYTES,
            )
            after = parse_document(
                RouteWriterState, canonical_document(self.writer_after), MAX_HEAD_BYTES,
            )
            before.require_owner(
                before.owner_token, before.publication_id, self.head.activated_epoch,
            )
            if (
                before.scope != self.receipt.scope or before.pending is None
                or before.pending.operation_id != self.receipt.operation_id
                or before.pending.approval != self.head.approval
                or before.active_generation != self.receipt.before_generation
                or after != RouteWriterState.model_validate({
                    **before.model_dump(mode="python"), "pending": None,
                    "active_generation": self.head.approval.generation,
                    "active_version": self.head.approval.route_version,
                    "active_sha256": document_digest(self.head),
                })
            ):
                raise ValueError("ROUTE_ACTIVATION_WRITER_CHANGED")
            expected.insert(0, ("writer", after, exact_etag(self.writer_etag)))
        elif self.writer_etag is not None:
            raise ValueError("ROUTE_ACTIVATION_WRITER_INCOMPLETE")
        require_route_batch(
            self.endpoint, self.table, self.receipt.scope, self.content_type, self.body,
            expected,
        )

    def to_record(self) -> dict[str, Any]:
        receipt = parse_document(
            RouteTransitionReceipt, canonical_document(self.receipt), MAX_HEAD_BYTES,
        )
        head = parse_document(ActiveRouteHead, canonical_document(self.head), MAX_HEAD_BYTES)
        if (
            type(self.body) is not bytes or len(self.body) > 65536
            or not isinstance(self.content_type, str)
            or not re.fullmatch(r"multipart/mixed; boundary=batch_[0-9a-f]{32}", self.content_type)
            or self.body_sha256 != text_digest(self.body.decode("utf-8"))
            or receipt.scope != head.approval.scope
            or receipt.route_version != head.approval.route_version
            or receipt.approval_id != head.approval.approval_id
            or receipt.after_sha256 != document_digest(head)
            or receipt.before_generation + 1 != head.approval.generation
            or (receipt.before_etag is None) != (receipt.before_generation == 0)
        ):
            raise ValueError("ROUTE_ACTIVATION_INTENT_INVALID")
        if receipt.before_etag is not None:
            exact_etag(receipt.before_etag)
        self._require_valid_body()
        return {
            "version": 1,
            "endpoint": self.endpoint,
            "table": self.table,
            "receipt": receipt.model_dump(mode="json"),
            "head": head.model_dump(mode="json"),
            "content_type": self.content_type,
            "body_base64": base64.b64encode(self.body).decode("ascii"),
            "body_sha256": self.body_sha256,
            "writer_before": (
                self.writer_before.model_dump(mode="json")
                if self.writer_before is not None else None
            ),
            "writer_after": (
                self.writer_after.model_dump(mode="json") if self.writer_after is not None else None
            ),
            "writer_etag": self.writer_etag,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> RouteActivationIntent:
        if (
            set(record) != {
                "version", "receipt", "head", "content_type", "body_base64", "body_sha256",
                "endpoint", "table", "writer_before", "writer_after", "writer_etag",
            }
            or type(record["version"]) is not int or record["version"] != 1
            or not isinstance(record["body_base64"], str) or len(record["body_base64"]) > 87384
        ):
            raise ValueError("ROUTE_ACTIVATION_RECORD_INVALID")
        try:
            body = base64.b64decode(record["body_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("ROUTE_ACTIVATION_BODY_INVALID") from None
        restored = cls(
            parse_document(
                RouteTransitionReceipt, canonical_document(record["receipt"]), MAX_HEAD_BYTES,
            ),
            parse_document(ActiveRouteHead, canonical_document(record["head"]), MAX_HEAD_BYTES),
            record["content_type"], body, record["body_sha256"],
            record["endpoint"], record["table"],
            parse_document(
                RouteWriterState, canonical_document(record["writer_before"]), MAX_HEAD_BYTES,
            ) if record["writer_before"] is not None else None,
            parse_document(
                RouteWriterState, canonical_document(record["writer_after"]), MAX_HEAD_BYTES,
            ) if record["writer_after"] is not None else None,
            record["writer_etag"],
        )
        if restored.to_record() != dict(record):
            raise ValueError("ROUTE_ACTIVATION_RECORD_NONCANONICAL")
        return restored


@dataclass(frozen=True)
class ModelRouteActivationIntent:
    receipt: RouteTransitionReceipt
    head: ModelRouteHead
    content_type: str
    body: bytes
    endpoint: str
    table: str

    def require_valid(self) -> None:
        head = parse_document(ModelRouteHead, canonical_document(self.head), MAX_HEAD_BYTES)
        receipt = parse_document(
            RouteTransitionReceipt, canonical_document(self.receipt), MAX_HEAD_BYTES,
        )
        if (receipt.scope != head.head.approval.scope or receipt.before_etag is not None
                or receipt.before_generation != 0 or receipt.after_sha256 != document_digest(head)
                or receipt.route_version != head.head.approval.route_version
                or receipt.approval_id != head.head.approval.approval_id):
            raise ValueError("MODEL_ACTIVATION_INTENT_CHANGED")
        require_route_batch(
            self.endpoint, self.table, receipt.scope, self.content_type, self.body,
            [(head.row_key, head, None),
             ("model-transition:" + receipt.operation_id, receipt, None)],
        )

    def to_record(self) -> dict[str, Any]:
        self.require_valid()
        return {
            "version": 1, "receipt": self.receipt.model_dump(mode="json"),
            "head": self.head.model_dump(mode="json"), "content_type": self.content_type,
            "body_base64": base64.b64encode(self.body).decode("ascii"),
            "body_sha256": text_digest(self.body.decode()),
            "endpoint": self.endpoint, "table": self.table,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ModelRouteActivationIntent:
        if (set(record) != {"version", "receipt", "head", "content_type", "body_base64",
                            "body_sha256", "endpoint", "table"}
                or type(record["version"]) is not int or record["version"] != 1
                or not isinstance(record["body_base64"], str)
                or len(record["body_base64"]) > 87384):
            raise ValueError("MODEL_ACTIVATION_RECORD_INVALID")
        try:
            body = base64.b64decode(record["body_base64"], validate=True)
            digest = text_digest(body.decode())
        except (binascii.Error, ValueError):
            raise ValueError("MODEL_ACTIVATION_BODY_CHANGED") from None
        if digest != record["body_sha256"]:
            raise ValueError("MODEL_ACTIVATION_BODY_CHANGED")
        intent = cls(
            RouteTransitionReceipt.model_validate_json(canonical_document(record["receipt"])),
            ModelRouteHead.model_validate_json(canonical_document(record["head"])),
            record["content_type"], body, record["endpoint"], record["table"],
        )
        intent.require_valid()
        if intent.to_record() != dict(record):
            raise ValueError("MODEL_ACTIVATION_RECORD_NONCANONICAL")
        return intent


class ModelRouteActivationJournal(Protocol):
    def begin_once(self, intent: ModelRouteActivationIntent) -> None: ...

    def record_outcome(self, operation_id: str, outcome: str) -> None: ...


@dataclass(frozen=True)
class RouteWriterIntent:
    receipt: RouteWriterReceipt
    before: RouteWriterState | None
    after: RouteWriterState
    content_type: str
    body: bytes
    body_sha256: str
    endpoint: str
    table: str

    def to_record(self) -> dict[str, Any]:
        receipt = parse_document(
            RouteWriterReceipt, canonical_document(self.receipt), MAX_HEAD_BYTES,
        )
        after = parse_document(RouteWriterState, canonical_document(self.after), MAX_HEAD_BYTES)
        before = (
            parse_document(RouteWriterState, canonical_document(self.before), MAX_HEAD_BYTES)
            if self.before is not None else None
        )
        if (
            receipt.scope != after.scope or receipt.owner_token != after.owner_token
            or receipt.writer_epoch != after.writer_epoch
            or receipt.after_sha256 != document_digest(after)
            or receipt.before_sha256 != (document_digest(before) if before is not None else None)
            or (receipt.before_etag is None) != (before is None)
            or type(self.body) is not bytes
            or self.body_sha256 != text_digest(self.body.decode("utf-8"))
        ):
            raise ValueError("ROUTE_WRITER_INTENT_INVALID")
        if before is None:
            if receipt.kind != "claim" or after.writer_epoch != 1 or after.pending is not None:
                raise ValueError("ROUTE_WRITER_TRANSITION_INVALID")
        else:
            if receipt.kind == "takeover":
                expected = before.takeover(
                    after.owner_token, after.publication_id, after.leased_epoch,
                )
            elif receipt.kind == "renew":
                expected = before.renew(after.owner_token, after.publication_id, after.leased_epoch)
            elif receipt.kind == "prepare" and after.pending is not None:
                expected = before.prepare(
                    after.pending, after.owner_token, after.publication_id, after.leased_epoch,
                )
            elif receipt.kind == "abort" and before.pending is not None:
                before.require_owner(after.owner_token, after.publication_id, after.leased_epoch)
                expected = RouteWriterState.model_validate({
                    **before.model_dump(mode="python"), "pending": None,
                })
            else:
                raise ValueError("ROUTE_WRITER_TRANSITION_INVALID")
            if expected != after:
                raise ValueError("ROUTE_WRITER_TRANSITION_INVALID")
        require_route_batch(
            self.endpoint, self.table, receipt.scope, self.content_type, self.body,
            [("writer", after, receipt.before_etag),
             ("writer-transition:" + receipt.operation_id, receipt, None)],
        )
        return {
            "version": 1, "receipt": receipt.model_dump(mode="json"),
            "before": before.model_dump(mode="json") if before is not None else None,
            "after": after.model_dump(mode="json"), "content_type": self.content_type,
            "body_base64": base64.b64encode(self.body).decode("ascii"),
            "body_sha256": self.body_sha256, "endpoint": self.endpoint, "table": self.table,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> RouteWriterIntent:
        if (
            set(record) != {"version", "receipt", "before", "after", "content_type",
                            "body_base64", "body_sha256", "endpoint", "table"}
            or type(record["version"]) is not int or record["version"] != 1
            or not isinstance(record["body_base64"], str) or len(record["body_base64"]) > 87384
        ):
            raise ValueError("ROUTE_WRITER_RECORD_INVALID")
        try:
            body = base64.b64decode(record["body_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("ROUTE_WRITER_RECORD_INVALID") from None
        restored = cls(
            parse_document(
                RouteWriterReceipt, canonical_document(record["receipt"]), MAX_HEAD_BYTES,
            ),
            parse_document(RouteWriterState, canonical_document(record["before"]), MAX_HEAD_BYTES)
            if record["before"] is not None else None,
            parse_document(RouteWriterState, canonical_document(record["after"]), MAX_HEAD_BYTES),
            record["content_type"], body, record["body_sha256"],
            record["endpoint"], record["table"],
        )
        if restored.to_record() != dict(record):
            raise ValueError("ROUTE_WRITER_RECORD_NONCANONICAL")
        return restored


class RouteWriterJournal(Protocol):
    def begin_once(self, intent: RouteWriterIntent) -> None: ...

    def record_outcome(self, operation_id: str, outcome: str) -> None: ...


class RouteActivationJournal(Protocol):
    def begin_once(self, intent: RouteActivationIntent) -> None: ...

    def record_outcome(self, operation_id: str, outcome: str) -> None: ...


def route_entity(scope: RouteScope, partition: str, row: str, document: str) -> dict[str, Any]:
    if partition != scope.partition and not partition.startswith(scope.partition + ":"):
        raise ValueError("ROUTE_ENTITY_SCOPE_MISMATCH")
    return {
        "PartitionKey": partition,
        "RowKey": row,
        "SchemaVersion": 2,
        "Document": document,
        "Sha256": text_digest(document),
    }


def entity_document(
    entity: Mapping[str, Any],
    scope: RouteScope,
    partition: str,
    row: str,
    maximum: int,
) -> str:
    document = entity.get("Document")
    if (
        not isinstance(document, str)
        or len(document.encode()) > maximum
        or len(document.encode("utf-16-le")) > 32768
        or len(canonical_document(entity).encode()) > maximum + 2048
        or {name: value for name, value in entity.items() if name not in TABLE_METADATA}
        != route_entity(scope, partition, row, document)
        or type(entity.get("SchemaVersion")) is not int
    ):
        raise RouteProjectionConflict("ROUTE_ENTITY_INVALID")
    return document


class TableRouteProjection(TableStorageLedger):
    def __init__(
        self,
        endpoint: str,
        table: str,
        scope: RouteScope,
        token_provider: StorageTokenProvider | None = None,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        require_route_storage(endpoint, table)
        self.scope = scope
        super().__init__(endpoint, table, token_provider, client, timeout)

    def _entity_url(self, partition: str, row_key: str) -> str:
        if partition != self.scope.partition and not partition.startswith(
            self.scope.partition + ":"
        ):
            raise ValueError("ROUTE_ENTITY_SCOPE_MISMATCH")
        return super()._entity_url(partition, row_key)

    def _request(
        self,
        method: str,
        url: str,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        address = urlsplit(url)
        if (
            address.scheme != "https"
            or address.netloc != urlsplit(self._endpoint).netloc
            or address.query
            or address.fragment
            or method not in {"GET", "POST", "DELETE"}
            or not (
                (method == "POST" and address.path in {"/$batch", "/" + self._table})
                or (
                    method == "GET"
                    and address.path.startswith("/" + self._table + "(PartitionKey='")
                )
                or (
                    method == "DELETE"
                    and unquote(address.path).startswith(
                        "/" + self._table + "(PartitionKey='" + self.scope.partition
                        + "',RowKey='model:",
                    )
                    and extra_headers is not None and "If-Match" in extra_headers
                )
            )
        ):
            raise ValueError("ROUTE_STORAGE_ORIGIN_CHANGED")
        response = self._client.request(
            method,
            url,
            headers={**self._headers(), **(extra_headers or {})},
            follow_redirects=False,
            **kwargs,
        )
        if response.status_code == 401:
            self._access_token = None
        if response.is_redirect:
            raise RouteProjectionUnknown("ROUTE_STORAGE_REDIRECT_REFUSED")
        return response

    def _read_document(self, partition: str, row: str, maximum: int) -> tuple[str, str] | None:
        observed = self.read_versioned_entity(partition, row)
        if observed is None:
            return None
        entity, etag = observed
        return entity_document(entity, self.scope, partition, row, maximum), exact_etag(etag)

    def _stage_entity(self, partition: str, row: str, document: str, maximum: int) -> None:
        existing = self._read_document(partition, row, maximum)
        if existing is not None:
            if existing[0] != document:
                raise RouteProjectionConflict("ROUTE_IMMUTABLE_ENTITY_CONFLICT")
            return
        response = self._request(
            "POST",
            self._endpoint + "/" + self._table,
            json=route_entity(self.scope, partition, row, document),
            extra_headers={"Prefer": "return-no-content"},
        )
        if response.status_code not in {201, 204, 409}:
            response.raise_for_status()
            raise RouteProjectionUnknown("ROUTE_CREATE_NOT_CONFIRMED")
        readback = self._read_document(partition, row, maximum)
        if readback is None or readback[0] != document:
            raise RouteProjectionConflict("ROUTE_IMMUTABLE_READBACK_DIFFERS")

    def _snapshot_entities(
        self, snapshot: RouteSnapshot, checked_at: int,
    ) -> list[tuple[str, str, str, int]]:
        snapshot.require_valid(checked_at)
        if snapshot.approval.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_SNAPSHOT_SCOPE_MISMATCH")
        return [
            (content_partition(self.scope), route.sha256,
             canonical_document(route), MAX_ROUTE_BYTES)
            for route in snapshot.routes
        ] + [
            (member_partition(self.scope, snapshot.approval.route_version),
             member_row(member.alias), canonical_document(member), MAX_MEMBER_BYTES)
            for member in snapshot.members
        ] + [
            (catalog_partition(self.scope, snapshot.approval.route_version),
             member_row(route.alias), canonical_document(catalog_entry(route)), MAX_HEAD_BYTES)
            for route in sorted(snapshot.routes, key=lambda value: value.alias)
        ]

    @staticmethod
    def _slice_end(cursor: int, maximum: int, count: int) -> int:
        if (
            type(cursor) is not int or not 0 <= cursor <= count
            or type(maximum) is not int or not 1 <= maximum <= 100
        ):
            raise ValueError("ROUTE_PROJECTION_SLICE_INVALID")
        return min(count, cursor + maximum)

    def stage_model_addition(self, addition: ModelRouteAddition, checked_at: int) -> None:
        addition = ModelRouteAddition.model_validate_json(canonical_document(addition))
        if addition.program.scope != self.scope:
            raise RouteProjectionConflict("MODEL_ADDITION_SCOPE_CHANGED")
        existing = self._read_document(
            self.scope.partition, "model:" + member_row(addition.snapshot.routes[0].alias),
            MAX_HEAD_BYTES,
        )
        if existing is not None:
            raise RouteProjectionConflict("MODEL_ADDITION_ALREADY_ACTIVE")
        for partition, row, document, limit in self._snapshot_entities(
            addition.snapshot, checked_at,
        ):
            self._stage_entity(partition, row, document, limit)
        self.verify_slice_recorded(addition.snapshot, checked_at, 0, maximum=3)

    def read_model_active(self, alias: str) -> tuple[ModelRouteHead, str] | None:
        observed = self._read_document(
            self.scope.partition, "model:" + member_row(alias), MAX_HEAD_BYTES,
        )
        if observed is None:
            return None
        model = parse_document(ModelRouteHead, observed[0], MAX_HEAD_BYTES)
        if model.alias.casefold() != alias.casefold() or model.head.approval.scope != self.scope:
            raise RouteProjectionConflict("MODEL_ROUTE_HEAD_IDENTITY_CHANGED")
        return model, observed[1]

    def read_model_route(
        self, alias: str, program: ProgramIdentity, proof: ProgramCapabilityProof,
        budget_epoch: str, checked_at: int,
    ) -> RouteContent | None:
        observed = self.read_model_active(alias)
        if observed is None:
            return None
        model, _ = observed
        model.head.require_ready(program, proof, budget_epoch, checked_at)
        member_value = self._read_document(
            member_partition(self.scope, model.head.approval.route_version), member_row(alias),
            MAX_MEMBER_BYTES,
        )
        content_value = self._read_document(
            content_partition(self.scope), model.content_sha256, MAX_ROUTE_BYTES,
        )
        if member_value is None or content_value is None:
            raise RouteProjectionConflict("MODEL_ROUTE_RECORD_INCOMPLETE")
        member = parse_document(RouteMember, member_value[0], MAX_MEMBER_BYTES)
        route = parse_document(RouteContent, content_value[0], MAX_ROUTE_BYTES)
        if (route.alias != model.alias or route.model_id != model.model_id
                or route.sha256 != model.content_sha256 or member.content_sha256 != route.sha256):
            raise RouteProjectionConflict("MODEL_ROUTE_RECORD_CHANGED")
        snapshot = RouteSnapshot(approval=model.head.approval, routes=(route,), members=(member,))
        self.require_current_connections(snapshot, program, checked_at)
        return route

    def activate_model_addition(
        self, addition: ModelRouteAddition, proof: ProgramCapabilityProof, budget_epoch: str,
        operation_id: str, now: Callable[[], int], journal: ModelRouteActivationJournal,
    ) -> ModelRouteHead:
        addition = ModelRouteAddition.model_validate_json(canonical_document(addition))
        model = addition.active_head(proof, budget_epoch, now())
        if model.head.approval.scope != self.scope:
            raise RouteProjectionConflict("MODEL_ADDITION_SCOPE_CHANGED")
        self.require_verified_snapshot(addition.snapshot, now())
        if self.read_model_active(model.alias) is not None:
            raise RouteProjectionConflict("MODEL_ADDITION_ALREADY_ACTIVE")
        receipt = RouteTransitionReceipt(
            operation_id=operation_id, scope=self.scope, before_etag=None, before_generation=0,
            after_sha256=document_digest(model), route_version=model.head.approval.route_version,
            approval_id=model.head.approval.approval_id,
        )
        row = "model-transition:" + operation_id
        if self._read_document(self.scope.partition, row, MAX_HEAD_BYTES) is not None:
            raise RouteProjectionConflict("MODEL_ACTIVATION_EXISTS_RECOVER_ONLY")
        content_type, body = conditional_batch(
            self._endpoint, self._table, self.scope.partition,
            [(model.row_key, route_entity(self.scope, self.scope.partition, model.row_key,
                                         canonical_document(model)), None),
             (row, route_entity(self.scope, self.scope.partition, row,
                                canonical_document(receipt)), None)],
        )
        intent = ModelRouteActivationIntent(
            receipt, model, content_type, body, self._endpoint, self._table,
        )
        intent.require_valid()
        journal.begin_once(intent)
        try:
            dispatch_epoch = now()
            model.head.require_ready(addition.program, proof, budget_epoch, dispatch_epoch)
            if dispatch_epoch >= model.head.approval.activate_before_epoch:
                raise ValueError("MODEL_ACTIVATION_APPROVAL_EXPIRED")
            addition.snapshot.require_valid(dispatch_epoch)
        except ValueError:
            journal.record_outcome(operation_id, "expired_before_dispatch")
            raise
        try:
            response = self._request(
                "POST", self._endpoint + "/$batch",
                extra_headers={"Content-Type": content_type}, content=body,
            )
            if response.status_code in {409, 412}:
                raise TableBatchConflict("MODEL_ACTIVATION_CONFLICT")
            validate_batch_response(
                response.status_code, response.headers.get("Content-Type", ""), response.content, 2,
            )
        except TableBatchConflict as error:
            journal.record_outcome(operation_id, "conditional_conflict")
            raise RouteProjectionConflict("MODEL_ACTIVATION_CONFLICT") from error
        except (httpx.HTTPError, TableBatchUnavailable, RouteProjectionUnknown) as error:
            journal.record_outcome(operation_id, "unknown_no_replay")
            raise RouteProjectionUnknown("MODEL_ACTIVATION_UNKNOWN_RECOVER_ONLY") from error
        try:
            outcome = self.recover_model_addition(intent)
        except (httpx.HTTPError, RouteProjectionConflict, RouteProjectionUnknown):
            journal.record_outcome(operation_id, "unknown_no_replay")
            raise
        journal.record_outcome(operation_id, outcome)
        if outcome != "confirmed":
            raise RouteProjectionUnknown("MODEL_ACTIVATION_RECEIPT_NOT_CONFIRMED")
        return model

    def remove_model_head(self, expected: ModelRouteHead, expected_etag: str) -> None:
        expected = ModelRouteHead.model_validate_json(canonical_document(expected))
        if expected.head.approval.scope != self.scope:
            raise RouteProjectionConflict("MODEL_REMOVAL_SCOPE_CHANGED")
        etag = exact_etag(expected_etag)
        current = self.read_model_active(expected.alias)
        if current is None or current != (expected, etag):
            raise RouteProjectionConflict("MODEL_REMOVAL_HEAD_CHANGED")
        try:
            response = self._request(
                "DELETE", self._entity_url(self.scope.partition, expected.row_key),
                extra_headers={"If-Match": etag},
            )
        except httpx.HTTPError as error:
            raise RouteProjectionUnknown("MODEL_REMOVAL_UNKNOWN_RECOVER_ONLY") from error
        if response.status_code in {409, 412}:
            raise RouteProjectionConflict("MODEL_REMOVAL_ETAG_CHANGED")
        if response.status_code != 204:
            raise RouteProjectionUnknown("MODEL_REMOVAL_UNKNOWN_RECOVER_ONLY")
        if self.read_model_active(expected.alias) is not None:
            raise RouteProjectionUnknown("MODEL_REMOVAL_UNKNOWN_RECOVER_ONLY")

    def recover_model_addition(self, intent: ModelRouteActivationIntent) -> str:
        intent.require_valid()
        if (intent.endpoint != self._endpoint or intent.table != self._table
                or intent.receipt.scope != self.scope):
            raise RouteProjectionConflict("MODEL_ACTIVATION_TARGET_CHANGED")
        observed = self._read_document(
            self.scope.partition, "model-transition:" + intent.receipt.operation_id, MAX_HEAD_BYTES,
        )
        if observed is None:
            return "unknown_no_replay"
        if observed[0] != canonical_document(intent.receipt):
            raise RouteProjectionConflict("MODEL_ACTIVATION_RECEIPT_CHANGED")
        current = self.read_model_active(intent.head.alias)
        if current is None or current[0] != intent.head:
            raise RouteProjectionConflict("MODEL_ACTIVATION_HEAD_CHANGED")
        return "confirmed"

    def read_approval_renewal(
        self, route: RouteContent, member: RouteMember, program: ProgramIdentity, now: int,
    ) -> tuple[RouteApprovalRenewal, str] | None:
        observed = self.read_approval_renewal_history(route, member, program)
        if observed is not None:
            observed[0].require_current(route, member, program, now)
        return observed

    def read_approval_renewal_history(
        self, route: RouteContent, member: RouteMember, program: ProgramIdentity,
    ) -> tuple[RouteApprovalRenewal, str] | None:
        row = "approval:" + text_digest(route.sha256 + member.approval_sha256)
        observed = self._read_document(self.scope.partition, row, MAX_MEMBER_BYTES)
        if observed is None:
            return None
        renewal = parse_document(RouteApprovalRenewal, observed[0], MAX_MEMBER_BYTES)
        renewal.require_current(route, member, program, renewal.approval.issued_epoch)
        return renewal, observed[1]

    def require_current_connections(
        self, snapshot: RouteSnapshot, program: ProgramIdentity, now: int,
    ) -> None:
        snapshot.require_valid(snapshot.approval.issued_epoch)
        if snapshot.approval.scope != self.scope or program.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_SCOPE_CHANGED")
        members_by_alias = {member.alias: member for member in snapshot.members}
        for route in snapshot.routes:
            member = members_by_alias[route.alias]
            approval = member.connection_approval
            approval.require_current(route.connection, now)
            self.read_approval_renewal(route, member, program, now)

    def publish_approval_renewal(
        self, renewal: RouteApprovalRenewal, route: RouteContent, member: RouteMember,
        proof: ProgramCapabilityProof, expected_etag: str | None, now: int,
    ) -> None:
        renewal = parse_document(
            RouteApprovalRenewal, canonical_document(renewal), MAX_MEMBER_BYTES,
        )
        renewal.require_current(route, member, proof.identity, now)
        if (proof.identity.scope != self.scope
            or not proof.issued_epoch <= now < proof.expires_epoch
                or route.profile not in proof.supported_profiles):
            raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_PROOF_CHANGED")
        row = renewal.row_key
        receipt_row = "approval-receipt:" + renewal.approval.approval_id
        intent_row = "approval-intent:" + renewal.approval.approval_id
        document = canonical_document(renewal)
        receipt = self._read_document(self.scope.partition, receipt_row, MAX_MEMBER_BYTES)
        if receipt is not None:
            confirmed = self.read_approval_renewal(route, member, proof.identity, now)
            if receipt[0] != document or confirmed is None or confirmed[0] != renewal:
                raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_RECEIPT_CHANGED")
            return
        intent = self._read_document(self.scope.partition, intent_row, MAX_MEMBER_BYTES)
        if intent is not None:
            raise RouteProjectionUnknown("ROUTE_APPROVAL_RENEWAL_UNKNOWN_RECOVER_ONLY")
        current = self._read_document(self.scope.partition, row, MAX_MEMBER_BYTES)
        if (current[1] if current is not None else None) != expected_etag:
            raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_ETAG_CHANGED")
        if current is not None:
            previous = parse_document(RouteApprovalRenewal, current[0], MAX_MEMBER_BYTES)
            if (previous.model_dump(exclude={"approval"})
                    != renewal.model_dump(exclude={"approval"})
                    or previous.approval.connection != renewal.approval.connection
                    or renewal.approval.issued_epoch < previous.approval.issued_epoch
                    or renewal.approval.expires_epoch <= previous.approval.expires_epoch):
                raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_REGRESSION")
        changes = [(row, renewal, expected_etag), (receipt_row, renewal, None)]
        content_type, body = conditional_batch(
            self._endpoint, self._table, self.scope.partition,
            [(key, route_entity(
                self.scope, self.scope.partition, key, canonical_document(value),
            ), etag)
             for key, value, etag in changes],
        )
        require_route_batch(self._endpoint, self._table, self.scope, content_type, body, changes)
        record = canonical_document({
            "version": 1, "renewal": renewal.model_dump(mode="json"),
            "expected_etag": expected_etag, "endpoint": self._endpoint, "table": self._table,
            "body_sha256": text_digest(body.decode()),
        })
        saved = self._request(
            "POST", self._endpoint + "/" + self._table,
            json=route_entity(self.scope, self.scope.partition, intent_row, record),
            extra_headers={"Prefer": "return-no-content"},
        )
        if saved.status_code not in {201, 204}:
            raise RouteProjectionUnknown("ROUTE_APPROVAL_RENEWAL_INTENT_NOT_OWNED")
        observed_intent = self._read_document(self.scope.partition, intent_row, MAX_MEMBER_BYTES)
        if observed_intent is None or observed_intent[0] != record:
            raise RouteProjectionUnknown("ROUTE_APPROVAL_RENEWAL_INTENT_NOT_CONFIRMED")
        try:
            response = self._request(
                "POST", self._endpoint + "/$batch",
                extra_headers={"Content-Type": content_type}, content=body,
            )
            if response.status_code in {409, 412}:
                raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_CONFLICT")
            validate_batch_response(
                response.status_code, response.headers.get("Content-Type", ""), response.content, 2,
            )
        except (httpx.HTTPError, TableBatchUnavailable, RouteProjectionUnknown):
            pass
        receipt = self._read_document(self.scope.partition, receipt_row, MAX_MEMBER_BYTES)
        if receipt is None:
            raise RouteProjectionUnknown("ROUTE_APPROVAL_RENEWAL_UNKNOWN_RECOVER_ONLY")
        current_renewal = self.read_approval_renewal(route, member, proof.identity, now)
        if receipt[0] != document or current_renewal is None or current_renewal[0] != renewal:
            raise RouteProjectionConflict("ROUTE_APPROVAL_RENEWAL_RECEIPT_CHANGED")

    def stage_slice(
        self, snapshot: RouteSnapshot, checked_at: int, cursor: int, *, maximum: int = 16,
    ) -> int:
        entities = self._snapshot_entities(snapshot, checked_at)
        end = self._slice_end(cursor, maximum, len(entities))
        for partition, row, document, limit in entities[cursor:end]:
            self._stage_entity(partition, row, document, limit)
        return end

    def verify_slice(
        self, snapshot: RouteSnapshot, checked_at: int, cursor: int, *, maximum: int = 16,
    ) -> int:
        entities = self._snapshot_entities(snapshot, checked_at)
        end = self._slice_end(cursor, maximum, len(entities))
        for partition, row, document, limit in entities[cursor:end]:
            observed = self._read_document(partition, row, limit)
            if observed is None or observed[0] != document:
                raise RouteProjectionConflict("ROUTE_SNAPSHOT_READBACK_INCOMPLETE")
        return end

    @staticmethod
    def _readback_receipt(snapshot: RouteSnapshot, cursor: int) -> tuple[str, str]:
        digest = snapshot.configuration_sha256
        return (
            "readback:" + digest + ":" + str(cursor),
            canonical_document({
                "version": 1, "scope": snapshot.approval.scope.model_dump(mode="json"),
                "configuration_sha256": digest, "verified_entities": cursor,
                "total_entities": 3 * len(snapshot.routes),
            }),
        )

    def verify_slice_recorded(
        self, snapshot: RouteSnapshot, checked_at: int, cursor: int, *, maximum: int = 16,
    ) -> int:
        snapshot.require_valid(checked_at)
        self._slice_end(cursor, maximum, 3 * len(snapshot.routes))
        if cursor:
            row, document = self._readback_receipt(snapshot, cursor)
            previous = self._read_document(self.scope.partition, row, MAX_HEAD_BYTES)
            if previous is None or previous[0] != document:
                raise RouteProjectionConflict("ROUTE_PREVIOUS_READBACK_RECEIPT_MISSING")
        end = self.verify_slice(snapshot, checked_at, cursor, maximum=maximum)
        row, document = self._readback_receipt(snapshot, end)
        self._stage_entity(self.scope.partition, row, document, MAX_HEAD_BYTES)
        return end

    def require_verified_snapshot(self, snapshot: RouteSnapshot, checked_at: int) -> None:
        snapshot.require_valid(checked_at)
        if snapshot.approval.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_SNAPSHOT_SCOPE_MISMATCH")
        row, document = self._readback_receipt(snapshot, 3 * len(snapshot.routes))
        observed = self._read_document(self.scope.partition, row, MAX_HEAD_BYTES)
        if observed is None or observed[0] != document:
            raise RouteProjectionConflict("ROUTE_FINAL_READBACK_RECEIPT_MISSING")

    def stage(self, snapshot: RouteSnapshot, checked_at: int) -> None:
        for partition, row, document, limit in self._snapshot_entities(snapshot, checked_at):
            self._stage_entity(partition, row, document, limit)
        self.verify_snapshot(snapshot, checked_at)

    def verify_snapshot(self, snapshot: RouteSnapshot, checked_at: int) -> None:
        for partition, row, document, limit in self._snapshot_entities(snapshot, checked_at):
            observed = self._read_document(partition, row, limit)
            if observed is None or observed[0] != document:
                raise RouteProjectionConflict("ROUTE_SNAPSHOT_READBACK_INCOMPLETE")

    def stage_candidate(
        self, candidate: RouteCandidateProbe, snapshot: RouteSnapshot,
        program: ProgramIdentity, proof: ProgramCapabilityProof, checked_at: int,
    ) -> None:
        candidate = parse_document(
            RouteCandidateProbe, canonical_document(candidate), MAX_MEMBER_BYTES,
        )
        snapshot.require_valid(checked_at)
        candidate.head.require_ready(
            program, proof, snapshot.approval.budget_epoch, checked_at,
        )
        route = next((route for route in snapshot.routes if route.alias == candidate.alias), None)
        if (
            candidate.head.approval != snapshot.approval or program.scope != self.scope
            or route is None or not (
                candidate.operation in route.operations
                or candidate.operation == "anthropic-count-tokens"
                and route.profile == "anthropic-messages"
            )
            or not candidate.issued_epoch <= checked_at < candidate.expires_epoch
            or candidate.expires_epoch > proof.expires_epoch
        ):
            raise RouteProjectionConflict("ROUTE_CANDIDATE_SNAPSHOT_CHANGED")
        route.connection.require_probe_member(candidate.member_backend_id)
        self._stage_entity(
            self.scope.partition, "candidate:" + candidate.probe_id,
            canonical_document(candidate), MAX_MEMBER_BYTES,
        )

    @staticmethod
    def _program_proof_row(identity: ProgramIdentity) -> str:
        return "proof:" + text_digest(json.dumps(
            [identity.api_revision, identity.scope.gateway_id, identity.instance_id],
            separators=(",", ":"),
        ))

    def read_program_proof(
        self, identity: ProgramIdentity,
    ) -> tuple[ProgramCapabilityProof, str] | None:
        if identity.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_SCOPE_CHANGED")
        observed = self._read_document(
            self.scope.partition, self._program_proof_row(identity), 8192,
        )
        if observed is None:
            return None
        proof = parse_document(ProgramCapabilityProof, observed[0], 8192)
        if proof.identity != identity:
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_IDENTITY_CHANGED")
        return proof, observed[1]

    def publish_program_proof(
        self, proof: ProgramCapabilityProof, identity: ProgramIdentity,
        expected_etag: str | None, checked_at: int,
    ) -> None:
        self._publish_program_proof(proof, identity, expected_etag, checked_at, identity)

    def publish_upgraded_program_proof(
        self, upgrade: FixedProgramUpgrade, previous: FixedProgramInstallation,
        proof: ProgramCapabilityProof, expected_etag: str | None, checked_at: int,
    ) -> None:
        upgrade.require_previous(upgrade.previous_publication_id, previous)
        if (upgrade.installation.storage_endpoint != self._endpoint
                or upgrade.installation.storage_table != self._table):
            raise RouteProjectionConflict("ROUTE_PROGRAM_UPGRADE_STORAGE_CHANGED")
        self._publish_program_proof(
            proof, upgrade.installation.program, expected_etag, checked_at, previous.program,
        )

    def _publish_program_proof(
        self, proof: ProgramCapabilityProof, identity: ProgramIdentity,
        expected_etag: str | None, checked_at: int, previous_identity: ProgramIdentity,
    ) -> None:
        proof = parse_document(ProgramCapabilityProof, canonical_document(proof), 8192)
        identity = parse_document(ProgramIdentity, canonical_document(identity), MAX_HEAD_BYTES)
        if (
            identity.scope != self.scope or proof.identity != identity
            or type(checked_at) is not int
            or not proof.issued_epoch <= checked_at < proof.expires_epoch
        ):
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_IDENTITY_OR_TIME_INVALID")
        if expected_etag is not None:
            exact_etag(expected_etag)
        document = canonical_document(proof)
        row = self._program_proof_row(identity)
        receipt_row = "proof-receipt:" + proof.proof_id
        prior = self._read_document(self.scope.partition, receipt_row, 8192)
        if prior is not None:
            current = self.read_program_proof(identity)
            if prior[0] != document or current is None or current[0] != proof:
                raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_RECEIPT_CHANGED")
            return
        intent_row = "proof-intent:" + proof.proof_id
        previous_intent = self._read_document(self.scope.partition, intent_row, MAX_MEMBER_BYTES)
        if previous_intent is not None:
            recorded = json.loads(previous_intent[0], object_pairs_hook=unique_object)
            if (
                set(recorded) != {
                    "version", "proof", "expected_etag", "endpoint", "table", "content_type",
                    "body_base64", "body_sha256",
                }
                or type(recorded["version"]) is not int or recorded["version"] != 1
                or recorded["proof"] != proof.model_dump(mode="json")
                or recorded["expected_etag"] != expected_etag
                or recorded["endpoint"] != self._endpoint or recorded["table"] != self._table
                or not isinstance(recorded["body_base64"], str)
                or len(recorded["body_base64"]) > 32768
            ):
                raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_INTENT_CHANGED")
            recorded_body = base64.b64decode(recorded["body_base64"], validate=True)
            if text_digest(recorded_body.decode()) != recorded["body_sha256"]:
                raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_INTENT_CHANGED")
            require_route_batch(
                self._endpoint, self._table, self.scope, recorded["content_type"], recorded_body,
                [(row, proof, expected_etag), (receipt_row, proof, None)],
            )
            observed = self._read_document(self.scope.partition, receipt_row, 8192)
            if observed is None:
                raise RouteProjectionUnknown("ROUTE_PROGRAM_PROOF_UNKNOWN_RECOVER_ONLY")
            current = self.read_program_proof(identity)
            if observed[0] != document or current is None or current[0] != proof:
                raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_RECEIPT_CHANGED")
            return
        current = self.read_program_proof(previous_identity)
        if (current[1] if current is not None else None) != expected_etag:
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_ETAG_CHANGED")
        if current is not None and (
            proof.issued_epoch < current[0].issued_epoch
            or proof.expires_epoch <= current[0].expires_epoch
            or proof.proof_id == current[0].proof_id
        ):
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_REGRESSION")
        content_type, body = conditional_batch(
            self._endpoint, self._table, self.scope.partition,
            [(row, route_entity(self.scope, self.scope.partition, row, document), expected_etag),
             (receipt_row, route_entity(
                 self.scope, self.scope.partition, receipt_row, document,
             ), None)],
        )
        require_route_batch(
            self._endpoint, self._table, self.scope, content_type, body,
            [(row, proof, expected_etag), (receipt_row, proof, None)],
        )
        intent_document = canonical_document({
            "version": 1, "proof": proof.model_dump(mode="json"), "expected_etag": expected_etag,
            "endpoint": self._endpoint, "table": self._table, "content_type": content_type,
            "body_base64": base64.b64encode(body).decode("ascii"),
            "body_sha256": text_digest(body.decode()),
        })
        if (len(intent_document.encode()) > MAX_MEMBER_BYTES
                or len(intent_document.encode("utf-16-le")) > 32768):
            raise ValueError("ROUTE_PROGRAM_PROOF_INTENT_TOO_LARGE")
        saved = self._request(
            "POST", self._endpoint + "/" + self._table,
            json=route_entity(self.scope, self.scope.partition, intent_row, intent_document),
            extra_headers={"Prefer": "return-no-content"},
        )
        if saved.status_code not in {201, 204}:
            raise RouteProjectionUnknown("ROUTE_PROGRAM_PROOF_INTENT_NOT_OWNED_RECOVER_ONLY")
        saved_intent = self._read_document(self.scope.partition, intent_row, MAX_MEMBER_BYTES)
        if saved_intent is None or saved_intent[0] != intent_document:
            raise RouteProjectionUnknown("ROUTE_PROGRAM_PROOF_INTENT_NOT_CONFIRMED")
        try:
            response = self._request(
                "POST", self._endpoint + "/$batch",
                extra_headers={"Content-Type": content_type}, content=body,
            )
            if response.status_code in {409, 412}:
                raise TableBatchConflict("ROUTE_PROGRAM_PROOF_CONFLICT")
            validate_batch_response(
                response.status_code, response.headers.get("Content-Type", ""), response.content, 2,
            )
        except TableBatchConflict as error:
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_CONFLICT") from error
        except (httpx.HTTPError, TableBatchUnavailable, RouteProjectionUnknown):
            pass
        observed = self._read_document(self.scope.partition, receipt_row, 8192)
        if observed is None:
            raise RouteProjectionUnknown("ROUTE_PROGRAM_PROOF_UNKNOWN_RECOVER_ONLY")
        if observed[0] != document:
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_RECEIPT_CHANGED")
        current = self.read_program_proof(identity)
        if current is None or current[0] != proof:
            raise RouteProjectionConflict("ROUTE_PROGRAM_PROOF_READBACK_CHANGED")

    def read_active(self) -> tuple[ActiveRouteHead, str] | None:
        observed = self._read_document(self.scope.partition, "active", MAX_HEAD_BYTES)
        if observed is None:
            return None
        head = parse_document(ActiveRouteHead, observed[0], MAX_HEAD_BYTES)
        if head.approval.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_ACTIVE_SCOPE_MISMATCH")
        return head, observed[1]

    def read_writer(self) -> tuple[RouteWriterState, str] | None:
        observed = self._read_document(self.scope.partition, "writer", MAX_HEAD_BYTES)
        if observed is None:
            return None
        state = parse_document(RouteWriterState, observed[0], MAX_HEAD_BYTES)
        if state.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_WRITER_SCOPE_MISMATCH")
        return state, observed[1]

    def _change_writer(
        self, before: tuple[RouteWriterState, str] | None, after: RouteWriterState,
        kind: Literal["claim", "takeover", "renew", "prepare", "abort"], operation_id: str,
        now: Callable[[], int], journal: RouteWriterJournal,
    ) -> tuple[RouteWriterState, str]:
        receipt = RouteWriterReceipt(
            operation_id=operation_id, scope=self.scope, kind=kind,
            before_etag=before[1] if before is not None else None,
            before_sha256=document_digest(before[0]) if before is not None else None,
            after_sha256=document_digest(after), owner_token=after.owner_token,
            writer_epoch=after.writer_epoch,
        )
        row = "writer-transition:" + operation_id
        if self._read_document(self.scope.partition, row, MAX_HEAD_BYTES) is not None:
            raise RouteProjectionConflict("ROUTE_WRITER_TRANSITION_EXISTS_RECOVER_ONLY")
        content_type, body = conditional_batch(
            self._endpoint, self._table, self.scope.partition,
            [("writer", route_entity(self.scope, self.scope.partition, "writer",
                                     canonical_document(after)), receipt.before_etag),
             (row, route_entity(self.scope, self.scope.partition, row,
                                canonical_document(receipt)), None)],
        )
        intent = RouteWriterIntent(
            receipt, before[0] if before is not None else None, after, content_type, body,
            text_digest(body.decode()), self._endpoint, self._table,
        )
        intent.to_record()
        journal.begin_once(intent)
        try:
            after.require_owner(after.owner_token, after.publication_id, now())
            if before is not None and kind not in {"takeover", "claim"}:
                before[0].require_owner(after.owner_token, after.publication_id, now())
        except ValueError:
            journal.record_outcome(operation_id, "expired_before_dispatch")
            raise
        try:
            response = self._request(
                "POST", self._endpoint + "/$batch",
                extra_headers={"Content-Type": content_type}, content=body,
            )
            if response.status_code in {409, 412}:
                raise TableBatchConflict("ROUTE_WRITER_CONFLICT")
            validate_batch_response(
                response.status_code, response.headers.get("Content-Type", ""), response.content, 2,
            )
        except TableBatchConflict as error:
            journal.record_outcome(operation_id, "conditional_conflict")
            raise RouteProjectionConflict("ROUTE_WRITER_CONFLICT") from error
        except (httpx.HTTPError, TableBatchUnavailable, RouteProjectionUnknown) as error:
            journal.record_outcome(operation_id, "unknown_no_replay")
            raise RouteProjectionUnknown("ROUTE_WRITER_UNKNOWN_RECOVER_ONLY") from error
        outcome = self.recover_writer(intent)
        journal.record_outcome(operation_id, outcome)
        if outcome != "confirmed":
            raise RouteProjectionUnknown("ROUTE_WRITER_RECEIPT_NOT_CONFIRMED")
        current = self.read_writer()
        if current is None or current[0] != after:
            raise RouteProjectionConflict("ROUTE_WRITER_ALREADY_SUPERSEDED")
        current[0].require_owner(after.owner_token, after.publication_id, now())
        return current

    def claim_writer(
        self, owner_token: str, publication_id: str, operation_id: str,
        now: Callable[[], int], journal: RouteWriterJournal,
    ) -> tuple[RouteWriterState, str]:
        current = self.read_writer()
        if current is not None:
            raise RouteProjectionConflict("ROUTE_WRITER_EXISTS_USE_TAKEOVER")
        active = self.read_active()
        instant = now()
        state = RouteWriterState(
            scope=self.scope, owner_token=owner_token, publication_id=publication_id,
            writer_epoch=1, leased_epoch=instant, lease_until_epoch=instant + 30,
            active_generation=active[0].approval.generation if active is not None else 0,
            active_version=active[0].approval.route_version if active is not None else None,
            active_sha256=document_digest(active[0]) if active is not None else None,
        )
        return self._change_writer(None, state, "claim", operation_id, now, journal)

    def takeover_writer(
        self, current: tuple[RouteWriterState, str], owner_token: str, publication_id: str,
        operation_id: str, now: Callable[[], int], journal: RouteWriterJournal,
    ) -> tuple[RouteWriterState, str]:
        after = current[0].takeover(owner_token, publication_id, now())
        return self._change_writer(current, after, "takeover", operation_id, now, journal)

    def renew_writer(
        self, current: tuple[RouteWriterState, str], owner_token: str, publication_id: str,
        operation_id: str, now: Callable[[], int], journal: RouteWriterJournal,
    ) -> tuple[RouteWriterState, str]:
        after = current[0].renew(owner_token, publication_id, now())
        return self._change_writer(current, after, "renew", operation_id, now, journal)

    def prepare_writer(
        self, current: tuple[RouteWriterState, str], pending: RoutePendingActivation,
        owner_token: str, publication_id: str, operation_id: str, now: Callable[[], int],
        journal: RouteWriterJournal,
    ) -> tuple[RouteWriterState, str]:
        after = current[0].prepare(pending, owner_token, publication_id, now())
        return self._change_writer(current, after, "prepare", operation_id, now, journal)

    def abort_writer(
        self, current: tuple[RouteWriterState, str], pending: RoutePendingActivation,
        owner_token: str, publication_id: str, operation_id: str, now: Callable[[], int],
        journal: RouteWriterJournal,
    ) -> tuple[RouteWriterState, str]:
        current[0].require_owner(owner_token, publication_id, now())
        if current[0].pending != pending:
            raise RouteProjectionConflict("ROUTE_WRITER_ABORT_PENDING_CHANGED")
        after = RouteWriterState.model_validate({
            **current[0].model_dump(mode="python"), "pending": None,
        })
        return self._change_writer(current, after, "abort", operation_id, now, journal)

    def recover_writer(
        self, intent: RouteWriterIntent,
    ) -> Literal["confirmed", "conflict", "unknown", "not_applied_fenced"]:
        intent.to_record()
        if (
            intent.receipt.scope != self.scope or intent.endpoint != self._endpoint
            or intent.table != self._table
        ):
            raise RouteProjectionConflict("ROUTE_WRITER_RECOVERY_TARGET_CHANGED")
        row = "writer-transition:" + intent.receipt.operation_id
        observed = self._read_document(self.scope.partition, row, MAX_HEAD_BYTES)
        if observed is not None:
            return "confirmed" if observed[0] == canonical_document(intent.receipt) else "conflict"
        current = self.read_writer()
        if current is None or current[1] == intent.receipt.before_etag:
            return "unknown"
        second = self._read_document(self.scope.partition, row, MAX_HEAD_BYTES)
        if second is not None:
            return "confirmed" if second[0] == canonical_document(intent.receipt) else "conflict"
        return "not_applied_fenced"

    def activate(
        self,
        snapshot: RouteSnapshot,
        identity: ProgramIdentity,
        proof: ProgramCapabilityProof,
        active_budget_epoch: str,
        operation_id: str,
        expected_etag: str | None,
        now: Callable[[], int],
        journal: RouteActivationJournal,
        *,
        writer: tuple[RouteWriterState, str] | None = None,
    ) -> Literal["confirmed"]:
        if writer is not None:
            self.require_verified_snapshot(snapshot, now())
        else:
            self.verify_snapshot(snapshot, now())
        observed_writer = self.read_writer()
        if writer is None and observed_writer is not None:
            raise RouteProjectionConflict("ROUTE_ACTIVATION_WRITER_REQUIRED")
        if writer is not None and observed_writer != writer:
            raise RouteProjectionConflict("ROUTE_ACTIVATION_WRITER_ETAG_CHANGED")
        current = self.read_active()
        if (None if current is None else current[1]) != expected_etag:
            raise RouteProjectionConflict("ROUTE_ACTIVATION_ETAG_CHANGED")
        generation = 0 if current is None else current[0].approval.generation
        if snapshot.approval.generation != generation + 1:
            raise RouteProjectionConflict("ROUTE_ACTIVATION_GENERATION_INVALID")
        if expected_etag is not None:
            exact_etag(expected_etag)
        instant = now()
        head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=instant)
        head.require_ready(identity, proof, active_budget_epoch, instant, activating=True)
        snapshot.require_valid(instant)
        writer_after = None
        if writer is not None:
            state = writer[0]
            state.require_owner(state.owner_token, state.publication_id, instant)
            if (
                state.scope != self.scope or state.active_generation != generation
                or state.active_sha256 != (document_digest(current[0]) if current else None)
                or state.pending != RoutePendingActivation(
                    operation_id=operation_id, base_generation=generation,
                    approval=snapshot.approval, snapshot_sha256=document_digest(snapshot),
                )
            ):
                raise RouteProjectionConflict("ROUTE_ACTIVATION_PENDING_CHANGED")
            writer_after = RouteWriterState.model_validate({
                **state.model_dump(mode="python"), "pending": None,
                "active_generation": head.approval.generation,
                "active_version": head.approval.route_version,
                "active_sha256": document_digest(head),
            })
        receipt = RouteTransitionReceipt(
            operation_id=operation_id,
            scope=self.scope,
            before_etag=expected_etag,
            before_generation=generation,
            after_sha256=document_digest(head),
            route_version=head.approval.route_version,
            approval_id=head.approval.approval_id,
        )
        receipt_row = "transition:" + receipt.operation_id
        try:
            previous = self._read_document(self.scope.partition, receipt_row, MAX_HEAD_BYTES)
        except (httpx.HTTPError, RouteProjectionUnknown) as error:
            raise RouteProjectionUnknown("ROUTE_TRANSITION_UNCERTAIN_RECOVER_ONLY") from error
        if previous is not None:
            raise RouteProjectionConflict("ROUTE_TRANSITION_ALREADY_EXISTS_RECOVER_ONLY")
        content_type, body = conditional_batch(
            self._endpoint,
            self._table,
            self.scope.partition,
            ([("writer", route_entity(
                self.scope, self.scope.partition, "writer", canonical_document(writer_after),
            ), writer[1])] if writer is not None and writer_after is not None else []) + [
                (
                    "active",
                    route_entity(
                        self.scope,
                        self.scope.partition,
                        "active",
                        canonical_document(head),
                    ),
                    expected_etag,
                ),
                (
                    receipt_row,
                    route_entity(
                        self.scope,
                        self.scope.partition,
                        receipt_row,
                        canonical_document(receipt),
                    ),
                    None,
                ),
            ],
        )
        intent = RouteActivationIntent(
            receipt, head, content_type, body, text_digest(body.decode()),
            self._endpoint, self._table,
            writer[0] if writer is not None else None, writer_after,
            writer[1] if writer is not None else None,
        )
        intent.to_record()
        journal.begin_once(intent)
        dispatch_at = now()
        try:
            if writer is not None:
                writer[0].require_owner(
                    writer[0].owner_token, writer[0].publication_id, dispatch_at,
                )
            head.require_ready(identity, proof, active_budget_epoch, dispatch_at)
            snapshot.require_valid(dispatch_at)
            if dispatch_at >= head.approval.activate_before_epoch:
                raise RouteProjectionConflict("ROUTE_ACTIVATION_WINDOW_EXPIRED")
        except ValueError:
            journal.record_outcome(operation_id, "expired_before_dispatch")
            raise
        try:
            response = self._request(
                "POST",
                self._endpoint + "/$batch",
                extra_headers={"Content-Type": content_type},
                content=body,
            )
            if response.status_code in {409, 412}:
                raise TableBatchConflict("ROUTE_ACTIVATION_CONFLICT")
            validate_batch_response(
                response.status_code,
                response.headers.get("Content-Type", ""),
                response.content,
                3 if writer is not None else 2,
            )
        except TableBatchConflict as error:
            journal.record_outcome(operation_id, "conditional_conflict")
            raise RouteProjectionConflict("ROUTE_ACTIVATION_CONFLICT") from error
        except (httpx.HTTPError, TableBatchUnavailable, RouteProjectionUnknown) as error:
            journal.record_outcome(operation_id, "unknown_no_replay")
            raise RouteProjectionUnknown("ROUTE_ACTIVATION_UNKNOWN_RECOVER_ONLY") from error
        try:
            outcome = self.recover_activation(intent)
        except (httpx.HTTPError, ValueError, RouteProjectionUnknown) as error:
            journal.record_outcome(operation_id, "unknown_no_replay")
            raise RouteProjectionUnknown("ROUTE_ACTIVATION_RECEIPT_NOT_CONFIRMED") from error
        journal.record_outcome(operation_id, outcome)
        if outcome != "confirmed":
            raise RouteProjectionUnknown("ROUTE_ACTIVATION_RECEIPT_NOT_CONFIRMED")
        return "confirmed"

    def initialize_empty_scope(
        self, identity: ProgramIdentity, proof: ProgramCapabilityProof,
        budget_epoch: str, operation_id: str, clock: Callable[[], int],
    ) -> ActiveRouteHead:
        from ..domain.storage_routes import (
            ConfigurationApproval,
            catalog_digest,
            configuration_manifests,
        )

        if identity.scope != self.scope or proof.identity != identity:
            raise RouteProjectionConflict("ROUTE_INITIALIZATION_PROGRAM_CHANGED")
        row = "initialization:" + str(UUID(operation_id))
        recorded = self._read_document(self.scope.partition, row, 65536)
        if recorded is not None:
            intent = RouteActivationIntent.from_record(
                json.loads(recorded[0], object_pairs_hook=unique_object),
            )
            if (
                intent.receipt.operation_id != operation_id
                or intent.head.approval.required_program_sha256 != identity.program_sha256
                or intent.head.approval.budget_epoch != budget_epoch
                or intent.head.approval.entry_count != 0
                or self.recover_activation(intent) != "confirmed"
            ):
                raise RouteProjectionUnknown("ROUTE_INITIALIZATION_RECOVERY_ONLY")
            observed = self.read_active()
            if observed is None or observed[0] != intent.head:
                raise RouteProjectionConflict("ROUTE_INITIALIZATION_HEAD_CHANGED")
            return intent.head
        if self.read_active() is not None:
            raise RouteProjectionConflict("ROUTE_INITIALIZATION_CANNOT_REPLACE_HEAD")
        instant = clock()
        version = str(uuid5(UUID(operation_id), "empty-configuration"))
        _, manifest, dependencies = configuration_manifests(self.scope, version, (), {}, instant)
        approval = ConfigurationApproval(
            approval_id=str(uuid5(UUID(operation_id), "empty-approval")), scope=self.scope,
            route_version=version, generation=1, manifest_sha256=manifest,
            dependency_manifest_sha256=dependencies, catalog_sha256=catalog_digest(()),
            entry_count=0, required_program_sha256=identity.program_sha256,
            required_profiles=(), budget_epoch=budget_epoch, issued_epoch=instant,
            activate_before_epoch=instant + 600,
        )
        snapshot = RouteSnapshot.prepare(approval, (), {}, instant)
        self.stage(snapshot, instant)
        projection = self

        class Journal:
            head: ActiveRouteHead | None = None

            def begin_once(self, intent: RouteActivationIntent) -> None:
                if projection._read_document(projection.scope.partition, row, 65536) is not None:
                    raise RouteProjectionUnknown("ROUTE_INITIALIZATION_RECOVERY_ONLY")
                projection._stage_entity(
                    projection.scope.partition, row, canonical_document(intent.to_record()), 65536,
                )
                self.head = intent.head

            def record_outcome(self, current_operation: str, outcome: str) -> None:
                if current_operation != operation_id:
                    raise RouteProjectionConflict("ROUTE_INITIALIZATION_OPERATION_CHANGED")

        journal = Journal()
        self.activate(snapshot, identity, proof, budget_epoch, operation_id, None, clock, journal)
        if journal.head is None:
            raise RouteProjectionUnknown("ROUTE_INITIALIZATION_RECOVERY_ONLY")
        return journal.head

    def recover_activation(
        self, intent: RouteActivationIntent,
    ) -> Literal["confirmed", "conflict", "unknown", "not_applied_fenced"]:
        intent.to_record()
        if intent.endpoint != self._endpoint or intent.table != self._table:
            raise RouteProjectionConflict("ROUTE_ACTIVATION_RECOVERY_TARGET_CHANGED")
        outcome = self.recover(intent.receipt)
        if outcome != "unknown" or intent.writer_etag is None:
            return outcome
        writer = self.read_writer()
        if writer is None or writer[1] == intent.writer_etag:
            return "unknown"
        observed = self._read_document(
            self.scope.partition, "transition:" + intent.receipt.operation_id, MAX_HEAD_BYTES,
        )
        if observed is not None:
            return "confirmed" if observed[0] == canonical_document(intent.receipt) else "conflict"
        return "not_applied_fenced"

    def recover(
        self,
        receipt: RouteTransitionReceipt,
    ) -> Literal["confirmed", "conflict", "unknown", "not_applied_fenced"]:
        if receipt.scope != self.scope:
            raise RouteProjectionConflict("ROUTE_RECEIPT_SCOPE_MISMATCH")
        row = "transition:" + receipt.operation_id
        observed = self._read_document(self.scope.partition, row, MAX_HEAD_BYTES)
        if observed is not None:
            return "confirmed" if observed[0] == canonical_document(receipt) else "conflict"
        current = self.read_active()
        if current is None or current[1] == receipt.before_etag:
            return "unknown"
        second = self._read_document(self.scope.partition, row, MAX_HEAD_BYTES)
        if second is not None:
            return "confirmed" if second[0] == canonical_document(receipt) else "conflict"
        return "not_applied_fenced"
