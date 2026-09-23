from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from ..domain.billable_requests import (
    BillableOutcomeUncertain,
    BillableRequestOutcome,
    BillableRequestPlan,
)
from ..domain.control_plane import GatewayModelBinding
from ..domain.image_profiles import validate_image_profile
from ..integrations.apim_control_plane_contract import (
    AuthorizationRequiredError,
    PolicyCompilationError,
)
from ..persistence.repository_contract import QueryRepository


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
    ) -> None:
        self._repository = repository
        self._operation_key = operation_key
        self._authorization_id = authorization_id
        self._attempt_limit = attempt_limit
        self._heartbeat = heartbeat
        self._plan_context = dict(plan_context or {})
        self._planned_model_ids = dict(planned_model_ids or {})

    def heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat()

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
            failed = (
                error.billable_outcome
                if isinstance(error, (PolicyCompilationError, AuthorizationRequiredError))
                else None
            )
            outcome = failed or BillableRequestOutcome(
                evidence={
                    "error_type": type(error).__name__,
                    "image_validated": False,
                }
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
