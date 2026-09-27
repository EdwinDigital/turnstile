from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from ..domain.model_availability import (
    MAX_AVAILABILITY_REVISION,
    ModelAvailabilityCommand,
    ModelAvailabilityConflict,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeEnrollment,
    ModelAvailabilityOperation,
    ModelAvailabilityReadback,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
)

_STATE_SQL = (
    "SELECT identity.model_id, identity.revision, identity.lifecycle, "
    "CASE identity.lifecycle WHEN 'active' THEN model.enabled "
    "WHEN 'staged' THEN TRUE ELSE FALSE END AS enabled, "
    "identity.effective_revision, identity.effective_enabled, identity.verified_at "
    "FROM model_availability_identity identity "
    "LEFT JOIN managed_model model ON model.id = identity.model_id"
)


def _state(row: Mapping[str, Any]) -> ModelAvailabilityState:
    return ModelAvailabilityState.model_validate(
        {key: row[key] for key in ModelAvailabilityState.model_fields}
    )


def _operation(row: Mapping[str, Any]) -> ModelAvailabilityOperation:
    return ModelAvailabilityOperation.model_validate(
        {key: row[key] for key in ModelAvailabilityOperation.model_fields}
    )


def _coverage(connection: Any) -> tuple[dict[UUID, str], dict[UUID, ModelAvailabilityCoverage]]:
    inventory = {
        row["target_id"]: row["inventory_digest"]
        for row in connection.execute(
            "SELECT * FROM model_availability_inventory()",
        ).fetchall()
    }
    capabilities = {
        row["target_id"]: ModelAvailabilityCoverage.model_validate(row["coverage"])
        for row in connection.execute("SELECT * FROM model_availability_capability").fetchall()
    }
    return inventory, capabilities


def _ready(
    inventory: Mapping[UUID, str],
    capabilities: Mapping[UUID, ModelAvailabilityCoverage],
    now: datetime,
) -> bool:
    return bool(inventory) and all(
        target_id in capabilities
        and capabilities[target_id].target_id == target_id
        and capabilities[target_id].current(now, digest)
        for target_id, digest in inventory.items()
    )


class PostgreSqlModelAvailabilityRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def model_availability_supported(self) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT to_regclass('model_availability_identity') IS NOT NULL AS available",
            ).fetchone()
        return bool(row and row["available"])

    def get_model_availability_state(self, model_id: UUID) -> ModelAvailabilityState | None:
        with self._connection() as connection:
            row = connection.execute(
                _STATE_SQL + " WHERE identity.model_id = %s", (model_id,)
            ).fetchone()
        return _state(row) if row else None

    def get_model_availability_states(
        self, model_ids: Sequence[UUID]
    ) -> dict[UUID, ModelAvailabilityState]:
        if not model_ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                _STATE_SQL + " WHERE identity.model_id = ANY(%s)", (list(model_ids),)
            ).fetchall()
        return {row["model_id"]: _state(row) for row in rows}

    def get_model_availability_inventory(self) -> dict[UUID, str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM model_availability_inventory()").fetchall()
        return {row["target_id"]: row["inventory_digest"] for row in rows}

    def get_model_availability_coverage(self, target_id: UUID) -> ModelAvailabilityCoverage | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT coverage FROM model_availability_capability WHERE target_id = %s",
                (target_id,),
            ).fetchone()
        return ModelAvailabilityCoverage.model_validate(row["coverage"]) if row else None

    def save_model_availability_coverage(self, coverage: ModelAvailabilityCoverage) -> bool:
        with self._connection() as connection, connection.transaction():
            inventory = connection.execute(
                "SELECT inventory_digest FROM model_availability_inventory() WHERE target_id = %s",
                (coverage.target_id,),
            ).fetchone()
            if not inventory or not coverage.current(
                datetime.now(UTC), inventory["inventory_digest"]
            ):
                return False
            row = connection.execute(
                "INSERT INTO model_availability_capability AS existing (target_id, coverage) "
                "VALUES (%s, %s) ON CONFLICT (target_id) DO UPDATE SET "
                "coverage = EXCLUDED.coverage, updated_at = now() "
                "WHERE (existing.coverage->>'verified_at')::TIMESTAMPTZ "
                "<= (EXCLUDED.coverage->>'verified_at')::TIMESTAMPTZ RETURNING target_id",
                (coverage.target_id, Jsonb(coverage.model_dump(mode="json"))),
            ).fetchone()
        return row is not None

    def list_model_availability_node_enrollments(
        self, target_id: UUID
    ) -> list[ModelAvailabilityNodeEnrollment]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT ON (deployment_id) enrollment_id, target_id, deployment_id, "
                "revision, enrollment FROM model_availability_node_enrollment "
                "WHERE target_id = %s ORDER BY deployment_id, revision DESC",
                (target_id,),
            ).fetchall()
        results = []
        for row in rows:
            enrollment = ModelAvailabilityNodeEnrollment.model_validate(row["enrollment"])
            if enrollment.target_id != target_id or any(
                row[key] != getattr(enrollment, key)
                for key in ("enrollment_id", "target_id", "deployment_id", "revision")
            ):
                raise ModelAvailabilityConflict("Node enrollment identity is inconsistent")
            results.append(enrollment)
        return results

    def save_model_availability_node_enrollment(
        self,
        enrollment: ModelAvailabilityNodeEnrollment,
        *,
        expected_revision: int,
        expected_inventory_digest: str,
        actor: str,
    ) -> bool:
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or enrollment.revision != expected_revision + 1
            or not 1 <= len(actor.strip()) <= 255
            or not 0 <= (datetime.now(UTC) - enrollment.enrolled_at).total_seconds() <= 300
        ):
            raise ModelAvailabilityConflict("Node enrollment approval is stale or invalid")
        with self._connection() as connection, connection.transaction():
            deployment = connection.execute(
                "SELECT deployment.apim_target_id, deployment.native_gateway_id, "
                "deployment.kind, target.api_id FROM gateway_deployment deployment "
                "JOIN apim_target_metadata target "
                "ON target.gateway_profile_id = deployment.apim_target_id "
                "WHERE deployment.id = %s FOR UPDATE OF deployment",
                (enrollment.deployment_id,),
            ).fetchone()
            if not deployment or (
                deployment["apim_target_id"] != enrollment.target_id
                or deployment["native_gateway_id"] != enrollment.native_gateway_id
                or deployment["api_id"] != enrollment.api_id
                or deployment["kind"] != "self_hosted"
            ):
                raise ModelAvailabilityConflict("Node enrollment deployment identity changed")
            previous = connection.execute(
                "SELECT enrollment, created_by FROM model_availability_node_enrollment "
                "WHERE deployment_id = %s ORDER BY revision DESC LIMIT 1",
                (enrollment.deployment_id,),
            ).fetchone()
            prior = (
                ModelAvailabilityNodeEnrollment.model_validate(previous["enrollment"])
                if previous
                else None
            )
            if prior == enrollment and previous and previous["created_by"] == actor:
                return False
            if (prior.revision if prior else 0) != expected_revision:
                raise ModelAvailabilityConflict("Node enrollment revision changed")
            inventory = connection.execute(
                "SELECT inventory_digest FROM model_availability_inventory() WHERE target_id = %s",
                (enrollment.target_id,),
            ).fetchone()
            if not inventory or inventory["inventory_digest"] != expected_inventory_digest:
                raise ModelAvailabilityConflict("Node enrollment target inventory changed")
            connection.execute(
                "INSERT INTO model_availability_node_enrollment "
                "(enrollment_id, target_id, deployment_id, revision, enrollment, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    enrollment.enrollment_id,
                    enrollment.target_id,
                    enrollment.deployment_id,
                    enrollment.revision,
                    Jsonb(enrollment.model_dump(mode="json")),
                    actor,
                ),
            )
        return True

    def list_model_availability(self) -> list[ModelAvailabilityReadback]:
        with self._connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            states = connection.execute(
                _STATE_SQL + " WHERE identity.lifecycle = 'active'"
            ).fetchall()
            operations = {
                row["model_id"]: _operation(row)
                for row in connection.execute(
                    "SELECT DISTINCT ON (model_id) * FROM model_availability_operation "
                    "ORDER BY model_id, desired_revision DESC",
                ).fetchall()
            }
            inventory, capabilities = _coverage(connection)
        ready = _ready(inventory, capabilities, datetime.now(UTC))
        items = []
        for row in states:
            current, operation = _state(row), operations.get(row["model_id"])
            verified = row["effective_revision"] == current.revision and ready
            items.append(
                ModelAvailabilityReadback(
                    model_id=current.model_id,
                    desired_enabled=current.enabled,
                    desired_revision=current.revision,
                    effective_enabled=row["effective_enabled"],
                    effective_revision=row["effective_revision"],
                    verified_at=row["verified_at"],
                    status="succeeded"
                    if verified
                    else (
                        operation.status
                        if operation and operation.status != "succeeded"
                        else "unknown"
                    ),
                    command_ready=ready
                    and (operation is None or operation.status in {"succeeded", "failed"}),
                    reason=None if ready else "model_availability_capability_unverified",
                    operation=operation,
                )
            )
        return items

    def get_model_availability_operation(
        self, operation_id: UUID
    ) -> ModelAvailabilityOperation | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_availability_operation WHERE id = %s", (operation_id,)
            ).fetchone()
        return _operation(row) if row else None

    def change_model_availability(
        self,
        model_id: UUID,
        command: ModelAvailabilityCommand,
        actor: str,
    ) -> ModelAvailabilityOperation:
        try:
            with self._connection() as connection, connection.transaction():
                model = connection.execute(
                    "SELECT id, enabled FROM managed_model WHERE id = %s FOR UPDATE", (model_id,)
                ).fetchone()
                previous = connection.execute(
                    "SELECT * FROM model_availability_operation WHERE idempotency_key = %s",
                    (command.idempotency_key,),
                ).fetchone()
                if previous:
                    operation = _operation(previous)
                    if (
                        operation.model_id != model_id
                        or operation.created_by != actor
                        or operation.expected_revision != command.expected_revision
                        or operation.desired_enabled != command.enabled
                    ):
                        raise ModelAvailabilityConflict("Availability idempotency key was reused")
                    return operation
                identity = connection.execute(
                    "SELECT * FROM model_availability_identity WHERE model_id = %s FOR UPDATE",
                    (model_id,),
                ).fetchone()
                if not model or not identity or identity["lifecycle"] != "active":
                    raise ModelAvailabilityConflict("Model availability identity is not active")
                if identity["revision"] != command.expected_revision:
                    raise ModelAvailabilityConflict("Model availability revision changed")
                if command.expected_revision == MAX_AVAILABILITY_REVISION:
                    raise ModelAvailabilityConflict("Model availability revision limit reached")
                pending = connection.execute(
                    "SELECT id FROM model_availability_operation WHERE model_id = %s "
                    "AND status NOT IN ('succeeded', 'failed') LIMIT 1",
                    (model_id,),
                ).fetchone()
                removing = connection.execute(
                    "SELECT id FROM gateway_publication WHERE publication_kind = 'model_remove' "
                    "AND status NOT IN ('active', 'superseded', 'failed', 'rolled_back') "
                    "AND desired_spec #>> '{removed_models,0,model_id}' = %s LIMIT 1",
                    (str(model_id),),
                ).fetchone()
                if pending or removing:
                    raise ModelAvailabilityConflict(
                        "A model lifecycle operation is still in progress"
                    )
                inventory, capabilities = _coverage(connection)
                now = datetime.now(UTC)
                if not _ready(inventory, capabilities, now):
                    raise ModelAvailabilityConflict("Model availability capability is not verified")
                operation = ModelAvailabilityOperation(
                    id=uuid4(),
                    model_id=model_id,
                    idempotency_key=command.idempotency_key,
                    expected_revision=command.expected_revision,
                    desired_revision=command.expected_revision + 1,
                    desired_enabled=command.enabled,
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                    targets=tuple(
                        ModelAvailabilityTargetResult(
                            target_id=target_id,
                            inventory_digest=digest,
                            policy_digest=capabilities[target_id].policy_digest,
                        )
                        for target_id, digest in sorted(inventory.items())
                    ),
                )
                connection.execute(
                    "UPDATE model_availability_identity SET revision = %s, updated_at = now() "
                    "WHERE model_id = %s",
                    (operation.desired_revision, model_id),
                )
                values = operation.model_dump(mode="python")
                values["targets"] = Jsonb(operation.model_dump(mode="json")["targets"])
                connection.execute(
                    "INSERT INTO model_availability_operation (id, model_id, idempotency_key, "
                    "expected_revision, desired_revision, desired_enabled, "
                    "created_by, created_at, updated_at, targets) "
                    "VALUES (%(id)s, %(model_id)s, %(idempotency_key)s, %(expected_revision)s, "
                    "%(desired_revision)s, %(desired_enabled)s, %(created_by)s, "
                    "%(created_at)s, %(updated_at)s, %(targets)s)",
                    values,
                )
                connection.execute(
                    "UPDATE managed_model SET enabled = %s, updated_at = now() WHERE id = %s",
                    (command.enabled, model_id),
                )
                connection.execute(
                    "INSERT INTO model_availability_task (model_id, revision, operation_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT (model_id) DO UPDATE SET "
                    "revision = EXCLUDED.revision, operation_id = EXCLUDED.operation_id, "
                    "status = 'pending', available_at = now(), lease_owner = NULL, "
                    "lease_expires_at = NULL, attempt_count = 0, retry_count = 0, "
                    "last_error_code = NULL, updated_at = now()",
                    (model_id, operation.desired_revision, operation.id),
                )
                connection.execute(
                    "INSERT INTO model_availability_audit "
                    "(model_id, operation_id, event, actor, detail) "
                    "VALUES (%s, %s, 'accepted', %s, %s)",
                    (model_id, operation.id, actor, Jsonb(operation.model_dump(mode="json"))),
                )
                return operation
        except UniqueViolation as error:
            raise ModelAvailabilityConflict(
                "Model availability command conflicted; retry its key"
            ) from error

    def stage_model_availability(
        self, model_id: UUID, publication_id: UUID
    ) -> ModelAvailabilityState:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO model_availability_identity "
                "(model_id, lifecycle, staged_publication_id) "
                "VALUES (%s, 'staged', %s) ON CONFLICT (model_id) DO NOTHING",
                (model_id, publication_id),
            )
            row = connection.execute(
                _STATE_SQL + " WHERE identity.model_id = %s FOR UPDATE OF identity", (model_id,)
            ).fetchone()
            current = _state(row)
            if current.lifecycle == "deleted":
                raise ModelAvailabilityConflict("A deleted model identity cannot be republished")
            owner = connection.execute(
                "SELECT staged_publication_id FROM model_availability_identity WHERE model_id = %s",
                (model_id,),
            ).fetchone()
            if current.lifecycle == "staged" and owner["staged_publication_id"] != publication_id:
                raise ModelAvailabilityConflict("The staged model belongs to another publication")
            connection.execute(
                "INSERT INTO model_availability_task (model_id, revision) VALUES (%s, %s) "
                "ON CONFLICT (model_id) DO NOTHING",
                (model_id, current.revision),
            )
        return current

    def claim_model_availability_task(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                "WITH candidate AS (SELECT model_id FROM model_availability_task "
                "WHERE status IN ('pending', 'leased') AND available_at <= now() "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= now()) "
                "ORDER BY available_at, model_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE model_availability_task task SET status = 'leased', lease_owner = %s, "
                "lease_expires_at = now() + (%s * interval '1 second'), "
                "attempt_count = task.attempt_count + 1, updated_at = now() "
                "FROM candidate WHERE task.model_id = candidate.model_id RETURNING task.*",
                (worker_id, lease_seconds),
            ).fetchone()
            if row is None:
                inventory, capabilities = _coverage(connection)
                if _ready(inventory, capabilities, datetime.now(UTC)):
                    row = connection.execute(
                        "WITH candidate AS (SELECT task.model_id FROM model_availability_task task "
                        "JOIN model_availability_identity identity "
                        "ON identity.model_id = task.model_id WHERE task.status = 'failed' "
                        "AND task.operation_id IS NULL AND identity.lifecycle = 'active' "
                        "AND task.revision = identity.revision AND task.available_at <= now() "
                        "AND task.lease_owner IS NULL ORDER BY task.available_at, task.model_id "
                        "FOR UPDATE OF task SKIP LOCKED LIMIT 1) "
                        "UPDATE model_availability_task task SET status = 'leased', "
                        "lease_owner = %s, lease_expires_at = now() + (%s * interval '1 second'), "
                        "attempt_count = task.attempt_count + 1, updated_at = now() "
                        "FROM candidate WHERE task.model_id = candidate.model_id RETURNING task.*",
                        (worker_id, lease_seconds),
                    ).fetchone()
                    if row:
                        connection.execute(
                            "INSERT INTO model_availability_audit (model_id, event, actor, detail) "
                            "VALUES (%s, 'initialization_recovery', %s, %s)",
                            (
                                row["model_id"],
                                worker_id,
                                Jsonb(
                                    {
                                        key: row[key]
                                        for key in ("revision", "attempt_count", "retry_count")
                                    }
                                ),
                            ),
                        )
        return cast(dict[str, Any] | None, row)

    def renew_model_availability_lease(
        self, model_id: UUID, revision: int, worker_id: str, lease_seconds: int
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "UPDATE model_availability_task SET "
                "lease_expires_at = now() + (%s * interval '1 second') "
                "WHERE model_id = %s AND revision = %s AND lease_owner = %s "
                "AND lease_expires_at > now() "
                "AND status = 'leased' RETURNING model_id",
                (lease_seconds, model_id, revision, worker_id),
            ).fetchone()
        return row is not None

    def mark_model_availability_projected(
        self, model_id: UUID, revision: int, worker_id: str, projected: ModelAvailabilityState
    ) -> bool:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                _STATE_SQL + " WHERE identity.model_id = %s FOR UPDATE OF identity", (model_id,)
            ).fetchone()
            task = connection.execute(
                "SELECT * FROM model_availability_task WHERE model_id = %s AND revision = %s "
                "AND lease_owner = %s AND lease_expires_at > now() "
                "AND status = 'leased' FOR UPDATE",
                (model_id, revision, worker_id),
            ).fetchone()
            if not row or not task or _state(row) != projected or projected.revision != revision:
                return False
            if task["operation_id"] is None:
                return True
            existing = connection.execute(
                "SELECT * FROM model_availability_operation WHERE id = %s", (task["operation_id"],)
            ).fetchone()
            operation = _operation(existing)
            if (
                operation.model_id != model_id
                or operation.desired_revision != revision
                or operation.desired_enabled != projected.enabled
            ):
                return False
            if operation.projected_revision == revision and operation.projected_at is not None:
                return True
            now = datetime.now(UTC)
            connection.execute(
                "UPDATE model_availability_operation SET status = 'projected', "
                "projected_revision = %s, "
                "projected_at = %s, updated_at = %s WHERE id = %s",
                (revision, now, now, operation.id),
            )
            connection.execute(
                "INSERT INTO model_availability_audit "
                "(model_id, operation_id, event, actor, detail) "
                "VALUES (%s, %s, 'projected', %s, %s)",
                (model_id, operation.id, worker_id, Jsonb({"revision": revision})),
            )
        return True

    def finish_model_availability_task(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        projected: ModelAvailabilityState | None,
        results: Sequence[ModelAvailabilityTargetResult],
        error_code: str | None,
    ) -> bool:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                _STATE_SQL + " WHERE identity.model_id = %s FOR UPDATE OF identity", (model_id,)
            ).fetchone()
            current = _state(row) if row else None
            task = connection.execute(
                "SELECT * FROM model_availability_task WHERE model_id = %s AND revision = %s "
                "AND lease_owner = %s AND lease_expires_at > now() "
                "AND status = 'leased' FOR UPDATE",
                (model_id, revision, worker_id),
            ).fetchone()
            if task is None or current is None or current.revision != revision:
                return False
            inventory, capabilities = _coverage(connection)
            now = datetime.now(UTC)
            projected_ok = projected == current
            verified = (
                projected_ok
                and _ready(inventory, capabilities, now)
                and (
                    len(results) == len(inventory)
                    and len({result.target_id for result in results}) == len(results)
                    and all(
                        result.target_id in inventory
                        and result.inventory_digest == inventory[result.target_id]
                        and result.policy_digest == capabilities[result.target_id].policy_digest
                        and result.status == "verified"
                        and result.observed_revision == revision
                        and result.observed_enabled == current.enabled
                        and result.verified_at is not None
                        and 0 <= (now - result.verified_at).total_seconds() <= 300
                        for result in results
                    )
                )
            )
            exhausted = task["attempt_count"] >= 30 * (task["retry_count"] + 1)
            task_status = "done" if verified else "failed" if exhausted else "pending"
            status = (
                "succeeded"
                if verified
                else "failed"
                if exhausted
                else (
                    "partial"
                    if any(result.status == "verified" for result in results)
                    else "projected"
                    if projected_ok
                    else "pending"
                )
            )
            if task["operation_id"] is not None:
                prior = _operation(
                    connection.execute(
                        "SELECT * FROM model_availability_operation WHERE id = %s",
                        (task["operation_id"],),
                    ).fetchone()
                )
                operation = ModelAvailabilityOperation.model_validate(
                    {
                        **prior.model_dump(),
                        "status": status,
                        "targets": list(results),
                        "updated_at": now,
                        "error_code": error_code,
                        "projected_revision": revision
                        if projected_ok
                        else prior.projected_revision,
                        "projected_at": prior.projected_at or (now if projected_ok else None),
                        "completed_at": now if verified else None,
                    }
                )
                connection.execute(
                    "UPDATE model_availability_operation SET status = %s, targets = %s, "
                    "projected_revision = %s, projected_at = %s, completed_at = %s, "
                    "error_code = %s, updated_at = %s WHERE id = %s",
                    (
                        operation.status,
                        Jsonb(operation.model_dump(mode="json")["targets"]),
                        operation.projected_revision,
                        operation.projected_at,
                        operation.completed_at,
                        operation.error_code,
                        now,
                        operation.id,
                    ),
                )
            if verified:
                connection.execute(
                    "UPDATE model_availability_identity SET effective_revision = %s, "
                    "effective_enabled = %s, verified_at = %s WHERE model_id = %s",
                    (revision, current.enabled, now, model_id),
                )
            connection.execute(
                "UPDATE model_availability_task SET status = %s, lease_owner = NULL, "
                "lease_expires_at = NULL, available_at = now() + interval '30 seconds', "
                "last_error_code = %s, updated_at = now() WHERE model_id = %s",
                (task_status, error_code, model_id),
            )
            connection.execute(
                "INSERT INTO model_availability_audit "
                "(model_id, operation_id, event, actor, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    model_id,
                    task["operation_id"],
                    status,
                    worker_id,
                    Jsonb(
                        {
                            "revision": revision,
                            "attempt_count": task["attempt_count"],
                            "projected": projected_ok,
                            "error_code": error_code,
                            "targets": [result.model_dump(mode="json") for result in results],
                        }
                    ),
                ),
            )
        return True

    def retry_model_availability(
        self, operation_id: UUID, expected_revision: int, actor: str
    ) -> ModelAvailabilityOperation:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                "SELECT * FROM model_availability_operation WHERE id = %s", (operation_id,)
            ).fetchone()
            if row is None:
                raise ModelAvailabilityConflict("Model availability operation is unknown")
            operation = _operation(row)
            identity = connection.execute(
                "SELECT * FROM model_availability_identity WHERE model_id = %s FOR UPDATE",
                (operation.model_id,),
            ).fetchone()
            task = connection.execute(
                "SELECT * FROM model_availability_task WHERE model_id = %s FOR UPDATE",
                (operation.model_id,),
            ).fetchone()
            if (
                not identity
                or not task
                or identity["revision"] != expected_revision
                or operation.desired_revision != expected_revision
                or task["operation_id"] != operation_id
                or identity["lifecycle"] != "active"
            ):
                raise ModelAvailabilityConflict("Model availability revision changed")
            if task["status"] != "failed":
                return operation
            inventory, capabilities = _coverage(connection)
            if task["retry_count"] >= 3 or not _ready(inventory, capabilities, datetime.now(UTC)):
                raise ModelAvailabilityConflict(
                    "Model availability retry is not ready or exhausted"
                )
            connection.execute(
                "UPDATE model_availability_task SET status = 'pending', "
                "retry_count = retry_count + 1, available_at = now(), "
                "lease_owner = NULL, lease_expires_at = NULL, updated_at = now() "
                "WHERE model_id = %s",
                (operation.model_id,),
            )
            updated = connection.execute(
                "UPDATE model_availability_operation SET status = 'pending', updated_at = now(), "
                "completed_at = NULL, error_code = NULL WHERE id = %s RETURNING *",
                (operation_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO model_availability_audit "
                "(model_id, operation_id, event, actor, detail) "
                "VALUES (%s, %s, 'retry', %s, %s)",
                (
                    operation.model_id,
                    operation_id,
                    actor,
                    Jsonb(
                        {
                            "revision": expected_revision,
                            "attempt_count": task["attempt_count"],
                            "retry_count": task["retry_count"] + 1,
                        }
                    ),
                ),
            )
        return _operation(updated)
