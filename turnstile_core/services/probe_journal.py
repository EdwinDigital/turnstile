from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..domain.billable_requests import (
    BillableOutcomeUncertain,
    BillableRequestOutcome,
    BillableRequestPlan,
)
from ..domain.budget_counting import BudgetOperation
from ..domain.control_plane import GatewayModelBinding
from ..domain.image_profiles import validate_image_profile
from ..domain.storage_routes import RouteContent, canonical_document, document_digest
from ..integrations.apim_control_plane_contract import (
    AuthorizationRequiredError,
    PolicyCompilationError,
    ProbeVerificationDeferred,
    ProbeVerificationJournal,
)
from ..persistence.repository_contract import QueryRepository

MAX_PROBE_STEPS = 2000


class _ProbeCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    steps: dict[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], Literal["started", "passed"]] = (
        Field(default_factory=dict, max_length=MAX_PROBE_STEPS)
    )


class PublicationProbeProgress:
    def __init__(
        self,
        context_sha256: str,
        checkpoint: object,
        persist: Callable[[dict[str, Any] | None, dict[str, Any]], None],
        *,
        maximum_steps: int = 8,
        time_budget_seconds: float = 60,
        request_budget_seconds: float = 120,
        hard_deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_steps < 1 or min(time_budget_seconds, request_budget_seconds) <= 0:
            raise ValueError("Verification slice limits must be positive")
        self._checkpoint = (
            _ProbeCheckpoint.model_validate(checkpoint) if checkpoint is not None
            else _ProbeCheckpoint(context_sha256=context_sha256)
        )
        if self._checkpoint.context_sha256 != context_sha256:
            raise PolicyCompilationError("Publication verification context changed")
        self._stored = self._checkpoint.model_dump(mode="json") if checkpoint is not None else None
        self._persist = persist
        self._maximum_steps = maximum_steps
        self._completed = 0
        self._clock = clock
        self._deadline = clock() + time_budget_seconds
        self._request_budget_seconds = request_budget_seconds
        self._hard_deadline = hard_deadline
        self._step_deadline: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return self._checkpoint.model_dump(mode="json")

    def request_timeout(self, maximum: float) -> float:
        if self._step_deadline is None:
            return maximum
        remaining = self._step_deadline - self._clock()
        if remaining <= 0:
            raise PolicyCompilationError("Verification step deadline reached; outcome unconfirmed")
        return min(maximum, remaining)

    def _save(self, key: str, state: Literal["started", "passed"]) -> None:
        candidate = _ProbeCheckpoint.model_validate({
            **self.snapshot(), "steps": {**self._checkpoint.steps, key: state},
        })
        updated = candidate.model_dump(mode="json")
        self._persist(self._stored, updated)
        self._checkpoint = candidate
        self._stored = candidate.model_dump(mode="json")

    def run_step(
        self, key: str, verify: Callable[[], None],
        *, recover: Callable[[], None] | None = None,
    ) -> None:
        state = self._checkpoint.steps.get(key)
        if state == "passed":
            return
        if state == "started":
            if recover is not None:
                recover()
                self._save(key, "passed")
                self._completed += 1
                return
            raise PolicyCompilationError(
                "A publication verification outcome is unconfirmed; no request was replayed"
            )
        if (self._completed >= self._maximum_steps or self._clock() >= self._deadline
            or self._hard_deadline is not None
            and self._clock() + self._request_budget_seconds > self._hard_deadline):
            raise ProbeVerificationDeferred(
                "Verification slice completed; continue from persisted probe checkpoints"
            )
        self._save(key, "started")
        self._step_deadline = self._clock() + self._request_budget_seconds
        try:
            verify()
            self._save(key, "passed")
            self._completed += 1
        finally:
            self._step_deadline = None


class PersistentProbeJournal:
    def __init__(
        self,
        repository: QueryRepository,
        operation_key: str,
        authorization_id: UUID,
        attempt_limit: int = 1,
        heartbeat: Callable[[], None] | None = None,
        *,
        plan_context: Mapping[str, Any] | None = None,
        planned_model_ids: Mapping[str, UUID] | None = None,
        request_recorder: Callable[[str, str], None] | None = None,
        verification: ProbeVerificationJournal | None = None,
    ) -> None:
        self._repository = repository
        self._operation_key = operation_key
        self._authorization_id = authorization_id
        self._attempt_limit = attempt_limit
        self._heartbeat = heartbeat
        self._plan_context = dict(plan_context or {})
        self._planned_model_ids = dict(planned_model_ids or {})
        self._request_recorder = request_recorder
        self.verification = verification

    def heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat()

    def record_request(self, method: str, endpoint: str) -> None:
        self.heartbeat()
        if self._request_recorder is not None:
            self._request_recorder(method, endpoint)

    def run_route(
        self, route: RouteContent, operation: BudgetOperation, endpoint: str,
        body: Mapping[str, Any], send: Callable[[str], dict[str, Any]],
        *, recovery_only: bool = False, member_backend_id: str | None = None,
    ) -> dict[str, Any]:
        self.heartbeat()
        route = RouteContent.model_validate_json(canonical_document(route))
        if operation not in route.operations or body.get("model") != route.alias:
            raise PolicyCompilationError("Route probe does not match its approved operation")
        route.connection.require_probe_member(member_backend_id)
        member_context = {"member_backend_id": member_backend_id} if member_backend_id else {}
        contract = route.budget.protocols.get(operation, route.budget)
        payload = canonical_document(body)
        if contract.algorithm == "request_total_ceiling_v1":
            reserved = contract.request_token_ceiling
        else:
            requested = body.get(contract.output_field)
            if operation == "chat-completions":
                requested = body.get("max_completion_tokens", body.get("max_tokens", requested))
            output = requested if requested is not None else contract.output_estimate
            if output is None:
                output = min(4096, contract.output_ceiling)
            if type(output) is not int or output < 1:
                raise PolicyCompilationError("Route probe output bound is invalid")
            if (
                contract.algorithm != "legacy_request_estimate_v1"
                and output > contract.output_ceiling
            ):
                raise PolicyCompilationError("Route probe output exceeds its approved bound")
            samples = 1
            for field in ("n", "best_of") if operation == "chat-completions" else ():
                count = body.get(field, 1)
                if type(count) is not int or count < 1:
                    raise PolicyCompilationError("Route probe sample bound is invalid")
                samples = max(samples, count)
            reserved = (
                len(payload.encode()) if contract.algorithm == "legacy_request_estimate_v1"
                else contract.input_ceiling
            ) + output * samples
        if reserved is None or not 0 < reserved <= 2**63 - 1:
            raise PolicyCompilationError("Route probe reservation is invalid")
        now = datetime.now(UTC)
        plan = BillableRequestPlan(
            operation_key=self._operation_key + ":route:" + document_digest({
                "model": route.model_id, "alias": route.alias, "operation": operation,
                "endpoint": endpoint, **member_context,
            }),
            plan_sha256=document_digest({
                "route": route.model_dump(mode="json"), "operation": operation,
                "endpoint": endpoint, "body": dict(body), "context": self._plan_context,
                **member_context,
            }),
            scope_type="system", scope_id=self._operation_key,
            period_start=date(now.year, now.month, 1), model_id=route.model_id,
            model_key=route.alias, reserved_tokens=reserved,
            authorization_id=self._authorization_id, attempt_limit=self._attempt_limit,
            reuse_requires={
                "route_validated": True,
                **({"image_validated": True} if operation == "images-generations" else {}),
            },
        )
        try:
            attempt = (
                self._repository.recover_billable_request(plan) if recovery_only
                else self._repository.begin_billable_request(plan)
            )
        except BillableOutcomeUncertain as error:
            raise PolicyCompilationError(
                "Route probe outcome is unconfirmed; no request replayed",
            ) from error
        if attempt.state == "exact":
            if (attempt.evidence.get("route_validated") is not True
                    or attempt.evidence.get("member_backend_id") != member_backend_id
                    or attempt.actual_tokens != attempt.evidence.get("total_tokens")
                    or operation == "images-generations"
                    and attempt.evidence.get("image_validated") is not True):
                raise PolicyCompilationError("Route probe charge is not validated route evidence")
            return dict(attempt.evidence)
        if recovery_only:
            raise PolicyCompilationError("Route probe outcome is unconfirmed; no request replayed")
        try:
            self.heartbeat()
            evidence = send(str(attempt.id))
            if evidence.get("member_backend_id") != member_backend_id:
                raise PolicyCompilationError(
                    "Route probe evidence belongs to another Pool member",
                    billable_outcome=BillableRequestOutcome(
                        actual_tokens=evidence.get("total_tokens"),
                        correlation_id=evidence.get("correlation_id"),
                        evidence={**evidence, "route_validated": False},
                    ),
                )
            outcome = BillableRequestOutcome(
                actual_tokens=evidence.get("total_tokens"),
                correlation_id=evidence.get("correlation_id"), evidence=evidence,
            )
        except Exception as error:
            failed = (
                error.billable_outcome
                if isinstance(error, (PolicyCompilationError, AuthorizationRequiredError)) else None
            )
            outcome = failed or BillableRequestOutcome(evidence={
                "route_validated": False, "error_type": type(error).__name__,
            })
            self._repository.finish_billable_request(
                attempt.id, actual_tokens=outcome.actual_tokens,
                correlation_id=outcome.correlation_id, evidence=outcome.evidence,
            )
            raise
        self._repository.finish_billable_request(
            attempt.id, actual_tokens=outcome.actual_tokens,
            correlation_id=outcome.correlation_id, evidence=outcome.evidence,
        )
        if outcome.actual_tokens is None or evidence.get("route_validated") is not True:
            raise PolicyCompilationError("Route probe did not retain exact validated evidence")
        return evidence

    def run(
        self,
        binding: GatewayModelBinding,
        revision: str,
        body: Mapping[str, Any],
        send: Callable[[str, str], dict[str, Any]],
        *,
        gateway_endpoint: str | None = None,
    ) -> None:
        self.heartbeat()
        profile = validate_image_profile(binding.model.image_profile)
        identity = self._repository.model_identities().get(binding.model.model_key)
        planned_id = self._planned_model_ids.get(binding.model.model_key)
        model_id = (
            str(planned_id) if planned_id is not None else identity.model_id if identity else None
        )
        if model_id is None:
            raise PolicyCompilationError("Image probe model identity is unavailable")
        operation_key = self._operation_key + ":" + binding.model.model_key
        plan_context = dict(self._plan_context)
        if gateway_endpoint is not None:
            operation_key += ":gateway:" + hashlib.sha256(gateway_endpoint.encode()).hexdigest()
            plan_context["gateway_endpoint"] = gateway_endpoint
        now = datetime.now(UTC)
        try:
            attempt = self._repository.begin_billable_request(
                BillableRequestPlan(
                    operation_key=operation_key,
                    plan_sha256=hashlib.sha256(
                        json.dumps(
                            {
                                "binding": binding.model_dump(mode="json"),
                                "model_id": model_id,
                                "revision": revision,
                                "body": body,
                                "context": plan_context,
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                    scope_type="system",
                    scope_id=self._operation_key,
                    period_start=date(now.year, now.month, 1),
                    model_id=model_id,
                    model_key=binding.model.model_key,
                    reserved_tokens=len(json.dumps(body, ensure_ascii=False).encode())
                    + profile.output_reservation_tokens,
                    authorization_id=self._authorization_id,
                    attempt_limit=self._attempt_limit,
                    reuse_requires={"image_validated": True},
                )
            )
        except BillableOutcomeUncertain as error:
            raise PolicyCompilationError(str(error)) from error
        if attempt.state == "exact":
            if attempt.evidence.get("image_validated") is not True:
                raise PolicyCompilationError("A confirmed charge is not image-validation evidence")
            return
        try:
            evidence = send(str(attempt.id), attempt.model_id)
            outcome = BillableRequestOutcome(
                actual_tokens=evidence.get("total_tokens"),
                correlation_id=evidence.get("correlation_id"),
                evidence=evidence,
            )
        except Exception as error:
            failed_outcome = (
                error.billable_outcome
                if isinstance(error, (PolicyCompilationError, AuthorizationRequiredError)) else None
            )
            outcome = failed_outcome or BillableRequestOutcome(
                evidence={"error_type": type(error).__name__, "image_validated": False},
            )
            self._repository.finish_billable_request(
                attempt.id,
                actual_tokens=outcome.actual_tokens,
                correlation_id=outcome.correlation_id,
                evidence=outcome.evidence,
            )
            raise
        self._repository.finish_billable_request(
            attempt.id,
            actual_tokens=outcome.actual_tokens,
            correlation_id=outcome.correlation_id,
            evidence=outcome.evidence,
        )
        if outcome.actual_tokens is None or evidence.get("image_validated") is not True:
            raise PolicyCompilationError(
                "Image verification did not retain exact validated evidence"
            )
