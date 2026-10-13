from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..domain.billable_requests import (
    BillableBudgetExceeded,
    BillableOutcomeUncertain,
    BillableRequestAttempt,
    BillableRequestOutcome,
    BillableRequestPlan,
    recover_billable_attempt,
)


class PostgreSqlBillableRequestRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def reservation_evidence_correlations(
        self, scope_type: str, scope_id: str, request_ids: Sequence[str],
    ) -> dict[str, str]:
        if not request_ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, correlation_id FROM billable_request_attempt
                   WHERE id::text = ANY(%s) AND scope_type = %s AND scope_id = %s
                     AND correlation_id IS NOT NULL AND strict_budget_evidence(created_at)""",
                (list(request_ids), scope_type, scope_id),
            ).fetchall()
        return {str(row["id"]): str(row["correlation_id"]) for row in rows}

    def begin_billable_request(self, plan: BillableRequestPlan) -> BillableRequestAttempt:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", ("billable:" + plan.operation_key,)
            )
            rows = connection.execute(
                     """SELECT * FROM billable_request_attempt
                         WHERE operation_key = %s ORDER BY attempt_index""",
                (plan.operation_key,),
            ).fetchall()
            for row in rows:
                if (
                    row["plan_sha256"] == plan.plan_sha256 and row["state"] == "exact"
                    and row["evidence"].get("admission_authority", "postgresql")
                    == plan.admission_authority
                    and all(key in row["evidence"] and row["evidence"][key] == value
                            for key, value in plan.reuse_requires.items())
                ):
                    return BillableRequestAttempt.model_validate(row)
            if len(rows) >= plan.attempt_limit or any(
                row["authorization_id"] == plan.authorization_id for row in rows
            ):
                raise BillableOutcomeUncertain(
                    "A billable attempt already exists; inspect its evidence "
                    "before authorizing another"
                )
            if plan.scope_type != "system" and plan.admission_authority == "postgresql":
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"billable-budget:{plan.scope_type}:{plan.scope_id}:{plan.period_start}",),
                )
                if plan.scope_type == "person":
                    quota = connection.execute(
                        """SELECT budget.token_limit,
                                  COALESCE(enforcement.mode, 'audit') = 'block' AS enforce
                           FROM token_budget budget LEFT JOIN department_enforcement enforcement
                             ON enforcement.department_id = budget.parent_scope_id
                           WHERE budget.scope_type = 'user' AND budget.scope_id = %s
                             AND budget.period_start = %s""",
                        (plan.scope_id, plan.period_start),
                    ).fetchone()
                else:
                    quota = connection.execute(
                        """SELECT token_limit, enforce FROM gateway_application_budget
                           WHERE application_id = %s AND period_start = %s""",
                        (plan.scope_id, plan.period_start),
                    ).fetchone()
                period_end = date(
                    plan.period_start.year + (plan.period_start.month == 12),
                    1 if plan.period_start.month == 12 else plan.period_start.month + 1,
                    1,
                )
                usage = connection.execute(
                    """SELECT budget_scope_confirmed_tokens(%s, %s, %s, %s)
                         + budget_scope_pending_request_tokens(%s, %s, %s) AS committed""",
                    (
                        plan.scope_type,
                        plan.scope_id,
                        plan.period_start,
                        period_end,
                        plan.scope_type,
                        plan.scope_id,
                        plan.period_start,
                    ),
                ).fetchone()
                if (
                    quota
                    and quota["enforce"]
                    and usage["committed"] + plan.reserved_tokens > quota["token_limit"]
                ):
                    raise BillableBudgetExceeded("The remaining budget cannot cover this request")
            row = connection.execute(
                """INSERT INTO billable_request_attempt (
                     id, operation_key, plan_sha256, scope_type, scope_id, period_start,
                   model_id, model_key, reserved_tokens,
                   authorization_id, attempt_index, state, evidence
                   ) VALUES (%s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, 'started', %s)
                   RETURNING *""",
                (
                    uuid4(),
                    plan.operation_key,
                    plan.plan_sha256,
                    plan.scope_type,
                    plan.scope_id,
                    plan.period_start,
                    plan.model_id,
                    plan.model_key,
                    plan.reserved_tokens,
                    plan.authorization_id,
                    len(rows) + 1,
                      Jsonb({"admission_authority": "apim_execution"}
                          if plan.admission_authority == "apim_execution" else {}),
                ),
            ).fetchone()
        return BillableRequestAttempt.model_validate(row)

    def recover_billable_request(self, plan: BillableRequestPlan) -> BillableRequestAttempt:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM billable_request_attempt
                   WHERE operation_key = %s ORDER BY attempt_index LIMIT 33""",
                (plan.operation_key,),
            ).fetchall()
        return recover_billable_attempt(
            plan, [BillableRequestAttempt.model_validate(row) for row in rows],
        )

    def finish_billable_request(
        self,
        request_id: UUID,
        *,
        actual_tokens: int | None,
        correlation_id: str | None,
        evidence: dict[str, Any],
    ) -> None:
        BillableRequestOutcome(
            actual_tokens=actual_tokens, correlation_id=correlation_id, evidence=evidence
        )
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE billable_request_attempt SET state = %s, actual_tokens = %s,
                                     correlation_id = COALESCE(%s, correlation_id),
                                     evidence = (%s::jsonb - 'admission_authority') ||
                                         CASE WHEN evidence ? 'admission_authority'
                                             THEN jsonb_build_object(
                                                 'admission_authority',
                                                 evidence->>'admission_authority')
                                         ELSE '{}'::jsonb END, updated_at = now()
                   WHERE id = %s AND state <> 'exact' RETURNING id""",
                (
                    "exact" if actual_tokens is not None else "uncertain",
                    actual_tokens,
                    correlation_id,
                    Jsonb(evidence),
                    request_id,
                ),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT actual_tokens FROM billable_request_attempt WHERE id = %s",
                    (request_id,),
                ).fetchone()
                if existing is None:
                    raise ValueError("Unknown billable request attempt")
                if actual_tokens is not None and existing["actual_tokens"] != actual_tokens:
                    raise ValueError("An exact acknowledgement cannot be changed")

    def pending_billable_requests(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT attempt.id, attempt.scope_type, attempt.scope_id, attempt.period_start,
                   attempt.reserved_tokens,
                   attempt.created_at,
                   finalization.finalization_kind
                   FROM billable_request_effective attempt
                   LEFT JOIN
                   budget_reservation_finalization_effective AS finalization
                   ON finalization.correlation_id = attempt.id::text
                   AND finalization.scope_type = attempt.scope_type
                   AND finalization.scope_id = attempt.scope_id
                   AND finalization.period_start = attempt.period_start
                   WHERE attempt.scope_type IN ('person', 'application')
                   AND attempt.evidence->>'admission_authority' IS DISTINCT FROM 'apim_execution'
                   AND attempt.effective_state <> 'exact'"""
            ).fetchall()
        return [dict(row) for row in rows]

    def settled_billable_requests(
        self,
        request_ids: list[str],
        scope_type: str,
        scope_id: str | None,
    ) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id FROM billable_request_effective WHERE id::text = ANY(%s)
                         AND effective_state = 'exact' AND scope_type = %s AND scope_id = %s""",
                (request_ids, scope_type, scope_id),
            ).fetchall()
        return {str(row["id"]) for row in rows}
