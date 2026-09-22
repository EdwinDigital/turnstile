from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel

from ..domain.ledger import (
    LedgerAdmissionReceipt,
    LedgerAttempt,
    LedgerAttemptIdentity,
    LedgerCharge,
    LedgerDispatch,
    LedgerProtocolConflict,
    LedgerRecoveryCursor,
    LedgerScope,
    LedgerSummary,
    complete_ledger,
    reserve_ledger,
    settle_ledger,
    transition_ledger_attempt,
    update_ledger_policy,
)
from .ledger import TableStorageLedger
from .table_batch import (
    TableBatchConflict,
    TableBatchUnavailable,
    conditional_batch,
    exact_etag,
    validate_batch_response,
)

SUMMARY_ROW = "S"


class LedgerUnavailable(RuntimeError):
    pass


class LedgerCompareFailed(LedgerUnavailable):
    pass


@dataclass(frozen=True)
class ConditionalEntity:
    row_key: str
    document: BaseModel
    etag: str | None


@dataclass(frozen=True)
class ReservedLedgerAttempt:
    summary: LedgerSummary
    attempt: LedgerAttempt
    created: bool


class AtomicTableLedger(TableStorageLedger):
    def __enter__(self) -> AtomicTableLedger:
        return self

    @staticmethod
    def _document(value: BaseModel) -> str:
        serialized = value.model_dump_json()
        if len(serialized.encode("utf-16-le")) > 60_000:
            raise LedgerProtocolConflict("Ledger document exceeds its property limit")
        return serialized

    def _read(self, scope: LedgerScope, row_key: str) -> tuple[dict[str, Any], str] | None:
        response = self._request("GET", self._entity_url(scope.partition, row_key))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        value = response.json()
        try:
            etag = exact_etag(response.headers.get("etag") or value.get("odata.etag"))
        except ValueError as error:
            raise LedgerUnavailable("Ledger read returned no usable concurrency token") from error
        document = json.loads(value["Document"])
        if not isinstance(document, dict):
            raise LedgerUnavailable("Ledger read returned an invalid document")
        return document, etag

    def read_summary(self, scope: LedgerScope) -> LedgerSummary:
        current = self._read(scope, SUMMARY_ROW)
        if current is None:
            raise LedgerUnavailable("Budget execution scope is not initialized")
        summary = LedgerSummary.model_validate(current[0])
        if summary.scope != scope:
            raise LedgerProtocolConflict("Ledger summary belongs to a different scope")
        return summary

    def _commit(self, scope: LedgerScope, entities: Sequence[ConditionalEntity]) -> None:
        try:
            content_type, content = conditional_batch(
                self._endpoint,
                self._table,
                scope.partition,
                [
                    (
                        entity.row_key,
                        {
                            "Document": self._document(entity.document),
                            **(
                                {"RecoveryPending": not entity.document.recovery_complete}
                                if isinstance(entity.document, LedgerDispatch)
                                else {}
                            ),
                        },
                        entity.etag,
                    )
                    for entity in entities
                ],
            )
        except ValueError as error:
            raise LedgerProtocolConflict(str(error)) from error
        response = self._request(
            "POST",
            f"{self._endpoint}/$batch",
            extra_headers={"Content-Type": content_type},
            content=content,
        )
        response.raise_for_status()
        try:
            validate_batch_response(
                response.status_code,
                response.headers.get("content-type", ""),
                response.content,
                len(entities),
            )
        except TableBatchConflict as error:
            raise LedgerCompareFailed(str(error)) from error
        except TableBatchUnavailable as error:
            raise LedgerUnavailable(str(error)) from error

    def initialize(self, summary: LedgerSummary) -> None:
        existing = self._read(summary.scope, SUMMARY_ROW)
        if existing is not None:
            if LedgerSummary.model_validate(existing[0]) != summary:
                raise LedgerProtocolConflict("An initialized budget cannot be replaced")
            return
        self._commit(summary.scope, [ConditionalEntity(SUMMARY_ROW, summary, None)])

    def reserve(
        self,
        identity: LedgerAttemptIdentity,
        reserved_tokens: int,
        *,
        epoch: UUID,
        attempts: int = 3,
    ) -> ReservedLedgerAttempt:
        for _ in range(max(1, min(attempts, 5))):
            current = self._read(identity.scope, SUMMARY_ROW)
            if current is None:
                raise LedgerUnavailable("Budget execution scope is not initialized")
            state = LedgerSummary.model_validate(current[0])
            if state.epoch != epoch or state.scope != identity.scope:
                raise LedgerProtocolConflict("Budget execution epoch does not match")
            previous = self._read(identity.scope, identity.row_key)
            if previous is not None:
                receipt = LedgerAttempt.model_validate(previous[0])
                if (
                    receipt.identity != identity
                    or receipt.epoch != state.epoch
                    or receipt.reserved_tokens != reserved_tokens
                ):
                    raise LedgerProtocolConflict("An attempt cannot change its reservation")
                return ReservedLedgerAttempt(state, receipt, False)
            updated, receipt = reserve_ledger(state, identity, reserved_tokens, datetime.now(UTC))
            try:
                self._commit(
                    identity.scope,
                    [
                        ConditionalEntity(SUMMARY_ROW, updated, current[1]),
                        ConditionalEntity(identity.row_key, receipt, None),
                    ],
                )
            except (LedgerCompareFailed, httpx.TransportError):
                continue
            return ReservedLedgerAttempt(updated, receipt, True)
        raise LedgerUnavailable("Ledger admission exhausted its bounded retries")

    def _update_attempt(
        self,
        identity: LedgerAttemptIdentity,
        transition: Callable[
            [LedgerSummary, LedgerAttempt, datetime], tuple[LedgerSummary, LedgerAttempt]
        ],
        *,
        attempts: int = 3,
    ) -> tuple[LedgerSummary, LedgerAttempt]:
        for _ in range(max(1, min(attempts, 5))):
            state_row = self._read(identity.scope, SUMMARY_ROW)
            attempt_row = self._read(identity.scope, identity.row_key)
            if state_row is None or attempt_row is None:
                raise LedgerUnavailable("Ledger request or summary is not available")
            state = LedgerSummary.model_validate(state_row[0])
            receipt = LedgerAttempt.model_validate(attempt_row[0])
            if (
                receipt.identity != identity
                or state.scope != identity.scope
                or receipt.epoch != state.epoch
            ):
                raise LedgerProtocolConflict("Ledger request identity does not match")
            updated, result = transition(state, receipt, datetime.now(UTC))
            if updated is state:
                return state, receipt
            try:
                self._commit(
                    identity.scope,
                    [
                        ConditionalEntity(SUMMARY_ROW, updated, state_row[1]),
                        ConditionalEntity(identity.row_key, result, attempt_row[1]),
                    ],
                )
            except (LedgerCompareFailed, httpx.TransportError):
                continue
            return updated, result
        raise LedgerUnavailable("Ledger settlement exhausted its bounded retries")

    def settle(self, charge: LedgerCharge) -> tuple[LedgerSummary, LedgerAttempt]:
        return self._update_attempt(
            charge.identity, lambda state, attempt, now: settle_ledger(state, attempt, charge, now)
        )

    def complete(
        self, receipt: LedgerAdmissionReceipt, completion: dict[str, Any]
    ) -> tuple[LedgerSummary, LedgerAttempt]:
        return self._update_attempt(
            receipt.identity,
            lambda state, attempt, now: complete_ledger(state, attempt, receipt, completion, now),
        )

    def read_attempt(self, receipt: LedgerAdmissionReceipt) -> LedgerAttempt:
        stored = self._read(receipt.identity.scope, receipt.identity.row_key)
        if stored is None:
            raise LedgerUnavailable("Execution admission receipt is unavailable")
        attempt = LedgerAttempt.model_validate(stored[0])
        if (
            attempt.identity != receipt.identity
            or attempt.epoch != receipt.epoch
            or attempt.reserved_tokens != receipt.reserved_tokens
        ):
            raise LedgerProtocolConflict("Stored attempt does not match its immutable admission")
        return attempt

    def transition(
        self,
        identity: LedgerAttemptIdentity,
        state: Literal["dispatching", "uncertain", "upper_bound", "cancelled"],
    ) -> tuple[LedgerSummary, LedgerAttempt]:
        if state == "cancelled":
            raise LedgerProtocolConflict("Cancellation requires a durable cancelled dispatch")
        return self._update_attempt(
            identity,
            lambda summary, receipt, now: transition_ledger_attempt(summary, receipt, state, now),
        )

    def write_policy(
        self,
        scope: LedgerScope,
        *,
        epoch: UUID,
        revision: int,
        token_limit: int,
        enforce: bool,
        enabled: bool,
    ) -> LedgerSummary:
        for _ in range(3):
            current = self._read(scope, SUMMARY_ROW)
            if current is None:
                raise LedgerUnavailable("Budget execution scope is not initialized")
            summary = LedgerSummary.model_validate(current[0])
            if summary.scope != scope:
                raise LedgerProtocolConflict("Budget policy belongs to another scope")
            updated = update_ledger_policy(
                summary,
                epoch=epoch,
                revision=revision,
                token_limit=token_limit,
                enforce=enforce,
                enabled=enabled,
                now=datetime.now(UTC),
            )
            if updated is summary:
                return summary
            try:
                self._commit(scope, [ConditionalEntity(SUMMARY_ROW, updated, current[1])])
            except (LedgerCompareFailed, httpx.TransportError):
                continue
            return updated
        raise LedgerUnavailable("Budget policy exhausted its bounded retries")

    def begin_dispatch(self, plan: LedgerDispatch) -> LedgerDispatch:
        if plan.state != "preparing" or plan.expires_at <= datetime.now(UTC):
            raise LedgerProtocolConflict("A new dispatch must have a live preparation lease")
        try:
            self._commit(plan.scope, [ConditionalEntity(plan.row_key, plan, None)])
        except (LedgerCompareFailed, httpx.TransportError):
            current = self._read(plan.scope, plan.row_key)
            if current is None or LedgerDispatch.model_validate(current[0]) != plan:
                raise LedgerUnavailable("Dispatch creation was not confirmed") from None
        return plan

    def reserve_dispatch(self, plan: LedgerDispatch) -> None:
        for admission in plan.receipts:
            current = self._read(plan.scope, plan.row_key)
            if current is None or LedgerDispatch.model_validate(current[0]) != plan:
                raise LedgerProtocolConflict("Dispatch preparation was cancelled or fenced")
            if plan.expires_at <= datetime.now(UTC):
                raise LedgerProtocolConflict("Dispatch preparation lease expired")
            result = self.reserve(
                admission.identity, admission.reserved_tokens, epoch=admission.epoch
            )
            if result.attempt.state != "reserved":
                raise LedgerProtocolConflict("An earlier attempt cannot authorize another dispatch")

    def authorize_dispatch(self, plan: LedgerDispatch) -> bool:
        current = self._read(plan.scope, plan.row_key)
        if current is None or LedgerDispatch.model_validate(current[0]) != plan:
            raise LedgerProtocolConflict("Only the current preparing owner can authorize dispatch")
        if plan.expires_at <= datetime.now(UTC):
            raise LedgerProtocolConflict("Dispatch preparation lease expired")
        for admission in plan.receipts:
            attempt = self.read_attempt(admission)
            if attempt.state != "reserved":
                raise LedgerProtocolConflict("Dispatch reservation does not match its plan")
        authorized = LedgerDispatch.model_validate(
            {
                **plan.model_dump(),
                "state": "dispatching",
                "updated_at": datetime.now(UTC),
            }
        )
        if authorized.updated_at >= plan.expires_at:
            raise LedgerProtocolConflict("Dispatch preparation expired before authorization")
        try:
            self._commit(plan.scope, [ConditionalEntity(plan.row_key, authorized, current[1])])
        except (LedgerCompareFailed, httpx.TransportError):
            return False
        return True

    def cancel_dispatch(self, plan: LedgerDispatch, *, recovery: bool = False) -> bool:
        for _ in range(3):
            current = self._read(plan.scope, plan.row_key)
            if current is None:
                raise LedgerUnavailable("Dispatch cancellation requires its durable plan")
            stored = LedgerDispatch.model_validate(current[0])
            if stored.owner != plan.owner or stored.receipts != plan.receipts:
                raise LedgerProtocolConflict("Dispatch cancellation belongs to another owner")
            if stored.state == "dispatching":
                return False
            if stored.state == "cancelled":
                return True
            if recovery and stored.expires_at > datetime.now(UTC):
                return False
            cancelled = LedgerDispatch.model_validate(
                {
                    **stored.model_dump(),
                    "state": "cancelled",
                    "updated_at": datetime.now(UTC),
                }
            )
            try:
                self._commit(plan.scope, [ConditionalEntity(plan.row_key, cancelled, current[1])])
            except (LedgerCompareFailed, httpx.TransportError):
                continue
            return True
        raise LedgerUnavailable("Dispatch cancellation exhausted its bounded retries")

    def compensate_dispatch(self, plan: LedgerDispatch) -> int:
        current = self._read(plan.scope, plan.row_key)
        if current is None:
            raise LedgerUnavailable("Dispatch compensation requires its durable plan")
        stored = LedgerDispatch.model_validate(current[0])
        if (
            stored.state != "cancelled"
            or stored.owner != plan.owner
            or stored.receipts != plan.receipts
        ):
            raise LedgerProtocolConflict("Unknown dispatch outcome cannot release reservations")
        released = 0
        for admission in stored.receipts:
            for retry in range(3):
                current_summary = self._read(admission.identity.scope, SUMMARY_ROW)
                existing = self._read(admission.identity.scope, admission.identity.row_key)
                if current_summary is None:
                    raise LedgerUnavailable("Dispatch compensation requires its budget summary")
                summary = LedgerSummary.model_validate(current_summary[0])
                if summary.epoch != admission.epoch or summary.scope != admission.identity.scope:
                    raise LedgerProtocolConflict("Dispatch compensation epoch does not match")
                if existing is not None:
                    attempt = self.read_attempt(admission)
                    if attempt.state not in {"settled", "cancelled"}:
                        self._update_attempt(
                            admission.identity,
                            lambda current, retained, moment: transition_ledger_attempt(
                                current, retained, "cancelled", moment
                            ),
                        )
                        released += 1
                    break
                now = datetime.now(UTC)
                tombstone = LedgerAttempt(
                    identity=admission.identity,
                    epoch=admission.epoch,
                    state="cancelled",
                    reserved_tokens=admission.reserved_tokens,
                    updated_at=now,
                )
                advanced = LedgerSummary.model_validate(
                    {
                        **summary.model_dump(),
                        "version": summary.version + 1,
                        "updated_at": now,
                    }
                )
                try:
                    self._commit(
                        admission.identity.scope,
                        [
                            ConditionalEntity(SUMMARY_ROW, advanced, current_summary[1]),
                            ConditionalEntity(admission.identity.row_key, tombstone, None),
                        ],
                    )
                except (LedgerCompareFailed, httpx.TransportError):
                    if retry == 2:
                        raise LedgerUnavailable(
                            "Cancellation tombstone was not confirmed"
                        ) from None
                    continue
                break
        return released

    def pending_dispatch_page(
        self,
        cursor: LedgerRecoveryCursor,
        *,
        limit: int = 16,
    ) -> tuple[list[LedgerDispatch], str | None, str | None]:
        partition = cursor.scope.partition.replace("'", "''")
        params = {
            "$filter": f"PartitionKey eq '{partition}' and RowKey ge 'D|' and RowKey lt 'E' "
            "and RecoveryPending eq true",
            "$top": str(max(1, min(limit, 32))),
            "$select": "RowKey,Document",
        }
        if cursor.next_partition is not None:
            params["NextPartitionKey"] = cursor.next_partition
        if cursor.next_row is not None:
            params["NextRowKey"] = cursor.next_row
        response = self._request("GET", f"{self._endpoint}/{self._table}()", params=params)
        response.raise_for_status()
        rows = response.json().get("value")
        if not isinstance(rows, list) or len(rows) > max(1, min(limit, 32)):
            raise LedgerUnavailable("Recovery page is malformed or exceeds its bounded size")
        plans = []
        for row in rows:
            plan = LedgerDispatch.model_validate_json(row["Document"])
            if (
                plan.scope != cursor.scope
                or plan.receipts[0].epoch != cursor.epoch
                or plan.row_key != row["RowKey"]
            ):
                raise LedgerProtocolConflict("Recovery row does not match its scope and epoch")
            plans.append(plan)
        next_partition = response.headers.get("x-ms-continuation-NextPartitionKey")
        next_row = response.headers.get("x-ms-continuation-NextRowKey")
        if (next_partition or next_row) and (next_partition, next_row) == (
            cursor.next_partition,
            cursor.next_row,
        ):
            raise LedgerUnavailable("Recovery continuation did not advance")
        return plans, next_partition, next_row

    def finish_dispatch_recovery(self, plan: LedgerDispatch) -> None:
        current = self._read(plan.scope, plan.row_key)
        if current is None:
            raise LedgerUnavailable("Recovered dispatch is missing")
        stored = LedgerDispatch.model_validate(current[0])
        if stored.owner != plan.owner or stored.receipts != plan.receipts:
            raise LedgerProtocolConflict("Recovered dispatch identity changed")
        if stored.state == "preparing":
            raise LedgerProtocolConflict("A preparing dispatch cannot leave recovery")
        if stored.recovery_complete:
            return
        updated = LedgerDispatch.model_validate(
            {
                **stored.model_dump(),
                "recovery_complete": True,
                "updated_at": datetime.now(UTC),
            }
        )
        self._commit(plan.scope, [ConditionalEntity(plan.row_key, updated, current[1])])

    def finalize_uncertain(self, identity: LedgerAttemptIdentity) -> None:
        self._update_attempt(
            identity,
            lambda summary, attempt, now: (
                (summary, attempt)
                if attempt.state in {"settled", "cancelled", "upper_bound"}
                else transition_ledger_attempt(summary, attempt, "upper_bound", now)
            ),
        )
