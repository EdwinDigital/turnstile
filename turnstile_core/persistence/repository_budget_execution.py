from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..domain.application_access import UsageApplicationAttribution
from ..domain.budget_completion import validate_completion
from ..domain.ledger import (
    LedgerAdmissionReceipt,
    LedgerAttemptIdentity,
    LedgerDispatch,
    LedgerPolicyUpdate,
    LedgerRecoveryCursor,
    LedgerScope,
    LedgerScopeSeed,
    LedgerSettlementTask,
)
from ..domain.models import TokenUsageRecord

_ADMISSION_UPSERT = """
INSERT INTO budget_execution_attempt (
    scope_type, scope_id, period_start, attempt_key, epoch, gateway_profile_id,
    native_gateway_id, correlation_id, model_id, admitted_at, reserved_tokens,
    identity, usage_id, usage_complete, caller_request_id
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (scope_type, scope_id, period_start, attempt_key) DO UPDATE SET
    usage_id = EXCLUDED.usage_id,
    usage_complete = budget_execution_attempt.usage_complete OR EXCLUDED.usage_complete
WHERE budget_execution_attempt.identity = EXCLUDED.identity
    AND budget_execution_attempt.epoch = EXCLUDED.epoch
    AND budget_execution_attempt.reserved_tokens = EXCLUDED.reserved_tokens
    AND budget_execution_attempt.caller_request_id = EXCLUDED.caller_request_id
RETURNING attempt_key
"""


class PostgreSqlBudgetExecutionMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def active_budget_execution_rollout(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT epoch, gateway_profile_ids, admitted_after, prepared_period
                   FROM budget_execution_rollout WHERE state = 'active' AND scope_sync_enabled"""
            ).fetchone()
        return dict(row) if row is not None else None

    def prepare_budget_execution_period(
        self,
        epoch: UUID,
        period: date,
        known_people: Sequence[str],
    ) -> None:
        with self._connection() as connection, connection.transaction():
            rollout = connection.execute(
                """SELECT prepared_period FROM budget_execution_rollout
                   WHERE epoch = %s AND state = 'active' AND scope_sync_enabled FOR UPDATE""",
                (epoch,),
            ).fetchone()
            if rollout is None or rollout["prepared_period"] == period:
                return
            connection.execute(
                """SELECT prepare_budget_execution_scope(kind, identity, %s::date) FROM (
                   SELECT 'person' AS kind, unnest(%s::text[]) AS identity
                   UNION SELECT 'person', scope_id FROM token_budget
                     WHERE scope_type = 'user' AND period_start = %s
                   UNION SELECT 'person', user_id FROM user_model_policy
                   UNION SELECT 'person', email FROM app_user
                   UNION SELECT 'person', user_id FROM token_usage
                     WHERE usage_domain = 'apim' AND user_id LIKE '%%@%%'
                       AND ts >= %s::date - interval '1 month'
                       AND ts < %s::date + interval '1 month'
                   UNION SELECT 'application', id::text FROM gateway_application
                     WHERE NOT system_managed AND status <> 'retired'
                   ) scopes ORDER BY kind, identity""",
                (period, list(known_people), period, period, period),
            )
            connection.execute(
                "UPDATE budget_execution_rollout SET prepared_period = %s WHERE epoch = %s",
                (period, epoch),
            )

    def budget_scope_seed_candidates(
        self,
        *,
        limit: int = 16,
        application_id: UUID | None = None,
    ) -> list[LedgerScopeSeed]:
        with self._connection() as connection, connection.transaction():
            if application_id is not None:
                connection.execute(
                    """SELECT prepare_budget_execution_scope('application', %s,
                       date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date)""",
                    (str(application_id),),
                )
            rows = connection.execute(
                """SELECT scope.scope_type, scope.scope_id, scope.period_start, scope.epoch,
                   scope.state, scope.seed_confirmed_tokens, scope.seed_checked_at,
                   scope.policy_revision, scope.desired_limit, scope.desired_enforce,
                   scope.desired_enabled FROM budget_execution_scope scope
                   JOIN budget_execution_rollout rollout USING (epoch)
                   WHERE rollout.state = 'active' AND rollout.scope_sync_enabled
                     AND (scope.state = 'prepared' OR %s::uuid IS NOT NULL)
                     AND scope.period_start =
                       date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date
                     AND (%s::uuid IS NULL OR (
                       scope.scope_type = 'application' AND scope.scope_id = %s))
                   ORDER BY scope.recovery_checked_at, scope.created_at,
                     scope.scope_type, scope.scope_id
                   LIMIT %s FOR UPDATE OF scope SKIP LOCKED""",
                (application_id, application_id, str(application_id), max(1, min(limit, 32))),
            ).fetchall()
            seeds = []
            for row in rows:
                if row["seed_confirmed_tokens"] is None:
                    if row["state"] != "prepared":
                        continue
                    selected = connection.execute(
                        """UPDATE budget_execution_scope SET seed_confirmed_tokens =
                           budget_scope_confirmed_tokens(scope_type, scope_id, period_start,
                             (period_start + interval '1 month')::date),
                           seed_checked_at = clock_timestamp()
                           WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                             AND epoch = %s AND state = 'prepared'
                             AND seed_confirmed_tokens IS NULL
                           RETURNING seed_confirmed_tokens, seed_checked_at""",
                        (row["scope_type"], row["scope_id"], row["period_start"], row["epoch"]),
                    ).fetchone()
                    row = {**row, **selected}
                scope = {name: row[name] for name in ("scope_type", "scope_id", "period_start")}
                seeds.append(
                    LedgerScopeSeed.model_validate(
                        {
                            "summary": {
                                "epoch": row["epoch"],
                                "scope": scope,
                                "token_limit": 0,
                                "enforce": True,
                                "enabled": False,
                                "confirmed_tokens": row["seed_confirmed_tokens"],
                                "updated_at": row["seed_checked_at"],
                            },
                            "policy": {
                                "scope": scope,
                                "epoch": row["epoch"],
                                "revision": row["policy_revision"],
                                "token_limit": row["desired_limit"],
                                "enforce": row["desired_enforce"],
                                "enabled": row["desired_enabled"],
                            },
                            "active": row["state"] == "active",
                        }
                    )
                )
        return seeds

    def activate_budget_scope_seed(self, seed: LedgerScopeSeed) -> None:
        scope = seed.summary.scope
        with self._connection() as connection:
            result = connection.execute(
                """UPDATE budget_execution_scope scope SET state = 'active'
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND epoch = %s AND state IN ('prepared', 'active')
                     AND seed_confirmed_tokens = %s AND seed_checked_at = %s
                     AND EXISTS (SELECT 1 FROM budget_execution_rollout rollout
                       WHERE rollout.epoch = scope.epoch AND rollout.state = 'active'
                         AND rollout.scope_sync_enabled)
                   RETURNING epoch""",
                (
                    scope.scope_type,
                    scope.scope_id,
                    scope.period_start,
                    seed.summary.epoch,
                    seed.summary.confirmed_tokens,
                    seed.summary.updated_at,
                ),
            ).fetchone()
        if result is None:
            raise ValueError("Execution scope activation was fenced or its opening balance changed")

    def defer_budget_scope_seed(self, scope: LedgerScope) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE budget_execution_scope SET recovery_checked_at = clock_timestamp()
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND state = 'prepared'""",
                (scope.scope_type, scope.scope_id, scope.period_start),
            )

    @staticmethod
    def _write_execution_receipts(
        connection: Any,
        record: TokenUsageRecord,
        usage_id: str,
        application: UsageApplicationAttribution | None,
        gateway_profile_id: UUID | None,
        native_gateway_id: str | None,
        receipts: Sequence[LedgerAdmissionReceipt],
        usage_complete: bool,
        evidence_source: str,
    ) -> None:
        if receipts:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("execution-ack:" + (record.request_id or ""),),
            )
        seen = set()
        for receipt in receipts:
            identity, scope = receipt.identity, receipt.identity.scope
            key = (scope.scope_type, scope.scope_id, scope.period_start)
            expected_scope = (
                record.user_id
                if scope.scope_type == "person"
                else str(application.application_id)
                if application
                else None
            )
            if (
                key in seen
                or scope.scope_id != expected_scope
                or record.usage_domain != "apim"
                or identity.model_id != record.model_id
                or identity.correlation_id != record.correlation_id
                or identity.gateway_profile_id != gateway_profile_id
                or identity.native_gateway_id != native_gateway_id
                or evidence_source not in {"apim", "observer"}
            ):
                raise ValueError(
                    "Budget execution receipt does not match trusted usage attribution"
                )
            seen.add(key)
            state = connection.execute(
                """SELECT epoch, state, admitted_after FROM budget_execution_scope
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s FOR SHARE""",
                key,
            ).fetchone()
            if (
                state is None
                or state["epoch"] != receipt.epoch
                or state["state"] not in {"active", "draining"}
                or identity.admitted_at < state["admitted_after"]
            ):
                raise ValueError("Budget execution scope is not active for this admission")
            values = (*key, identity.attempt_key)
            prior = connection.execute(
                """SELECT identity, epoch, reserved_tokens FROM budget_execution_attempt
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND attempt_key = %s FOR UPDATE""",
                values,
            ).fetchone()
            identity_json = identity.model_dump(mode="json")
            if prior is not None and (
                prior["identity"] != identity_json
                or prior["epoch"] != receipt.epoch
                or prior["reserved_tokens"] != receipt.reserved_tokens
            ):
                raise ValueError("Budget execution admission is immutable")
            inserted = connection.execute(
                _ADMISSION_UPSERT,
                (
                    *values,
                    receipt.epoch,
                    identity.gateway_profile_id,
                    identity.native_gateway_id,
                    identity.correlation_id,
                    identity.model_id,
                    identity.admitted_at,
                    receipt.reserved_tokens,
                    Jsonb(identity_json),
                    usage_id,
                    usage_complete,
                    record.request_id,
                ),
            ).fetchone()
            if inserted is None:
                raise ValueError("Concurrent admission conflicts with immutable execution receipt")
            if usage_complete and not record.estimated and record.ingest_error is None:
                connection.execute(
                    """SELECT queue_budget_execution_evidence(execution, %s, 3::smallint, %s, %s)
                       FROM budget_execution_attempt execution
                       WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                         AND attempt_key = %s""",
                    (
                        f"{evidence_source}:{record.id}",
                        record.input_tokens + record.cached_tokens + record.output_tokens,
                        record.ts,
                        *values,
                    ),
                )
            connection.execute(
                """SELECT enqueue_budget_execution_ack(execution)
                   FROM budget_execution_attempt execution
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND attempt_key = %s""",
                values,
            )

    def budget_execution_status(self, scope: LedgerScope) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT epoch, state, policy_revision, policy_applied_revision
                   FROM budget_execution_scope WHERE scope_type = %s AND scope_id = %s
                   AND period_start = %s""",
                (scope.scope_type, scope.scope_id, scope.period_start),
            ).fetchone()
        return dict(row) if row is not None else None

    def budget_recovery_scopes(self, limit: int = 8) -> list[LedgerRecoveryCursor]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT scope_type, scope_id, period_start, epoch, recovery_next_partition,
                   recovery_next_row, recovery_version FROM budget_execution_scope
                   WHERE state IN ('active', 'draining') ORDER BY recovery_checked_at, scope_type,
                   scope_id, period_start LIMIT %s""",
                (max(1, min(limit, 32)),),
            ).fetchall()
        return [
            LedgerRecoveryCursor.model_validate(
                {
                    "scope": {
                        name: row[name] for name in ("scope_type", "scope_id", "period_start")
                    },
                    "epoch": row["epoch"],
                    "next_partition": row["recovery_next_partition"],
                    "next_row": row["recovery_next_row"],
                    "version": row["recovery_version"],
                }
            )
            for row in rows
        ]

    def advance_budget_recovery(
        self, cursor: LedgerRecoveryCursor, next_partition: str | None, next_row: str | None
    ) -> bool:
        scope = cursor.scope
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE budget_execution_scope SET recovery_next_partition = %s,
                   recovery_next_row = %s, recovery_version = recovery_version + 1,
                   recovery_checked_at = clock_timestamp()
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND epoch = %s AND recovery_version = %s RETURNING epoch""",
                (
                    next_partition,
                    next_row,
                    scope.scope_type,
                    scope.scope_id,
                    scope.period_start,
                    cursor.epoch,
                    cursor.version,
                ),
            ).fetchone()
        return row is not None

    def register_budget_dispatch(self, dispatch: LedgerDispatch) -> None:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("execution-ack:" + dispatch.caller_request_id,),
            )
            for receipt in dispatch.receipts:
                identity, scope = receipt.identity, receipt.identity.scope
                key = (scope.scope_type, scope.scope_id, scope.period_start)
                state = connection.execute(
                    """SELECT epoch, state, admitted_after FROM budget_execution_scope
                       WHERE scope_type = %s AND scope_id = %s AND period_start = %s FOR SHARE""",
                    key,
                ).fetchone()
                if (
                    state is None
                    or state["epoch"] != receipt.epoch
                    or state["state"] not in {"active", "draining"}
                    or identity.admitted_at < state["admitted_after"]
                ):
                    raise ValueError(
                        "Recovered dispatch does not belong to an active execution epoch"
                    )
                connection.execute(
                    """INSERT INTO budget_execution_attempt (
                       scope_type, scope_id, period_start, epoch, attempt_key,
                       gateway_profile_id, native_gateway_id, correlation_id, caller_request_id,
                       model_id, admitted_at, reserved_tokens, identity
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (scope_type, scope_id, period_start, attempt_key) DO NOTHING""",
                    (
                        *key,
                        receipt.epoch,
                        identity.attempt_key,
                        identity.gateway_profile_id,
                        identity.native_gateway_id,
                        identity.correlation_id,
                        dispatch.caller_request_id,
                        identity.model_id,
                        identity.admitted_at,
                        receipt.reserved_tokens,
                        Jsonb(identity.model_dump(mode="json")),
                    ),
                )
                prior = connection.execute(
                    """SELECT identity, epoch, reserved_tokens, caller_request_id
                       FROM budget_execution_attempt WHERE scope_type = %s AND scope_id = %s
                       AND period_start = %s AND attempt_key = %s FOR UPDATE""",
                    (*key, identity.attempt_key),
                ).fetchone()
                if (
                    prior["identity"] != identity.model_dump(mode="json")
                    or prior["epoch"] != receipt.epoch
                    or prior["reserved_tokens"] != receipt.reserved_tokens
                    or prior["caller_request_id"] != dispatch.caller_request_id
                ):
                    raise ValueError("Recovered dispatch conflicts with immutable admission")
            if dispatch.state == "dispatching":
                members = Jsonb([receipt.model_dump(mode="json") for receipt in dispatch.receipts])
                connection.execute(
                    """INSERT INTO budget_execution_dispatch (id, caller_request_id, receipts)
                       VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (dispatch.owner, dispatch.caller_request_id, members),
                )
                group = connection.execute(
                    """SELECT id FROM budget_execution_dispatch WHERE id = %s
                       AND caller_request_id = %s AND receipts = %s""",
                    (dispatch.owner, dispatch.caller_request_id, members),
                ).fetchone()
                if group is None:
                    raise ValueError("Recovered dispatch conflicts with immutable scope membership")
                for receipt in dispatch.receipts:
                    identity, scope = receipt.identity, receipt.identity.scope
                    linked = connection.execute(
                        """UPDATE budget_execution_attempt SET dispatch_id = %s
                           WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                             AND attempt_key = %s AND epoch = %s AND identity = %s
                             AND caller_request_id = %s AND reserved_tokens = %s
                             AND (dispatch_id IS NULL OR dispatch_id = %s) RETURNING attempt_key""",
                        (
                            dispatch.owner,
                            scope.scope_type,
                            scope.scope_id,
                            scope.period_start,
                            identity.attempt_key,
                            receipt.epoch,
                            Jsonb(identity.model_dump(mode="json")),
                            dispatch.caller_request_id,
                            receipt.reserved_tokens,
                            dispatch.owner,
                        ),
                    ).fetchone()
                    if linked is None:
                        raise ValueError("Execution already belongs to another immutable dispatch")
            for receipt in dispatch.receipts:
                identity, scope = receipt.identity, receipt.identity.scope
                connection.execute(
                    """SELECT enqueue_budget_execution_ack(execution)
                       FROM budget_execution_attempt execution WHERE scope_type = %s
                       AND scope_id = %s AND period_start = %s AND attempt_key = %s""",
                    (scope.scope_type, scope.scope_id, scope.period_start, identity.attempt_key),
                )

    def pending_budget_completions(self, limit: int = 8) -> list[LedgerAdmissionReceipt]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT execution.identity, execution.epoch, execution.reserved_tokens
                   FROM budget_execution_attempt execution LEFT JOIN budget_settlement_task task
                   USING (scope_type, scope_id, period_start, attempt_key)
                   WHERE task.id IS NULL OR task.evidence_rank < 3
                   ORDER BY execution.completion_checked_at, execution.created_at LIMIT %s""",
                (max(1, min(limit, 32)),),
            ).fetchall()
        return [LedgerAdmissionReceipt.model_validate(row) for row in rows]

    def record_budget_completion(
        self, receipt: LedgerAdmissionReceipt, completion: dict[str, Any] | None
    ) -> None:
        identity, scope = receipt.identity, receipt.identity.scope
        key = (scope.scope_type, scope.scope_id, scope.period_start, identity.attempt_key)
        evidence = validate_completion(completion) if completion is not None else None
        with self._connection() as connection, connection.transaction():
            current = connection.execute(
                """SELECT identity, epoch, reserved_tokens FROM budget_execution_attempt
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND attempt_key = %s FOR UPDATE""",
                key,
            ).fetchone()
            if (
                current is None
                or current["identity"] != identity.model_dump(mode="json")
                or current["epoch"] != receipt.epoch
                or current["reserved_tokens"] != receipt.reserved_tokens
            ):
                raise ValueError("Recovered completion does not match its registered admission")
            if evidence is not None:
                connection.execute(
                    """SELECT queue_budget_execution_evidence(execution, %s, 3::smallint, %s, %s)
                       FROM budget_execution_attempt execution WHERE scope_type = %s
                       AND scope_id = %s AND period_start = %s AND attempt_key = %s""",
                    (
                        "completion:" + identity.attempt_key,
                        evidence["total_tokens"],
                        evidence["observed_at"],
                        *key,
                    ),
                )
            connection.execute(
                """UPDATE budget_execution_attempt SET completion_checked_at = clock_timestamp()
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND attempt_key = %s""",
                key,
            )

    def pending_budget_policies(self, limit: int = 16) -> list[LedgerPolicyUpdate]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT scope_type, scope_id, period_start, epoch, policy_revision,
                   desired_limit, desired_enforce, desired_enabled FROM budget_execution_scope
                                     WHERE state IN ('active', 'draining')
                                         AND policy_applied_revision < policy_revision
                   ORDER BY policy_updated_at, scope_type, scope_id, period_start LIMIT %s""",
                (max(1, min(limit, 32)),),
            ).fetchall()
        return [
            LedgerPolicyUpdate.model_validate(
                {
                    "scope": {
                        name: row[name] for name in ("scope_type", "scope_id", "period_start")
                    },
                    "epoch": row["epoch"],
                    "revision": row["policy_revision"],
                    "token_limit": row["desired_limit"],
                    "enforce": row["desired_enforce"],
                    "enabled": row["desired_enabled"],
                }
            )
            for row in rows
        ]

    def complete_budget_policy(self, update: LedgerPolicyUpdate) -> None:
        scope = update.scope
        with self._connection() as connection:
            connection.execute(
                """UPDATE budget_execution_scope SET policy_applied_revision =
                   GREATEST(policy_applied_revision, %s)
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND epoch = %s AND policy_revision >= %s""",
                (
                    update.revision,
                    scope.scope_type,
                    scope.scope_id,
                    scope.period_start,
                    update.epoch,
                    update.revision,
                ),
            )

    def unverified_execution_attempts(self, limit: int = 16) -> list[LedgerAttemptIdentity]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT execution.identity FROM budget_execution_attempt execution
                   LEFT JOIN budget_settlement_task task
                     USING (scope_type, scope_id, period_start, attempt_key)
                   WHERE execution.uncertain_finalized_at IS NULL
                     AND execution.admitted_at < clock_timestamp() - interval '24 hours'
                     AND task.id IS NULL ORDER BY execution.admitted_at LIMIT %s""",
                (max(1, min(limit, 32)),),
            ).fetchall()
        return [LedgerAttemptIdentity.model_validate(row["identity"]) for row in rows]

    def complete_execution_finalization(self, identity: LedgerAttemptIdentity) -> None:
        scope = identity.scope
        with self._connection() as connection:
            connection.execute(
                """UPDATE budget_execution_attempt SET uncertain_finalized_at = clock_timestamp()
                   WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                     AND attempt_key = %s AND uncertain_finalized_at IS NULL""",
                (scope.scope_type, scope.scope_id, scope.period_start, identity.attempt_key),
            )

    def claim_budget_settlement(
        self, *, usage_id: str | None = None, lease_seconds: int = 30
    ) -> LedgerSettlementTask | None:
        token = uuid4()
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """SELECT task.*, attempt.identity, attempt.epoch
                   FROM budget_settlement_task task JOIN budget_execution_attempt attempt
                     USING (scope_type, scope_id, period_start, attempt_key)
                   WHERE task.applied_revision < task.revision
                     AND task.available_at <= clock_timestamp()
                     AND (task.lease_until IS NULL OR task.lease_until < clock_timestamp())
                     AND (%s::text IS NULL OR attempt.usage_id = %s)
                   ORDER BY task.available_at, task.id
                   LIMIT 1 FOR UPDATE OF task SKIP LOCKED""",
                (usage_id, usage_id),
            ).fetchone()
            if row is None:
                return None
            lease = connection.execute(
                """UPDATE budget_settlement_task SET lease_token = %s,
                   lease_until = clock_timestamp() + (%s * interval '1 second'),
                   attempts = attempts + 1, updated_at = clock_timestamp()
                   WHERE id = %s RETURNING lease_until, attempts""",
                (token, max(5, min(lease_seconds, 120)), row["id"]),
            ).fetchone()
            return LedgerSettlementTask.model_validate(
                {
                    "id": row["id"],
                    "lease_token": token,
                    **lease,
                    "created_at": row["created_at"],
                    "charge": {
                        "identity": row["identity"],
                        "epoch": row["epoch"],
                        "revision": row["revision"],
                        "evidence_key": row["evidence_key"],
                        "total_tokens": row["total_tokens"],
                        "evidence_rank": row["evidence_rank"],
                    },
                }
            )

    def complete_budget_settlement(self, task: LedgerSettlementTask) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE budget_settlement_task SET
                         applied_revision = GREATEST(applied_revision, %s),
                   applied_at = clock_timestamp(), lease_token = NULL, lease_until = NULL,
                   last_error = NULL, updated_at = clock_timestamp()
                   WHERE id = %s AND lease_token = %s AND lease_until > clock_timestamp()
                     AND revision >= %s RETURNING id""",
                (task.charge.revision, task.id, task.lease_token, task.charge.revision),
            ).fetchone()
        return row is not None

    def retry_budget_settlement(self, task: LedgerSettlementTask, error_code: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE budget_settlement_task SET lease_token = NULL, lease_until = NULL,
                   available_at = clock_timestamp() + (%s * interval '1 second'),
                   last_error = %s, updated_at = clock_timestamp()
                   WHERE id = %s AND lease_token = %s AND lease_until > clock_timestamp()
                   RETURNING id""",
                (min(60, 2 ** min(task.attempts, 6)), error_code[:128], task.id, task.lease_token),
            ).fetchone()
        return row is not None

    def budget_settlement_health(self) -> dict[str, int | float]:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT count(*) AS pending,
                   COALESCE(extract(epoch FROM
                       clock_timestamp() - min(created_at)), 0) AS oldest_seconds,
                   count(*) FILTER (WHERE attempts >= 5) AS repeatedly_failed
                   FROM budget_settlement_task WHERE applied_revision < revision""",
            ).fetchone()
        return {
            "pending": int(row["pending"]),
            "oldest_seconds": float(row["oldest_seconds"]),
            "repeatedly_failed": int(row["repeatedly_failed"]),
        }
