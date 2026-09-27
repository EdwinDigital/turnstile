from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import ValidationError

from ..domain.model_availability import (
    AVAILABILITY_PARTITION,
    ModelAvailabilityCommand,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeEnrollment,
    ModelAvailabilityOperation,
    ModelAvailabilityReadback,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
    ModelAvailabilityUnavailable,
)
from ..integrations.apim_control_plane_contract import ApimPublisherClientResolver
from ..integrations.apim_publisher_client import AzureApimPublisherClient
from ..integrations.ledger import TableStorageLedger
from ..persistence.repository_contract import QueryRepository
from .worker_lease import WorkerLease

logger = logging.getLogger(__name__)


class AvailabilityStateReader(Protocol):
    def get_model_availability_state(self, model_id: UUID) -> ModelAvailabilityState | None: ...


class ModelAvailabilityRepository(AvailabilityStateReader, Protocol):
    def model_availability_supported(self) -> bool: ...

    def list_model_availability(self) -> list[ModelAvailabilityReadback]: ...

    def change_model_availability(
        self,
        model_id: UUID,
        command: ModelAvailabilityCommand,
        actor: str,
    ) -> ModelAvailabilityOperation: ...

    def retry_model_availability(
        self,
        operation_id: UUID,
        expected_revision: int,
        actor: str,
    ) -> ModelAvailabilityOperation: ...

    def get_model_availability_operation(
        self,
        operation_id: UUID,
    ) -> ModelAvailabilityOperation | None: ...

    def get_model_availability_inventory(self) -> dict[UUID, str]: ...

    def get_model_availability_states(
        self,
        model_ids: Sequence[UUID],
    ) -> dict[UUID, ModelAvailabilityState]: ...

    def get_model_availability_coverage(
        self,
        target_id: UUID,
    ) -> ModelAvailabilityCoverage | None: ...

    def save_model_availability_coverage(self, coverage: ModelAvailabilityCoverage) -> bool: ...

    def list_model_availability_node_enrollments(
        self,
        target_id: UUID,
    ) -> list[ModelAvailabilityNodeEnrollment]: ...

    def stage_model_availability(
        self,
        model_id: UUID,
        publication_id: UUID,
    ) -> ModelAvailabilityState: ...

    def claim_model_availability_task(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...

    def renew_model_availability_lease(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        lease_seconds: int,
    ) -> bool: ...

    def mark_model_availability_projected(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        projected: ModelAvailabilityState,
    ) -> bool: ...

    def finish_model_availability_task(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        projected: ModelAvailabilityState | None,
        results: Sequence[ModelAvailabilityTargetResult],
        error_code: str | None,
    ) -> bool: ...


class ModelAvailabilityService:
    def __init__(self, repository: ModelAvailabilityRepository) -> None:
        self._repository = repository

    def _require_supported(self) -> None:
        if not self._repository.model_availability_supported():
            raise ModelAvailabilityUnavailable("Model availability migration is not installed")

    def inventory(self) -> list[ModelAvailabilityReadback]:
        self._require_supported()
        return self._repository.list_model_availability()

    def read(self, model_id: UUID) -> ModelAvailabilityReadback:
        result = next((item for item in self.inventory() if item.model_id == model_id), None)
        if result is None:
            raise LookupError("Model availability identity is unknown")
        return result

    def change(
        self,
        model_id: UUID,
        command: ModelAvailabilityCommand,
        actor: str,
    ) -> ModelAvailabilityOperation:
        self._require_supported()
        return self._repository.change_model_availability(model_id, command, actor)

    def operation(self, operation_id: UUID) -> ModelAvailabilityOperation:
        self._require_supported()
        result = self._repository.get_model_availability_operation(operation_id)
        if result is None:
            raise LookupError("Model availability operation is unknown")
        return result

    def retry(
        self,
        operation_id: UUID,
        expected_revision: int,
        actor: str,
    ) -> ModelAvailabilityOperation:
        self._require_supported()
        return self._repository.retry_model_availability(operation_id, expected_revision, actor)


def _projected_state(
    value: tuple[dict[str, Any], str] | None,
    model_id: UUID,
) -> ModelAvailabilityState | None:
    if value is None:
        return None
    try:
        return ModelAvailabilityState.from_entity(value[0], model_id)
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ModelAvailabilityUnavailable("Model availability projection is invalid") from error


def project_model_availability(
    repository: AvailabilityStateReader,
    store: TableStorageLedger,
    model_id: UUID,
) -> ModelAvailabilityState:
    for _attempt in range(3):
        stored = store.read_versioned_entity(AVAILABILITY_PARTITION, str(model_id))
        current = _projected_state(stored, model_id)
        desired = repository.get_model_availability_state(model_id)
        if desired is None or desired.model_id != model_id:
            raise ModelAvailabilityUnavailable("Model availability source is missing")
        if current is not None and (
            current.revision > desired.revision
            or current.revision == desired.revision
            and current != desired
        ):
            raise ModelAvailabilityUnavailable("Model availability revision cannot roll back")
        if not store.replace_if_match(
            AVAILABILITY_PARTITION,
            str(model_id),
            desired.entity(),
            stored[1] if stored else None,
        ):
            continue
        observed = _projected_state(
            store.read_versioned_entity(AVAILABILITY_PARTITION, str(model_id)),
            model_id,
        )
        if observed == desired and repository.get_model_availability_state(model_id) == desired:
            return desired
    raise ModelAvailabilityUnavailable("Model availability projection is pending")


class ModelAvailabilityWorker:
    def __init__(
        self,
        repository: ModelAvailabilityRepository,
        store_factory: Callable[[], TableStorageLedger],
        verify: Callable[
            [ModelAvailabilityState, dict[UUID, str]], Sequence[ModelAvailabilityTargetResult]
        ],
    ) -> None:
        self._repository = repository
        self._store_factory = store_factory
        self._verify = verify

    def run_once(self, worker_id: str, lease_seconds: int = 180) -> bool:
        owner = f"{worker_id}:{uuid4().hex}"
        task = self._repository.claim_model_availability_task(owner, lease_seconds)
        if task is None:
            return False
        model_id, revision = UUID(str(task["model_id"])), int(task["revision"])
        projected = None
        results: Sequence[ModelAvailabilityTargetResult] = ()
        error_code = None
        try:
            with WorkerLease(
                lambda: self._repository.renew_model_availability_lease(
                    model_id,
                    revision,
                    owner,
                    lease_seconds,
                ),
                lease_seconds,
            ) as lease:
                with self._store_factory() as store:
                    projected = project_model_availability(self._repository, store, model_id)
                if projected.revision != revision:
                    raise ModelAvailabilityUnavailable("Availability source revision changed")
                if not self._repository.mark_model_availability_projected(
                    model_id,
                    revision,
                    owner,
                    projected,
                ):
                    raise ModelAvailabilityUnavailable("Availability projection lease changed")
                results = self._verify(
                    projected,
                    self._repository.get_model_availability_inventory(),
                )
                lease.heartbeat()
        except Exception:
            error_code = "model_availability_unavailable"
        self._repository.finish_model_availability_task(
            model_id,
            revision,
            owner,
            projected,
            results,
            error_code,
        )
        return True


class ModelAvailabilityCoordinator:
    def __init__(
        self,
        repository: QueryRepository,
        clients: ApimPublisherClientResolver,
        table_endpoint: str,
        table_name: str,
    ) -> None:
        self._repository = repository
        self._availability = cast(ModelAvailabilityRepository, repository)
        self._clients = clients
        self._table_endpoint, self._table_name = table_endpoint, table_name

    def _verify_target(self, target_id: UUID, digest: str) -> ModelAvailabilityCoverage:
        resolved = self._clients.resolve(target_id)
        if not isinstance(resolved.client, AzureApimPublisherClient):
            raise ModelAvailabilityUnavailable("Availability target verifier is unavailable")
        coverage = resolved.client.verify_model_availability(
            target_id,
            digest,
            self._repository.list_gateway_deployments(target_id, include_retired=True),
            self._table_endpoint,
            self._table_name,
            self._availability.get_model_availability_state,
            node_enrollment_reader=partial(
                self._availability.list_model_availability_node_enrollments,
                target_id,
            ),
        )
        if not self._availability.save_model_availability_coverage(coverage):
            raise ModelAvailabilityUnavailable("Availability target inventory changed")
        return coverage

    def refresh(self) -> int:
        verified = 0
        for target_id, digest in self._availability.get_model_availability_inventory().items():
            try:
                self._verify_target(target_id, digest)
            except Exception as error:
                logger.warning(
                    "Model availability coverage failed target=%s error=%s",
                    target_id,
                    type(error).__name__,
                )
                continue
            verified += 1
        return verified

    def verify(
        self,
        projected: ModelAvailabilityState,
        inventory: dict[UUID, str],
    ) -> Sequence[ModelAvailabilityTargetResult]:
        results = []
        for target_id, digest in inventory.items():
            try:
                coverage = self._availability.get_model_availability_coverage(target_id)
                if coverage is None or not coverage.current(datetime.now(UTC), digest):
                    raise ModelAvailabilityUnavailable("Availability coverage is stale")
                resolved = self._clients.resolve(target_id)
                if not isinstance(resolved.client, AzureApimPublisherClient):
                    raise ModelAvailabilityUnavailable("Availability verifier is unavailable")
                results.append(
                    resolved.client.confirm_model_availability(
                        target_id,
                        projected,
                        coverage,
                        self._repository.list_gateway_deployments(target_id, include_retired=True),
                        self._table_endpoint,
                        self._table_name,
                        self._availability.get_model_availability_states,
                        node_enrollment_reader=partial(
                            self._availability.list_model_availability_node_enrollments,
                            target_id,
                        ),
                    )
                )
            except Exception as error:
                logger.warning(
                    "Model availability confirmation failed target=%s error=%s",
                    target_id,
                    type(error).__name__,
                )
                reason = (
                    "self_hosted_node_coverage_unverified"
                    if (
                        isinstance(error, ModelAvailabilityUnavailable)
                        and str(error) == "self_hosted_node_coverage_unverified"
                    )
                    else "model_availability_capability_unverified"
                )
                results.append(
                    ModelAvailabilityTargetResult(
                        target_id=target_id,
                        inventory_digest=digest,
                        status="unknown",
                        reason=reason,
                    )
                )
        return results
