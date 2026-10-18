from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from copy import deepcopy
from functools import partial
from time import time
from typing import Any, Literal, NamedTuple, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from ..domain.budget_counting import BudgetOperation, BudgetPublicationContracts
from ..domain.control_plane import (
    ApiFormat,
    AuthStrategy,
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
    ImageProbeAuthorization,
)
from ..domain.storage_routes import (
    ActiveRouteHead,
    ConfigurationApproval,
    ConnectionApproval,
    ConnectionRevision,
    ModelRouteAddition,
    ModelRouteHead,
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteConfigurationPlan,
    RouteContent,
    RouteCredential,
    RouteMember,
    RoutePendingActivation,
    RouteProfile,
    RoutePublicationTask,
    RouteRequestAdapter,
    RouteScope,
    RouteSnapshot,
    canonical_document,
    catalog_digest,
    configuration_manifests,
    document_digest,
    text_digest,
    validate_origin,
    validate_path,
)
from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    ProbeVerificationDeferred,
    RetryablePublicationError,
)
from ..integrations.storage_route_projection import (
    ModelRouteActivationIntent,
    RouteActivationIntent,
    RouteProjectionUnknown,
    RouteWriterIntent,
    TableRouteProjection,
)
from ..persistence.repository_contract import QueryRepository
from .probe_journal import MAX_PROBE_STEPS, PersistentProbeJournal, PublicationProbeProgress


def require_model_addition_activation(
    publication: Mapping[str, Any], previous: Mapping[str, Any] | None,
    program_upgrade: Mapping[str, Any] | None,
) -> None:
    from ..domain.storage_routes import FixedProgramInstallation, FixedProgramUpgrade
    from ..persistence.repository_support import _model_addition_manifest

    manifest = publication.get("resource_manifest") or {}
    marker = manifest.get("model_route_addition")
    activation = manifest.get("route_activation")
    raw_task = manifest.get("route_configuration")
    if (publication.get("publication_kind") != "model_add"
            or publication.get("status") != "promoting" or not isinstance(marker, dict)
            or not isinstance(activation, dict) or not isinstance(raw_task, dict)):
        raise ValueError("Model addition activation evidence is incomplete")
    _model_addition_manifest(
        previous, publication["desired_spec"], publication["desired_spec_sha256"],
        {"model_route_addition": marker},
    )
    current = GatewayPublication.model_validate(publication)
    task = RoutePublicationTask.model_validate_json(canonical_document(raw_task))
    task.require_publication(
        str(current.id), str(current.base_release_id), current.desired_spec_sha256,
        str(current.gateway_profile_id),
    )
    require_route_specification(current, task)
    installed = FixedProgramInstallation.model_validate_json(canonical_document(
        manifest["fixed_route_program"],
    ))
    if previous is None:
        raise ValueError("Model addition program base is unavailable")
    expected_installation = FixedProgramInstallation.model_validate_json(canonical_document(
        previous["resource_manifest"]["fixed_route_program"],
    ))
    if program_upgrade is not None:
        upgrade = FixedProgramUpgrade.model_validate_json(canonical_document(program_upgrade))
        upgrade.require_previous(str(current.base_release_id), expected_installation)
        expected_installation = upgrade.installation
    if task.serving_programs:
        if task.serving_installation_sha256.get(installed.program.instance_id) != document_digest(
            installed,
        ):
            raise ValueError("Model addition serving installation changed")
        expected_installation = expected_installation.for_native_gateway(
            task.plan.program.scope.gateway_id, task.plan.program.instance_id,
            installed.gateway_endpoint,
        )
    progress = manifest.get("route_progress") or {}
    probes = manifest.get("probe_verification") or {}
    expected_steps = {step.key: "passed" for step in configuration_probe_steps(task)}
    if (not task.model_addition or not installed.model_additions_enabled
            or task.plan.program != installed.program or expected_installation != installed
            or current.apim_revision != installed.program.api_revision
            or current.policy_sha256 != installed.program.program_sha256
            or progress != {"version": 1, "context_sha256": task.context_sha256,
                            "staged": 3 * len(task.scope_tasks),
                            "verified": 3 * len(task.scope_tasks)}
            or probes.get("context_sha256") != task.context_sha256
            or probes.get("steps") != expected_steps
            or activation.get("context_sha256") != task.context_sha256
            or activation.get("outcome") != "confirmed"):
        raise ValueError("Model addition activation evidence changed")
    intent = ModelRouteActivationIntent.from_record(activation["intent"])
    route = task.plan.snapshot.routes[0]
    if (intent.endpoint != task.storage_endpoint or intent.table != task.storage_table
            or intent.head.publication_id != str(current.id)
            or intent.head.alias != route.alias or intent.head.model_id != route.model_id
            or intent.head.content_sha256 != route.sha256):
        raise ValueError("Model addition activation target changed")
    intent.head.head.approval.require_same_configuration(task.plan.snapshot.approval)
    scoped_records = activation.get("scopes", {})
    if set(scoped_records) != {
        child.plan.program.scope.partition for child in task.additional_scope_tasks
    }:
        raise ValueError("Model addition scope activation set changed")
    for child in task.additional_scope_tasks:
        checkpoint = _ActivationCheckpoint.model_validate(
            scoped_records[child.plan.program.scope.partition],
        )
        if checkpoint.context_sha256 != child.context_sha256 or checkpoint.outcome != "confirmed":
            raise ValueError("Model addition scope activation is unconfirmed")
        scoped_intent = ModelRouteActivationIntent.from_record(checkpoint.intent)
        route = child.plan.snapshot.routes[0]
        if (scoped_intent.endpoint != child.storage_endpoint
                or scoped_intent.table != child.storage_table
                or scoped_intent.head.publication_id != str(current.id)
                or scoped_intent.head.alias != route.alias
                or scoped_intent.head.model_id != route.model_id
                or scoped_intent.head.content_sha256 != route.sha256):
            raise ValueError("Model addition scope activation target changed")
        scoped_intent.head.head.approval.require_same_configuration(child.plan.snapshot.approval)


class RouteActivationNotApplied(PolicyCompilationError):
    pass


class RouteIntegrityCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: int = Field(default=0, ge=0, le=30000)
    connections: int = Field(default=0, ge=0, le=10000)
    connection_resources: int = Field(default=0, ge=0, le=120, exclude_if=lambda value: value == 0)
    credentials: int = Field(default=0, ge=0, le=20000)
    dependencies_started_epoch: int | None = Field(default=None, ge=0)


class RouteIntegrityDeferred(RetryablePublicationError):
    def __init__(self, checkpoint: RouteIntegrityCheckpoint) -> None:
        super().__init__("Fixed route integrity slice completed")
        self.checkpoint = checkpoint


class _ActivationCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: dict[str, Any]
    scopes: dict[str, dict[str, Any]] = Field(
        default_factory=dict, max_length=31, exclude_if=lambda value: not value,
    )
    outcome: Literal[
        "started", "expired_before_dispatch", "conditional_conflict", "unknown_no_replay",
        "confirmed", "conflict", "unknown", "not_applied_fenced",
    ]


def recorded_route_snapshot(
    publication: GatewayPublication,
    *, scope: RouteScope | None = None,
) -> tuple[RouteSnapshot, ActiveRouteHead]:
    manifest = publication.resource_manifest
    if "route_configuration" in manifest:
        raw_task = manifest["route_configuration"]
        if not isinstance(raw_task, dict):
            raise PolicyCompilationError("Fixed route task is unavailable")
        root_task = RoutePublicationTask.model_validate_json(canonical_document(raw_task))
        root_task.require_publication(
            str(publication.id), str(publication.base_release_id) if publication.base_release_id
            else None, publication.desired_spec_sha256, str(publication.gateway_profile_id),
        )
        require_route_specification(publication, root_task)
        task = root_task
        raw_activation = manifest.get("route_activation")
        if scope is not None:
            selected = [candidate for candidate in root_task.scope_tasks
                        if candidate.plan.program.scope == scope]
            if len(selected) != 1:
                raise PolicyCompilationError("Fixed route scope snapshot is unavailable")
            task = selected[0]
            if task is not root_task:
                if not isinstance(raw_activation, dict):
                    raise PolicyCompilationError("Fixed route scope activation is unavailable")
                raw_activation = raw_activation.get("scopes", {}).get(scope.partition)
        activation = _ActivationCheckpoint.model_validate(raw_activation)
        if activation.context_sha256 != task.context_sha256 or activation.outcome != "confirmed":
            raise PolicyCompilationError("Fixed route activation is not confirmed")
        snapshot = task.plan.snapshot
        if task.model_addition:
            model_intent = ModelRouteActivationIntent.from_record(activation.intent)
            route = snapshot.routes[0]
            if (
                model_intent.endpoint != task.storage_endpoint
                or model_intent.table != task.storage_table
                or model_intent.head.publication_id != str(publication.id)
                or model_intent.head.alias != route.alias
                or model_intent.head.model_id != route.model_id
                or model_intent.head.content_sha256 != route.sha256
            ):
                raise PolicyCompilationError("Model route activation identity changed")
            head = model_intent.head.head
        else:
            intent = RouteActivationIntent.from_record(activation.intent)
            if (
                intent.endpoint != task.storage_endpoint or intent.table != task.storage_table
                or intent.writer_before is not None
                and intent.writer_before.publication_id != str(publication.id)
            ):
                raise PolicyCompilationError(
                    "Fixed route activation belongs to another publication",
                )
            head = intent.head
    else:
        raw_snapshot = manifest.get("fixed_route_snapshot")
        raw_head = manifest.get("fixed_route_head")
        if not isinstance(raw_snapshot, dict) or not isinstance(raw_head, dict):
            raise PolicyCompilationError("Fixed route installation snapshot is unavailable")
        snapshot = RouteSnapshot.model_validate_json(canonical_document(raw_snapshot))
        head = ActiveRouteHead.model_validate_json(canonical_document(raw_head))
        if scope is not None and head.approval.scope != scope:
            raise PolicyCompilationError("Fixed route scope snapshot is unavailable")
    head.approval.require_same_configuration(snapshot.approval)
    if head.approval.scope.target_id != str(publication.gateway_profile_id):
        raise PolicyCompilationError("Fixed route snapshot belongs to another target")
    snapshot = snapshot.model_copy(update={"approval": head.approval})
    snapshot.require_valid(head.activated_epoch)
    return snapshot, head


def recorded_route_scopes(publication: GatewayPublication) -> tuple[RouteScope, ...]:
    raw = publication.resource_manifest.get("route_configuration")
    if raw is None:
        return (recorded_route_snapshot(publication)[1].approval.scope,)
    if not isinstance(raw, dict):
        raise PolicyCompilationError("Fixed route scope task is unavailable")
    task = RoutePublicationTask.model_validate_json(canonical_document(raw))
    return tuple(recorded_route_snapshot(
        publication, scope=scoped.plan.program.scope,
    )[1].approval.scope for scoped in task.scope_tasks)


def recorded_route_dependencies(publication: GatewayPublication) -> GatewayReleaseDependencies:
    from ..services.storage_route_publisher import FixedProgramInstallation

    raw_installation = publication.resource_manifest.get("fixed_route_program")
    if not isinstance(raw_installation, dict):
        raise PolicyCompilationError("Fixed route program installation is unavailable")
    installation = FixedProgramInstallation.model_validate_json(
        canonical_document(raw_installation),
    )
    snapshot, head = recorded_route_snapshot(publication)
    program = installation.program
    if (
        program.scope != head.approval.scope or program.api_revision != publication.apim_revision
        or program.program_sha256 != publication.policy_sha256
        or program.program_sha256 != head.approval.required_program_sha256
        or not set(head.approval.required_profiles) <= set(installation.supported_profiles)
    ):
        raise PolicyCompilationError("Fixed route program and configuration identity changed")
    head_sha256 = document_digest(head)
    if "route_configuration" in publication.resource_manifest:
        raw_task = publication.resource_manifest["route_configuration"]
        if not isinstance(raw_task, dict):
            raise PolicyCompilationError("Fixed route task is unavailable")
        task = RoutePublicationTask.model_validate_json(canonical_document(raw_task))
        if (
            task.plan.program != program or task.storage_endpoint != installation.storage_endpoint
            or task.storage_table != installation.storage_table
            or task.target_identity_sha256 != installation.target_identity_sha256
        ):
            raise PolicyCompilationError("Fixed route task installation changed")
        if task.model_addition:
            if not installation.model_additions_enabled:
                raise PolicyCompilationError("Fixed program does not support model additions")
            activation = _ActivationCheckpoint.model_validate(
                publication.resource_manifest["route_activation"],
            )
            head_sha256 = document_digest(
                ModelRouteActivationIntent.from_record(activation.intent).head,
            )
    pools = {
        route.connection.backend_id: route.connection.pool for route in snapshot.routes
        if route.connection.pool is not None
    }
    return GatewayReleaseDependencies(
        apim_revision=program.api_revision, parent_policy_sha256=installation.parent_policy_sha256,
        compiled_policy_sha256=program.program_sha256,
        route_configuration_sha256=snapshot.configuration_sha256,
        route_head_sha256=head_sha256,
        backends=sorted({route.connection.backend_id for route in snapshot.routes} | {
            member.backend_id for pool in pools.values() for member in pool.members
        }),
        backend_pools=sorted(pools),
        named_values=sorted({name for pool in pools.values()
                     for name in (*pool.credential_versions, *pool.header_values)}),
        recorded_complete=True, live_status="not_checked",
    )


class PersistentRouteActivationJournal:
    def __init__(
        self,
        repository: QueryRepository,
        publication_id: UUID,
        worker_id: str,
        context_sha256: str,
        checkpoint: object,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._repository = repository
        self._publication_id = publication_id
        self._worker_id = worker_id
        self._context_sha256 = context_sha256
        self._heartbeat = heartbeat
        self._checkpoint = (
            _ActivationCheckpoint.model_validate(checkpoint) if checkpoint is not None else None
        )
        if self._checkpoint is not None:
            if self._checkpoint.context_sha256 != context_sha256:
                raise ValueError("ROUTE_ACTIVATION_CONTEXT_CHANGED")
            RouteActivationIntent.from_record(self._checkpoint.intent)
        self._stored = (
            self._checkpoint.model_dump(mode="json") if self._checkpoint is not None else None
        )

    @property
    def intent(self) -> RouteActivationIntent | None:
        return (
            RouteActivationIntent.from_record(self._checkpoint.intent)
            if self._checkpoint is not None else None
        )

    def snapshot(self) -> dict[str, Any] | None:
        return deepcopy(self._stored)

    def _save(self, candidate: _ActivationCheckpoint) -> None:
        if self._heartbeat is not None:
            self._heartbeat()
        recorded = candidate.model_dump(mode="json")
        if not self._repository.checkpoint_gateway_publication_activation(
            self._publication_id, self._worker_id, deepcopy(self._stored), deepcopy(recorded),
        ):
            raise RuntimeError("ROUTE_ACTIVATION_CHECKPOINT_OR_LEASE_CHANGED")
        self._checkpoint = candidate
        self._stored = recorded

    def begin_once(self, intent: RouteActivationIntent) -> None:
        if self._checkpoint is not None:
            raise ValueError("ROUTE_ACTIVATION_ALREADY_STARTED_RECOVER_ONLY")
        self._save(_ActivationCheckpoint(
            context_sha256=self._context_sha256, intent=intent.to_record(), outcome="started",
        ))

    def record_outcome(self, operation_id: str, outcome: str) -> None:
        intent = self.intent
        if intent is None or intent.receipt.operation_id != operation_id:
            raise ValueError("ROUTE_ACTIVATION_OPERATION_CHANGED")
        assert self._checkpoint is not None
        previous = self._checkpoint.outcome
        if previous == outcome:
            return
        terminal = {"confirmed", "conflict", "not_applied_fenced", "expired_before_dispatch"}
        expired_fenced = previous == "expired_before_dispatch" and outcome in {
            "conflict", "not_applied_fenced",
        }
        if (previous in terminal and not expired_fenced) or outcome == "started" or (
            previous != "started" and outcome in {"expired_before_dispatch", "conditional_conflict"}
        ):
            raise ValueError("ROUTE_ACTIVATION_OUTCOME_REGRESSION")
        self._save(_ActivationCheckpoint.model_validate({
            **self._checkpoint.model_dump(mode="json"), "outcome": outcome,
        }))

    def recover(
        self, projection: TableRouteProjection,
    ) -> Literal["confirmed", "conflict", "unknown", "not_applied_fenced"]:
        intent = self.intent
        if intent is None:
            raise ValueError("ROUTE_ACTIVATION_INTENT_MISSING")
        if self._heartbeat is not None:
            self._heartbeat()
        outcome = projection.recover_activation(intent)
        if self._checkpoint is not None and (
            self._checkpoint.outcome == "expired_before_dispatch" and outcome == "unknown"
        ):
            return outcome
        self.record_outcome(intent.receipt.operation_id, outcome)
        return outcome


class PersistentModelRouteActivationJournal:
    def __init__(
        self, repository: QueryRepository, publication_id: UUID, worker_id: str,
        context_sha256: str, checkpoint: object, heartbeat: Callable[[], None],
        *, scope_partition: str | None = None,
    ) -> None:
        self._repository = repository
        self._publication_id = publication_id
        self._worker_id = worker_id
        self._context = context_sha256
        self._heartbeat = heartbeat
        self._scope_partition = scope_partition
        self._checkpoint = (
            _ActivationCheckpoint.model_validate(checkpoint) if checkpoint is not None else None
        )
        if self._checkpoint is not None:
            if self._checkpoint.context_sha256 != context_sha256:
                raise ValueError("MODEL_ACTIVATION_CONTEXT_CHANGED")
            intent = ModelRouteActivationIntent.from_record(self._checkpoint.intent)
            if intent.head.publication_id != str(publication_id):
                raise ValueError("MODEL_ACTIVATION_PUBLICATION_CHANGED")

    @property
    def intent(self) -> ModelRouteActivationIntent | None:
        return (ModelRouteActivationIntent.from_record(self._checkpoint.intent)
                if self._checkpoint is not None else None)

    @property
    def outcome(self) -> str | None:
        return self._checkpoint.outcome if self._checkpoint is not None else None

    def _save(self, checkpoint: _ActivationCheckpoint) -> None:
        self._heartbeat()
        before = self._checkpoint.model_dump(mode="json") if self._checkpoint is not None else None
        recorded = checkpoint.model_dump(mode="json")
        if self._scope_partition is not None:
            row = self._repository.get_gateway_publication(self._publication_id)
            root = (row or {}).get("resource_manifest", {}).get("route_activation")
            if (not isinstance(root, dict)
                    or root.get("scopes", {}).get(self._scope_partition) != before):
                raise RetryablePublicationError("Model scope activation checkpoint changed")
            recorded = {**root, "scopes": {**root.get("scopes", {}),
                                          self._scope_partition: recorded}}
            before = root
        if not self._repository.checkpoint_gateway_publication_activation(
            self._publication_id, self._worker_id, before, recorded,
        ):
            raise RetryablePublicationError("Model activation checkpoint lease changed")
        self._checkpoint = checkpoint

    def begin_once(self, intent: ModelRouteActivationIntent) -> None:
        intent.require_valid()
        if self._checkpoint is not None and self.outcome != "expired_before_dispatch":
            raise ValueError("MODEL_ACTIVATION_ALREADY_STARTED_RECOVER_ONLY")
        if intent.head.publication_id != str(self._publication_id):
            raise ValueError("MODEL_ACTIVATION_PUBLICATION_CHANGED")
        previous = self.intent
        if previous is not None:
            if (intent.endpoint != previous.endpoint or intent.table != previous.table
                    or intent.receipt.operation_id == previous.receipt.operation_id
                    or intent.head.model_dump(exclude={"head"})
                    != previous.head.model_dump(exclude={"head"})
                    or intent.head.head.activated_epoch < previous.head.head.activated_epoch):
                raise ValueError("MODEL_ACTIVATION_RECOVERY_CONFIGURATION_CHANGED")
            intent.head.head.approval.require_same_configuration(previous.head.head.approval)
        self._save(_ActivationCheckpoint(
            context_sha256=self._context, intent=intent.to_record(), outcome="started",
        ))

    def record_outcome(self, operation_id: str, outcome: str) -> None:
        intent = self.intent
        if (intent is None or intent.receipt.operation_id != operation_id
            or self._checkpoint is None):
            raise ValueError("MODEL_ACTIVATION_OPERATION_CHANGED")
        previous = self._checkpoint.outcome
        if previous == outcome:
            return
        if previous in {"confirmed", "conditional_conflict", "expired_before_dispatch"} or (
            outcome not in {"confirmed", "conditional_conflict", "expired_before_dispatch",
                            "unknown_no_replay"}
        ) or previous != "started" and outcome in {
            "conditional_conflict", "expired_before_dispatch",
        }:
            raise ValueError("MODEL_ACTIVATION_OUTCOME_REGRESSION")
        self._save(_ActivationCheckpoint.model_validate({
            **self._checkpoint.model_dump(mode="json"), "outcome": outcome,
        }))


class _WriterOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    intent: dict[str, Any]
    outcome: Literal[
        "started", "expired_before_dispatch", "conditional_conflict", "unknown_no_replay",
        "confirmed", "conflict", "unknown", "not_applied_fenced",
    ]


class _WriterCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[_WriterOperation] = Field(default_factory=list, max_length=128)


class PersistentRouteWriterJournal:
    def __init__(
        self, repository: QueryRepository, publication_id: UUID, worker_id: str,
        context_sha256: str, checkpoint: object, heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._repository = repository
        self._publication_id = publication_id
        self._worker_id = worker_id
        self._heartbeat = heartbeat
        self._checkpoint = (
            _WriterCheckpoint.model_validate(checkpoint) if checkpoint is not None
            else _WriterCheckpoint(context_sha256=context_sha256)
        )
        if self._checkpoint.context_sha256 != context_sha256:
            raise ValueError("ROUTE_WRITER_CONTEXT_CHANGED")
        recorded_ids: set[str] = set()
        target: tuple[str, str, str] | None = None
        for entry in self._checkpoint.operations:
            intent = RouteWriterIntent.from_record(entry.intent)
            bound = (intent.endpoint, intent.table, intent.receipt.scope.partition)
            if (
                intent.receipt.operation_id in recorded_ids
                or intent.after.publication_id != str(publication_id)
                or target is not None and bound != target
            ):
                raise ValueError("ROUTE_WRITER_HISTORY_INVALID")
            recorded_ids.add(intent.receipt.operation_id)
            target = bound
        if any(
            entry.outcome not in {"confirmed", "not_applied_fenced", "expired_before_dispatch"}
            for entry in self._checkpoint.operations[:-1]
        ):
            raise ValueError("ROUTE_WRITER_UNRESOLVED_HISTORY")
        self._stored = self._checkpoint.model_dump(mode="json") if checkpoint is not None else None

    @property
    def intent(self) -> RouteWriterIntent | None:
        return (
            RouteWriterIntent.from_record(self._checkpoint.operations[-1].intent)
            if self._checkpoint.operations else None
        )

    @property
    def outcome(self) -> str | None:
        return self._checkpoint.operations[-1].outcome if self._checkpoint.operations else None

    def snapshot(self) -> dict[str, Any] | None:
        return deepcopy(self._stored)

    def _save(self, candidate: _WriterCheckpoint) -> None:
        if self._heartbeat is not None:
            self._heartbeat()
        recorded = candidate.model_dump(mode="json")
        if not self._repository.checkpoint_gateway_publication_writer(
            self._publication_id, self._worker_id, deepcopy(self._stored), deepcopy(recorded),
        ):
            raise RuntimeError("ROUTE_WRITER_CHECKPOINT_OR_LEASE_CHANGED")
        self._checkpoint = candidate
        self._stored = recorded

    def begin_once(self, intent: RouteWriterIntent) -> None:
        recorded = intent.to_record()
        if any(
            entry.intent["receipt"]["operation_id"] == intent.receipt.operation_id
            for entry in self._checkpoint.operations
        ):
            raise ValueError("ROUTE_WRITER_OPERATION_REUSED")
        if self.outcome not in {None, "confirmed", "not_applied_fenced", "expired_before_dispatch"}:
            raise ValueError("ROUTE_WRITER_UNKNOWN_RECOVER_ONLY")
        previous = self.intent
        if intent.after.publication_id != str(self._publication_id) or previous is not None and (
            intent.endpoint != previous.endpoint or intent.table != previous.table
            or intent.receipt.scope != previous.receipt.scope
        ):
            raise ValueError("ROUTE_WRITER_CONTEXT_CHANGED")
        self._save(_WriterCheckpoint.model_validate({
            **self._checkpoint.model_dump(mode="python"),
            "operations": [*self._checkpoint.operations,
                           _WriterOperation(intent=recorded, outcome="started")],
        }))

    def record_outcome(self, operation_id: str, outcome: str) -> None:
        intent = self.intent
        if intent is None or intent.receipt.operation_id != operation_id:
            raise ValueError("ROUTE_WRITER_OPERATION_CHANGED")
        previous = self.outcome
        if previous == outcome:
            return
        if previous in {"confirmed", "conflict", "not_applied_fenced", "expired_before_dispatch"}:
            raise ValueError("ROUTE_WRITER_OUTCOME_REGRESSION")
        if outcome == "started" or (
            previous != "started" and outcome in {"expired_before_dispatch", "conditional_conflict"}
        ):
            raise ValueError("ROUTE_WRITER_OUTCOME_REGRESSION")
        last = _WriterOperation.model_validate({"intent": intent.to_record(), "outcome": outcome})
        self._save(_WriterCheckpoint.model_validate({
            **self._checkpoint.model_dump(mode="python"),
            "operations": [*self._checkpoint.operations[:-1], last],
        }))

    def recover(
        self, projection: TableRouteProjection,
    ) -> Literal["confirmed", "conflict", "unknown", "not_applied_fenced"]:
        intent = self.intent
        if intent is None:
            raise ValueError("ROUTE_WRITER_INTENT_MISSING")
        if self._heartbeat is not None:
            self._heartbeat()
        outcome = projection.recover_writer(intent)
        self.record_outcome(intent.receipt.operation_id, outcome)
        return outcome


class RoutePublicationPort(Protocol):
    projection: TableRouteProjection

    @property
    def program_record(self) -> Mapping[str, Any]: ...

    def check(self, task: RoutePublicationTask) -> ProgramCapabilityProof: ...

    def validate_configuration(self, task: RoutePublicationTask) -> ProgramCapabilityProof: ...

    def probe_changed(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None, probe_operation: BudgetOperation | None = None,
    ) -> None: ...

    def recover_changed(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None, probe_operation: BudgetOperation | None = None,
    ) -> None: ...

    def probe_count(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None,
    ) -> None: ...

    def probe_unavailable(
        self, task: RoutePublicationTask, alias: str, journal: PersistentProbeJournal,
        *, member_backend_id: str | None = None, probe_operation: BudgetOperation | None = None,
    ) -> None: ...

    def confirm(self, task: RoutePublicationTask, head: ActiveRouteHead) -> None: ...

    def confirm_model(self, task: RoutePublicationTask, head: ModelRouteHead) -> None: ...


class RouteProbeStep(NamedTuple):
    key: str
    alias: str
    kind: Literal["inference", "count", "non-dispatch"]
    member_backend_id: str | None = None
    operation: BudgetOperation | None = None


def configuration_probe_steps(task: RoutePublicationTask) -> tuple[RouteProbeStep, ...]:
    if task.configuration_only:
        return ()
    routes = {route.alias: route for route in task.plan.snapshot.routes}
    result: list[RouteProbeStep] = []
    for alias in task.plan.changed_aliases:
        route = routes[alias]
        if alias in task.known_unavailable_bindings:
            members = ([member.backend_id for member in route.connection.pool.members]
                       if route.connection.pool is not None else [None])
            for member_id in members:
                for operation in route.operations:
                    result.append(RouteProbeStep(document_digest({
                        "context": task.context_sha256, "alias": alias, "kind": "non-dispatch",
                        "member_backend_id": member_id, "operation": operation,
                    }), alias, "non-dispatch", member_id, operation))
            continue
        if route.connection.pool is not None:
            for index, member in enumerate(route.connection.pool.members):
                identity = {
                    "context": task.context_sha256, "alias": alias,
                    "member_backend_id": member.backend_id,
                }
                for operation in route.operations:
                    result.append(RouteProbeStep(
                        document_digest({**identity, "kind": "inference", "operation": operation}),
                        alias, "inference", member.backend_id, operation,
                    ))
                if route.profile == "anthropic-messages" and (
                    index == 0 or route.connection.public_count_path is not None
                ):
                    result.append(RouteProbeStep(
                        document_digest({**identity, "kind": "count"}),
                        alias, "count", member.backend_id,
                    ))
            continue
        result.append(RouteProbeStep(
            document_digest({"context": task.context_sha256, "alias": alias}), alias, "inference",
        ))
        if route.profile == "anthropic-messages":
            result.append(RouteProbeStep(
                document_digest({"context": task.context_sha256, "alias": alias, "kind": "count"}),
                alias, "count",
            ))
    if len(result) > MAX_PROBE_STEPS:
        raise PolicyCompilationError("Route verification exceeds checkpoint capacity")
    return tuple(result)


class _RouteProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged: int = Field(default=0, ge=0, le=30000)
    verified: int = Field(default=0, ge=0, le=30000)


class _CommitApprovalCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval: ConfigurationApproval
    scopes: dict[str, dict[str, Any]] = Field(
        default_factory=dict, max_length=31, exclude_if=lambda value: not value,
    )


def _binding_route_origin(binding: GatewayModelBinding) -> str:
    address = urlsplit(str(binding.backend_url or ""))
    origin = address.scheme + "://" + address.netloc
    validate_origin(origin)
    if address.query or address.fragment:
        raise PolicyCompilationError("Fixed route connection URL cannot contain query or fragment")
    if address.path.rstrip("/"):
        validate_path(address.path.rstrip("/"))
    return origin


def binding_public_count_path(binding: GatewayModelBinding) -> str | None:
    capability = binding.count_tokens_operation
    if capability.support != "supported" or capability.backend_path is None:
        return None
    _binding_route_origin(binding)
    path = capability.backend_path
    if binding.backend_pool is None:
        path = urlsplit(str(binding.backend_url)).path.rstrip("/") + path
    validate_path(path)
    return path


def route_credential_source(binding: GatewayModelBinding) -> str:
    if binding.runtime_id is None or binding.named_value_name is None:
        raise PolicyCompilationError("Fixed route credential has no stable source")
    return document_digest({
        "runtime_id": str(binding.runtime_id), "auth_strategy": binding.auth_strategy.value,
        "named_value_name": binding.named_value_name,
        "key_vault_secret_id": (
            str(binding.key_vault_secret_id) if binding.key_vault_secret_id is not None else None
        ),
    })


def pool_member_bindings(binding: GatewayModelBinding) -> tuple[GatewayModelBinding, ...]:
    pool = binding.backend_pool
    if pool is None:
        return ()
    config = {name: value for name, value in binding.runtime_config.items() if name not in {
        "apim_backend_pool", "apim_backend_pool_session_affinity",
    }}
    return tuple(GatewayModelBinding.model_validate({
        **binding.model_dump(mode="python"), "runtime_id": member.runtime_id,
        "runtime_name": member.runtime_name, "backend_url": member.backend_url,
        "auth_strategy": member.auth_strategy, "named_value_name": member.named_value_name,
        "key_vault_secret_id": binding.key_vault_secret_id if (
            member.runtime_id == binding.runtime_id
            and member.named_value_name == binding.named_value_name
        ) else None,
        "managed_identity_resource": binding.managed_identity_resource
        if member.auth_strategy is AuthStrategy.MANAGED_IDENTITY else None,
        "runtime_config": config,
    }) for member in pool.members)


def pool_credential_name(binding: GatewayModelBinding, scope: RouteScope, secret_id: str) -> str:
    return "turnstile-route-key-" + document_digest({
        "scope": scope.model_dump(mode="json"),
        "source": route_credential_source(binding), "secret_id": secret_id,
    })[:40]


def pool_header_name(scope: RouteScope, value: str) -> str:
    return "turnstile-route-data-" + document_digest({
        "scope": scope.model_dump(mode="json"), "value": value,
    })[:40]


def _pool_matches_binding(
    binding: GatewayModelBinding, connection: ConnectionRevision,
    credential_bindings: Mapping[str, str],
) -> bool:
    pool = binding.backend_pool
    actual = connection.pool
    if pool is None:
        return actual is None
    if actual is None:
        return False
    versions: dict[str, str] = {}
    headers: dict[str, str] = {}
    for source in pool_member_bindings(binding):
        _, _, credential = _binding_route_contract(source, credential_bindings=credential_bindings)
        if connection.observer:
            for value in (urlsplit(_binding_route_origin(source)).netloc, source.runtime_name):
                headers[pool_header_name(connection.scope, value)] = value
        if credential.secret_id:
            versions[pool_credential_name(source, connection.scope, credential.secret_id)] = (
                credential.secret_id
            )
    cookie = "TurnstileAffinity-" + text_digest(
        connection.scope.target_id + ":" + binding.model.model_key,
    )[:16] if pool.session_affinity else None
    return (
        [(member.runtime_id, member.priority, member.weight) for member in actual.members]
        == [(str(member.runtime_id), member.priority, member.weight) for member in pool.members]
        and actual.rate_limit == pool.rate_limit and actual.session_cookie_name == cookie
        and actual.credential_versions == versions
        and actual.header_values == headers
    )


def binding_request_adapter(binding: GatewayModelBinding) -> RouteRequestAdapter | None:
    if (binding.provider_brand_key.value != "amazon_bedrock"
            or binding.api_format is not ApiFormat.ANTHROPIC_MESSAGES):
        return None
    _binding_route_origin(binding)
    path = urlsplit(str(binding.backend_url)).path.rstrip("/") + binding.backend_path.replace(
        "{upstream_model_id}", binding.model.upstream_model_id,
    )
    if (binding.streaming_mode.value != "buffered"
            or path != "/model/" + binding.model.upstream_model_id + "/invoke"):
        raise PolicyCompilationError("Fixed route Bedrock adapter requires buffered model invoke")
    return "bedrock-anthropic-v1"


def _binding_route_contract(
    binding: GatewayModelBinding, *, credential_bindings: Mapping[str, str] | None = None,
) -> tuple[RouteProfile, dict[BudgetOperation, str], RouteCredential]:
    profiles: dict[ApiFormat, RouteProfile] = {
        ApiFormat.OPENAI_CHAT: "openai-v1",
        ApiFormat.ANTHROPIC_MESSAGES: "anthropic-messages",
        ApiFormat.OPENAI_IMAGES: "openai-images",
        ApiFormat.MICROSOFT_DECISION: "microsoft-decision",
    }
    operations: dict[ApiFormat, BudgetOperation] = {
        ApiFormat.OPENAI_CHAT: "chat-completions",
        ApiFormat.ANTHROPIC_MESSAGES: "anthropic-messages",
        ApiFormat.OPENAI_IMAGES: "images-generations",
        ApiFormat.MICROSOFT_DECISION: "microsoft-decision",
    }
    paths: dict[BudgetOperation, str] = {operations[binding.api_format]: binding.backend_path}
    if (binding.api_format is ApiFormat.OPENAI_CHAT and binding.routing_managed
            and binding.backend_path.rstrip("/") == "/openai/v1/chat/completions"):
        paths["responses"] = "/openai/v1/responses"
        paths["responses-compact"] = "/openai/v1/responses/compact"
    _binding_route_origin(binding)
    prefix = urlsplit(str(binding.backend_url)).path.rstrip("/")
    if binding.backend_pool is not None:
        prefix = ""
    paths = {operation: prefix + path for operation, path in paths.items()}
    if binding_request_adapter(binding) == "bedrock-anthropic-v1":
        paths = {
            operation: path.replace("{upstream_model_id}", binding.model.upstream_model_id)
            for operation, path in paths.items()
        }
    for path in paths.values():
        validate_path(path)
    credential = RouteCredential(mode="none")
    if binding.auth_strategy is AuthStrategy.OAUTH_CLIENT_CREDENTIALS:
        if binding.oauth is None:
            raise PolicyCompilationError("Fixed route OAuth reference is unavailable")
        credential = RouteCredential(
            mode="oauth_client_credentials", header="Authorization", oauth=binding.oauth,
        )
    elif binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY:
        credential = RouteCredential(
            mode="managed_identity", header="Authorization",
            audience=binding.managed_identity_resource or "",
        )
    elif binding.auth_strategy in {
        AuthStrategy.NAMED_VALUE_API_KEY, AuthStrategy.NAMED_VALUE_BEARER,
    }:
        if binding.backend_pool is not None:
            return profiles[binding.api_format], paths, credential
        secret_id = str(binding.key_vault_secret_id) if binding.key_vault_secret_id else None
        if credential_bindings:
            materialized = credential_bindings.get(route_credential_source(binding))
            if secret_id is not None and materialized not in {None, secret_id}:
                raise PolicyCompilationError("Fixed route declared credential version changed")
            secret_id = secret_id or materialized
        if secret_id is None:
            raise PolicyCompilationError("Fixed route specification requires versioned credentials")
        bearer = binding.auth_strategy is AuthStrategy.NAMED_VALUE_BEARER
        credential = RouteCredential(
            mode="key_vault_bearer" if bearer else "key_vault_api_key",
            header="Authorization" if bearer else "x-api-key" if (
                binding.provider_brand_key.value == "microsoft_foundry"
                and binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
            ) else "api-key",
            secret_id=secret_id,
        )
    elif binding.auth_strategy is not AuthStrategy.NONE:
        raise PolicyCompilationError("Fixed route specification uses unsupported authentication")
    return profiles[binding.api_format], paths, credential


def require_route_specification(
    publication: GatewayPublication, task: RoutePublicationTask,
    *, scope_only: bool = False,
) -> None:
    addition = publication.resource_manifest.get("model_route_addition")
    if task.model_addition != (addition is not None):
        raise PolicyCompilationError("Model addition task mode changed")
    if addition is not None and (
        not isinstance(addition, dict)
        or set(addition) != {"version", "program_release_id", "alias"}
        or type(addition["version"]) is not int or addition["version"] != 1
        or publication.publication_kind.value != "model_add"
        or addition["program_release_id"] != str(publication.base_release_id)
        or len(publication.desired_spec.bindings) != 1
        or addition["alias"] != task.plan.snapshot.routes[0].alias
    ):
        raise PolicyCompilationError("Model addition intent changed")
    migration = publication.resource_manifest.get("fixed_route_migration")
    if migration is not None and not isinstance(migration, dict):
        raise PolicyCompilationError("Fixed route migration descriptor is invalid")
    migration_digest = document_digest(migration) if migration is not None else None
    if task.program_migration_sha256 != migration_digest:
        raise PolicyCompilationError("Fixed route migration task binding changed")
    if task.known_unavailable_bindings != (
        migration.get("known_unavailable_bindings", {}) if migration is not None else {}
    ):
        raise PolicyCompilationError("Known unavailable upstream task binding changed")
    routes = {route.alias: route for route in task.plan.snapshot.routes}
    bindings = {binding.model.model_key: binding for binding in publication.desired_spec.bindings}
    discovery = {model.id: model for model in publication.desired_spec.discovery_models}
    if task.model_addition:
        selected = {
            snapshot.native_gateway_id for binding in publication.desired_spec.bindings
            for snapshot in binding.serving_deployments
        } or {"managed"}
        actual = {current.plan.program.scope.gateway_id for current in task.scope_tasks}
        if (not actual <= selected or not scope_only and actual != selected):
            raise PolicyCompilationError("Fixed route selected Gateway scopes changed")
        for child in task.additional_scope_tasks:
            require_route_specification(publication, child, scope_only=True)
    if set(routes) != set(bindings) or set(routes) != set(discovery):
        raise PolicyCompilationError(
            "Fixed route snapshot differs from its publication specification",
        )
    for alias, binding in bindings.items():
        route = routes[alias]
        profile, paths, credential = _binding_route_contract(
            binding, credential_bindings=task.credential_bindings,
        )
        if (
            not _pool_matches_binding(binding, route.connection, task.credential_bindings)
            or binding.serving_deployments and all(
                deployment.native_gateway_id != task.plan.program.scope.gateway_id
                for deployment in binding.serving_deployments
            )
            or route.profile != profile or set(route.operations) != set(paths)
            or route.connection.origin != _binding_route_origin(binding)
            or route.connection.path != next(iter(paths.values()))
            or route.connection.public_count_path != binding_public_count_path(binding)
            or route.connection.credential != credential
            or any(route.connection.protocol_paths.get(name, route.connection.path) != path
                   for name, path in paths.items())
            or route.upstream_model != binding.model.upstream_model_id
            or (binding.logical_model_id is not None
                and route.model_id != str(binding.logical_model_id))
            or route.display_name != discovery[alias].display_name
            or route.provider_name != binding.provider_name
            or route.runtime_name != binding.runtime_name
            or route.context_window != (binding.model.context_window or 0)
            or route.assignment_required != binding.model.assignment_required
            or route.streaming_mode != binding.streaming_mode.value
            or route.image_profile != binding.model.image_profile
            or route.request_adapter != binding_request_adapter(binding)
        ):
            raise PolicyCompilationError(
                "Fixed route snapshot differs from its publication specification",
            )


def build_route_publication_task(
    publication: GatewayPublication, program: ProgramIdentity, proof: ProgramCapabilityProof,
    previous_head: ActiveRouteHead | None, previous_routes: Sequence[RouteContent],
    budgets: BudgetPublicationContracts, connections: Mapping[str, ConnectionApproval],
    storage_endpoint: str, storage_table: str, target_identity_sha256: str, now: int,
    *, previous_members: Sequence[RouteMember] = (),
    credential_bindings: Mapping[str, str] | None = None,
    configuration_only: bool = False,
    scope_only: bool = False,
) -> RoutePublicationTask:
    model_addition = "model_route_addition" in publication.resource_manifest
    if model_addition and (
        len(publication.desired_spec.bindings) != 1 or previous_routes or previous_members
        or previous_head is not None
    ):
        raise PolicyCompilationError("Model addition cannot build a full gateway snapshot")
    budgets.validate_publication(publication)
    discovery = {item.id: item for item in publication.desired_spec.discovery_models}
    previous = {route.alias: route for route in previous_routes}
    aliases = {binding.model.model_key for binding in publication.desired_spec.bindings}
    if (
        len(previous) != len(previous_routes) or set(discovery) != aliases
        or set(connections) != aliases
        or program.scope.target_id != str(publication.gateway_profile_id)
    ):
        raise PolicyCompilationError(
            "Fixed route specification has incomplete approved dependencies",
        )
    routes: list[RouteContent] = []
    approvals: dict[str, ConnectionApproval] = {}
    for binding in publication.desired_spec.bindings:
        alias = binding.model.model_key
        approved = ConnectionApproval.model_validate_json(canonical_document(connections[alias]))
        approved.require_current(approved.connection, now)
        revision = approved.connection.revision_id
        if revision in approvals and approvals[revision] != approved:
            raise PolicyCompilationError("Fixed route connection approvals conflict")
        approvals[revision] = approved
        profile, paths, _ = _binding_route_contract(
            binding, credential_bindings=credential_bindings,
        )
        budget = budgets.model_contracts[alias]
        routes.append(RouteContent(
            scope=program.scope, model_id=str(budget.model_id), alias=alias,
            display_name=discovery[alias].display_name,
            created_at=previous[alias].created_at if alias in previous else publication.created_at,
            upstream_model=binding.model.upstream_model_id, profile=profile,
            provider_name=binding.provider_name, runtime_name=binding.runtime_name,
            context_window=binding.model.context_window or 0,
            assignment_required=binding.model.assignment_required,
            streaming_mode=binding.streaming_mode.value, operations=tuple(paths),
            connection=approved.connection, budget_epoch=str(budgets.epoch), budget=budget,
            image_profile=binding.model.image_profile,
            request_adapter=binding_request_adapter(binding),
        ))
    raw_migration = publication.resource_manifest.get("fixed_route_migration")
    if raw_migration is not None and not isinstance(raw_migration, dict):
        raise PolicyCompilationError("Fixed route migration descriptor is invalid")
    version = str(uuid5(publication.id, "route-configuration"))
    _, manifest, dependencies = configuration_manifests(
        program.scope, version, routes, approvals, now,
    )
    approval = ConfigurationApproval(
        approval_id=str(uuid5(publication.id, "route-configuration-approval")), scope=program.scope,
        route_version=version,
        generation=previous_head.approval.generation + 1 if previous_head else 1,
        manifest_sha256=manifest, dependency_manifest_sha256=dependencies,
        catalog_sha256=catalog_digest(routes), entry_count=len(routes),
        required_program_sha256=program.program_sha256,
        required_profiles=tuple(sorted({route.profile for route in routes})),
        budget_epoch=str(budgets.epoch), issued_epoch=now, activate_before_epoch=now + 600,
    )
    snapshot = RouteSnapshot.prepare(approval, routes, approvals, now)
    task = RoutePublicationTask(
        model_addition=model_addition,
        configuration_only=configuration_only,
        publication_id=str(publication.id),
        base_publication_id=(
            str(publication.base_release_id) if publication.base_release_id else None
        ),
        desired_spec_sha256=publication.desired_spec_sha256,
        target_identity_sha256=target_identity_sha256,
        storage_endpoint=storage_endpoint, storage_table=storage_table,
        plan=RouteConfigurationPlan.prepare(
            previous_routes, snapshot, program, proof, str(budgets.epoch), now,
        ),
        previous_routes=tuple(previous_routes), previous_head=previous_head,
        previous_members=tuple(previous_members),
        credential_bindings=dict(credential_bindings or {}),
        program_migration_sha256=(
            document_digest(raw_migration) if raw_migration is not None else None
        ),
        known_unavailable_bindings=(
            raw_migration.get("known_unavailable_bindings", {}) if raw_migration is not None else {}
        ),
    )
    require_route_specification(publication, task, scope_only=scope_only)
    return task


class RouteConfigurationExecutor:
    def __init__(
        self, repository: QueryRepository,
        resolve: Callable[[RoutePublicationTask], AbstractContextManager[RoutePublicationPort]],
        *, clock: Callable[[], int] = lambda: int(time()), maximum_entities: int = 16,
        prepare: Callable[
            [GatewayPublication, str, Callable[[], None]], RoutePublicationTask
        ] | None = None,
        promote_program: Callable[
            [GatewayPublication, RoutePublicationTask, str, Callable[[], None]], None
        ] | None = None,
        remove_model: Callable[
            [GatewayPublication, str, Callable[[], None]], GatewayPublication
        ] | None = None,
    ) -> None:
        if type(maximum_entities) is not int or not 1 <= maximum_entities <= 100:
            raise ValueError("ROUTE_EXECUTOR_SLICE_INVALID")
        self._repository = repository
        self._resolve = resolve
        self._clock = clock
        self._maximum_entities = maximum_entities
        self._prepare = prepare
        self._promote_program = promote_program
        self._remove_model = remove_model

    def __call__(
        self, publication: GatewayPublication, worker_id: str, heartbeat: Callable[[], None],
    ) -> GatewayPublication:
        if "model_route_removal" in publication.resource_manifest:
            if self._remove_model is None:
                raise PolicyCompilationError("Independent model removal executor is unavailable")
            return self._remove_model(publication, worker_id, heartbeat)
        if ("fixed_route_migration" in publication.resource_manifest
                and self._promote_program is None):
            raise PolicyCompilationError("Fixed route program migration executor is unavailable")
        if "route_configuration" not in publication.resource_manifest:
            if publication.status.value != "queued" or self._prepare is None:
                raise PolicyCompilationError("Fixed route configuration preparation is unavailable")
            heartbeat()
            prepared = self._prepare(publication, worker_id, heartbeat)
            task = RoutePublicationTask.model_validate_json(canonical_document(prepared))
            task.require_publication(
                str(publication.id), str(publication.base_release_id) if publication.base_release_id
                else None, publication.desired_spec_sha256, str(publication.gateway_profile_id),
            )
            require_route_specification(publication, task)
            heartbeat()
            latest_row = self._repository.get_gateway_publication(publication.id)
            if latest_row is None:
                raise RetryablePublicationError("Route prepared publication is unavailable")
            latest = GatewayPublication.model_validate(latest_row)
            if (latest.id != publication.id or latest.status.value != "queued"
                    or latest.desired_spec != publication.desired_spec
                    or latest.desired_spec_sha256 != publication.desired_spec_sha256
                    or latest.base_release_id != publication.base_release_id):
                raise PolicyCompilationError("Route publication changed during preparation")
            row = self._repository.transition_gateway_publication(
                publication.id, "queued", "queued", {"resource_manifest": {
                    **latest.resource_manifest,
                    "route_configuration": task.model_dump(mode="json"),
                    "route_last_progress_attempt": publication.attempt_count,
                }}, worker_id,
            )
            if row is None:
                raise RetryablePublicationError("Route preparation lease changed")
            return GatewayPublication.model_validate(row)
        raw = publication.resource_manifest.get("route_configuration")
        if not isinstance(raw, dict):
            raise PolicyCompilationError("Fixed route configuration task is missing")
        task = RoutePublicationTask.model_validate_json(canonical_document(raw))
        task.require_publication(
            str(publication.id), str(publication.base_release_id) if publication.base_release_id
            else None, publication.desired_spec_sha256, str(publication.gateway_profile_id),
        )
        require_route_specification(publication, task)
        progress = _RouteProgress.model_validate(publication.resource_manifest.get(
            "route_progress", {"context_sha256": task.context_sha256},
        ))
        total = 3 * sum(len(scoped.plan.snapshot.routes) for scoped in task.scope_tasks)
        if (progress.context_sha256 != task.context_sha256
                or not 0 <= progress.verified <= progress.staged <= total):
            raise PolicyCompilationError("Route projection progress changed")
        if publication.status.value == "promoting":
            verified_snapshot = PublicationProbeProgress(
                task.context_sha256, publication.resource_manifest.get("probe_verification"),
                lambda *_: None,
            ).snapshot()
            expected_steps = {
                step.key: "passed" for step in configuration_probe_steps(task)
            }
            if (
                progress.verified != total or verified_snapshot["steps"] != expected_steps
                or publication.apim_revision != task.plan.program.api_revision
                or publication.policy_sha256 != task.plan.program.program_sha256
                or publication.resource_manifest.get("apim_target_identity_sha256")
                != task.target_identity_sha256
            ):
                raise PolicyCompilationError("Route publication verification is incomplete")
        heartbeat()
        with self._resolve(task) as port:
            if publication.status.value == "promoting":
                return self._activate(publication, task, port, worker_id, heartbeat)
            proof = (port.validate_configuration(task) if task.configuration_only
                     and publication.status.value == "verifying" else port.check(task))
            instant = self._clock()
            head = ActiveRouteHead(
                approval=task.plan.snapshot.approval, activated_epoch=task.plan.planned_epoch,
            )
            head.require_ready(
                task.plan.program, proof, task.plan.snapshot.approval.budget_epoch,
                instant,
            )
            phase = publication.status.value
            updates: dict[str, Any] = {}
            manifest = dict(publication.resource_manifest)
            progressed = False
            if phase == "queued":
                updates.update(apim_revision=task.plan.program.api_revision,
                               policy_sha256=task.plan.program.program_sha256)
                manifest["apim_target_identity_sha256"] = task.target_identity_sha256
                manifest["fixed_route_program"] = dict(port.program_record)
                budgets = BudgetPublicationContracts(
                    publication_id=publication.id,
                    gateway_profile_id=publication.gateway_profile_id,
                    epoch=UUID(task.plan.snapshot.approval.budget_epoch),
                    release_spec_sha256=publication.desired_spec_sha256,
                    binding_sha256={
                        binding.model.model_key: BudgetPublicationContracts.binding_digest(binding)
                        for binding in publication.desired_spec.bindings
                    },
                    model_contracts={
                        route.alias: route.budget for route in task.plan.snapshot.routes
                    },
                )
                budgets.validate_publication(publication)
                manifest["budget_contracts"] = budgets.model_dump(mode="json")
                progressed = True
                phase = "provisioning" if task.configuration_only else "validating"
            elif phase == "validating":
                progressed = True
                phase = "provisioning"
            elif task.model_addition and phase in {"provisioning", "building_revision"}:
                port.projection.stage_model_addition(task.addition(), instant)
                for child in task.additional_scope_tasks:
                    with self._resolve(child) as child_port:
                        child_port.check(child)
                        child_port.projection.stage_model_addition(child.addition(), self._clock())
                progress = progress.model_copy(update={"staged": total, "verified": total})
                progressed = True
                phase = "verifying"
            elif phase in {"provisioning", "building_revision"}:
                staging = phase == "provisioning"
                cursor = progress.staged if staging else progress.verified
                advance = (
                    port.projection.stage_slice if staging
                    else port.projection.verify_slice_recorded
                )
                end = advance(task.plan.snapshot, instant, cursor, maximum=self._maximum_entities)
                if not cursor <= end <= min(total, cursor + self._maximum_entities):
                    raise PolicyCompilationError("Route projection returned an invalid cursor")
                progressed = end > cursor or end == total
                progress = _RouteProgress.model_validate({
                    **progress.model_dump(), "staged" if staging else "verified": end,
                })
                if end == total:
                    phase = "building_revision" if staging else "verifying"
            elif phase == "verifying":
                if progress.verified != total:
                    raise PolicyCompilationError("Route snapshot readback is incomplete")
                for child in task.additional_scope_tasks:
                    with self._resolve(child) as child_port:
                        child_port.validate_configuration(child)
                steps = configuration_probe_steps(task)
                routes = {route.alias: route for route in task.plan.snapshot.routes}
                maximum_requests = sum(
                    len(routes[step.alias].operations)
                    if step.kind == "inference" and step.operation is None else 1
                    for step in steps
                )

                def persist(expected: dict[str, Any] | None, checkpoint: dict[str, Any]) -> None:
                    heartbeat()
                    if not self._repository.checkpoint_gateway_publication_probes(
                        publication.id, worker_id, expected, checkpoint,
                    ):
                        raise RetryablePublicationError("Route verification checkpoint changed")

                verification = PublicationProbeProgress(
                    task.context_sha256, manifest.get("probe_verification"), persist,
                )
                previous_probes = verification.snapshot()
                authorization = ImageProbeAuthorization.model_validate(manifest.get(
                    "image_probe_authorization", {"id": str(publication.id)},
                ))
                journal = PersistentProbeJournal(
                    self._repository, f"publication:{publication.id}", authorization.id,
                    attempt_limit=authorization.attempt_limit, heartbeat=heartbeat,
                    verification=verification, plan_context={"route_task": task.context_sha256},
                    request_recorder=lambda method, endpoint: self._record_probe(
                        publication, worker_id, maximum_requests, method, endpoint,
                    ),
                )
                try:
                    for step in steps:
                        heartbeat()
                        options: dict[str, Any] = {}
                        if step.member_backend_id is not None:
                            options["member_backend_id"] = step.member_backend_id
                            if step.kind == "inference":
                                options["probe_operation"] = step.operation
                        if step.kind == "non-dispatch":
                            options["probe_operation"] = step.operation
                        verify_probe = (
                            port.probe_unavailable if step.kind == "non-dispatch"
                            else port.probe_count if step.kind == "count" else port.probe_changed
                        )
                        recovery = (
                            partial(port.recover_changed, task, step.alias, journal, **options)
                            if step.kind == "inference" else None
                        )
                        verification.run_step(
                            step.key, partial(verify_probe, task, step.alias, journal, **options),
                            recover=recovery,
                        )
                except ProbeVerificationDeferred:
                    pass
                else:
                    phase = "promoting"
                manifest["probe_verification"] = verification.snapshot()
                progressed = phase == "promoting" or previous_probes != verification.snapshot()
            else:
                raise PolicyCompilationError("Unsupported fixed route publication phase")
            manifest["route_progress"] = progress.model_dump(mode="json")
            if progressed:
                manifest["route_last_progress_attempt"] = publication.attempt_count
            updates["resource_manifest"] = manifest
            heartbeat()
            row = self._repository.transition_gateway_publication(
                publication.id, publication.status.value, phase, updates, worker_id,
            )
            if row is None:
                raise RetryablePublicationError("Route publication lease changed")
            return GatewayPublication.model_validate(row)

    def _record_probe(
        self, publication: GatewayPublication, worker_id: str, maximum_requests: int,
        method: str, endpoint: str,
    ) -> None:
        if not self._repository.reserve_gateway_publication_probe(
            publication.id, worker_id, maximum_requests, method,
            document_digest({"endpoint": endpoint}),
        ):
            raise PolicyCompilationError("Route verification request exceeds its publication plan")

    def _commit_snapshot(
        self, publication: GatewayPublication, task: RoutePublicationTask,
        port: RoutePublicationPort, worker_id: str, heartbeat: Callable[[], None],
        *, renew_before_dispatch: bool = False, scope_partition: str | None = None,
    ) -> RouteSnapshot:
        root = publication.resource_manifest.get("route_commit_approval")
        if root is not None and not isinstance(root, dict):
            raise PolicyCompilationError("Route commit approval checkpoint is invalid")
        raw = root
        if scope_partition is not None:
            raw = (root or {}).get("scopes", {}).get(scope_partition)
        if raw is not None:
            if not isinstance(raw, dict):
                raise PolicyCompilationError("Route commit approval checkpoint is invalid")
            checkpoint = _CommitApprovalCheckpoint.model_validate_json(canonical_document(raw))
            if checkpoint.context_sha256 != task.context_sha256:
                raise PolicyCompilationError("Route commit approval context changed")
            checkpoint.approval.require_same_configuration(task.plan.snapshot.approval)
            if (not renew_before_dispatch
                    or self._clock() < checkpoint.approval.activate_before_epoch):
                return task.plan.snapshot.model_copy(update={"approval": checkpoint.approval})
        if raw is None or renew_before_dispatch:
            activation = publication.resource_manifest.get("route_activation")
            if activation is not None and not isinstance(activation, dict):
                raise PolicyCompilationError("Route activation checkpoint is invalid")
            if scope_partition is not None:
                activation = (activation or {}).get("scopes", {}).get(scope_partition)
            if raw is None and activation is not None:
                raise PolicyCompilationError("Route activation has no commit approval checkpoint")
            proof = port.check(task)
            instant = self._clock()
            approval = ConfigurationApproval.model_validate({
                **task.plan.snapshot.approval.model_dump(mode="python"),
                "approval_id": str(uuid5(publication.id, "route-commit-approval")),
                "issued_epoch": instant, "activate_before_epoch": instant + 600,
            })
            approval.require_same_configuration(task.plan.snapshot.approval)
            ActiveRouteHead(approval=approval, activated_epoch=instant).require_ready(
                task.plan.program, proof, approval.budget_epoch, instant, activating=True,
            )
            checkpoint = _CommitApprovalCheckpoint(
                context_sha256=task.context_sha256, approval=approval,
            )
            expected = raw
            recorded = checkpoint.model_dump(mode="json")
            if scope_partition is not None:
                latest = self._repository.get_gateway_publication(publication.id)
                root = (latest or {}).get("resource_manifest", {}).get("route_commit_approval")
                if not isinstance(root, dict) or root.get("scopes", {}).get(scope_partition) != raw:
                    raise RetryablePublicationError("Route scope approval checkpoint changed")
                expected = root
                recorded = {**root, "scopes": {**root.get("scopes", {}), scope_partition: recorded}}
            heartbeat()
            if not self._repository.checkpoint_gateway_publication_approval(
                publication.id, worker_id, expected, recorded,
            ):
                raise RetryablePublicationError("Route commit approval checkpoint changed")
        return task.plan.snapshot.model_copy(update={"approval": checkpoint.approval})

    def _activate(
        self, publication: GatewayPublication, task: RoutePublicationTask,
        port: RoutePublicationPort,
        worker_id: str, heartbeat: Callable[[], None],
    ) -> GatewayPublication:
        if task.model_addition:
            return self._activate_model(publication, task, port, worker_id, heartbeat)
        projection = port.projection
        snapshot = self._commit_snapshot(publication, task, port, worker_id, heartbeat)
        activation = PersistentRouteActivationJournal(
            self._repository, publication.id, worker_id, task.context_sha256,
            publication.resource_manifest.get("route_activation"), heartbeat,
        )
        writer_journal = PersistentRouteWriterJournal(
            self._repository, publication.id, worker_id, task.context_sha256,
            publication.resource_manifest.get("route_writer"), heartbeat,
        )
        try:
            if activation.intent is not None:
                outcome = activation.recover(projection)
                if outcome == "unknown":
                    if writer_journal.intent is not None and writer_journal.outcome not in {
                        "confirmed", "not_applied_fenced", "expired_before_dispatch",
                    }:
                        writer_journal.recover(projection)
                    if writer_journal.outcome not in {
                        "confirmed", "not_applied_fenced", "expired_before_dispatch",
                    }:
                        raise RetryablePublicationError("Route writer requires receipt recovery")
                    current = projection.read_writer()
                    if current is None or self._clock() < current[0].lease_until_epoch:
                        raise RetryablePublicationError(
                            "Route Storage takeover is not yet available",
                        )
                    heartbeat()
                    projection.takeover_writer(
                        current, str(uuid4()), str(publication.id), str(uuid4()),
                        self._clock, writer_journal,
                    )
                    outcome = activation.recover(projection)
                if outcome == "not_applied_fenced":
                    original = activation.intent
                    if original is None or original.writer_before is None:
                        raise PolicyCompilationError("Route activation writer evidence is missing")
                    if writer_journal.outcome not in {
                        "confirmed", "not_applied_fenced", "expired_before_dispatch",
                    } and writer_journal.recover(projection) != "confirmed":
                        raise RetryablePublicationError("Route abort requires receipt recovery")
                    current = projection.read_writer()
                    if current is None:
                        raise RetryablePublicationError("Route abort writer is unavailable")
                    pending = original.writer_before.pending
                    if current[0].pending is not None:
                        if pending is None or current[0].pending != pending:
                            raise PolicyCompilationError("Route abort pending intent changed")
                        if self._clock() >= current[0].lease_until_epoch:
                            heartbeat()
                            current = projection.takeover_writer(
                                current, str(uuid4()), str(publication.id), str(uuid4()),
                                self._clock, writer_journal,
                            )
                        heartbeat()
                        projection.abort_writer(
                            current, pending, current[0].owner_token, str(publication.id),
                            str(uuid4()), self._clock, writer_journal,
                        )
                    raise RouteActivationNotApplied(
                        "Route activation was not applied; a new approval is required",
                    )
                if outcome != "confirmed":
                    raise RetryablePublicationError("Route activation requires receipt recovery")
            else:
                if (
                    writer_journal.intent is not None and writer_journal.outcome not in {
                        "confirmed", "not_applied_fenced", "expired_before_dispatch",
                    } and writer_journal.recover(projection) not in {
                        "confirmed", "not_applied_fenced",
                    }
                ):
                    raise RetryablePublicationError("Route writer requires receipt recovery")
                if self._clock() >= snapshot.approval.activate_before_epoch:
                    expired_writer = projection.read_writer()
                    if expired_writer is not None and expired_writer[0].pending is not None:
                        pending = expired_writer[0].pending
                        if (
                            expired_writer[0].publication_id != str(publication.id)
                            or pending.approval != snapshot.approval
                            or pending.snapshot_sha256 != document_digest(snapshot)
                        ):
                            raise PolicyCompilationError("Expired route pending ownership changed")
                        if self._clock() < expired_writer[0].lease_until_epoch:
                            raise RetryablePublicationError("Expired route writer is still leased")
                        heartbeat()
                        current = projection.takeover_writer(
                            expired_writer, str(uuid4()), str(publication.id), str(uuid4()),
                            self._clock, writer_journal,
                        )
                        heartbeat()
                        projection.abort_writer(
                            current, pending, current[0].owner_token, str(publication.id),
                            str(uuid4()), self._clock, writer_journal,
                        )
                    raise RouteActivationNotApplied(
                        "Route activation approval expired; a new approval is required",
                    )
                active = projection.read_active()
                if (active[0] if active is not None else None) != task.previous_head:
                    raise PolicyCompilationError("Route base configuration changed")
                proof = port.check(task)
                current = projection.read_writer()
                owner_token = str(uuid4())
                heartbeat()
                if current is None:
                    current = projection.claim_writer(
                        owner_token, str(publication.id), str(uuid4()), self._clock, writer_journal,
                    )
                else:
                    if self._clock() < current[0].lease_until_epoch:
                        raise RetryablePublicationError(
                            "Route Storage writer lease is still active",
                        )
                    current = projection.takeover_writer(
                        current, owner_token, str(publication.id), str(uuid4()),
                        self._clock, writer_journal,
                    )
                operation_id = str(uuid5(publication.id, "activation:" + task.context_sha256))
                pending = RoutePendingActivation(
                    operation_id=operation_id,
                    base_generation=snapshot.approval.generation - 1,
                    approval=snapshot.approval,
                    snapshot_sha256=document_digest(snapshot),
                )
                if current[0].pending is None:
                    current = projection.prepare_writer(
                        current, pending, owner_token, str(publication.id), str(uuid4()),
                        self._clock, writer_journal,
                    )
                elif current[0].pending != pending:
                    raise PolicyCompilationError("Another route intent requires explicit recovery")
                heartbeat()
                proof = port.check(task)
                projection.activate(
                    snapshot, task.plan.program, proof, snapshot.approval.budget_epoch,
                    operation_id, active[1] if active is not None else None, self._clock,
                    activation, writer=current,
                )
        except RouteProjectionUnknown as error:
            raise RetryablePublicationError(
                "Route transition outcome unknown; recover only",
            ) from error
        intent = activation.intent
        if intent is None:
            raise PolicyCompilationError("Route activation intent was not recorded")
        current_head = projection.read_active()
        if current_head is None or current_head[0] != intent.head:
            raise PolicyCompilationError("Route activation was superseded before confirmation")
        port.check(task)
        port.confirm(task, intent.head)
        if "fixed_route_migration" in publication.resource_manifest:
            if self._promote_program is None:
                raise PolicyCompilationError(
                    "Fixed route program migration executor is unavailable",
                )
            self._promote_program(publication, task, worker_id, heartbeat)
        heartbeat()
        return GatewayPublication.model_validate(
            self._repository.activate_gateway_publication(publication.id, worker_id),
        )

    def _activate_model(
        self, publication: GatewayPublication, task: RoutePublicationTask,
        port: RoutePublicationPort, worker_id: str, heartbeat: Callable[[], None],
    ) -> GatewayPublication:
        ports = [(task, port)]
        with ExitStack() as readers:
            for child in task.additional_scope_tasks:
                ports.append((child, readers.enter_context(self._resolve(child))))
            if task.additional_scope_tasks:
                for scoped, scoped_port in ports:
                    scoped_port.check(scoped)
            for index, (scoped, scoped_port) in enumerate(ports):
                current = publication
                if index:
                    latest = self._repository.get_gateway_publication(publication.id)
                    if latest is None or latest["status"] != "promoting":
                        raise RetryablePublicationError(
                            "Model scope activation publication changed",
                        )
                    current = GatewayPublication.model_validate(latest)
                self._activate_model_scope(
                    current, scoped, scoped_port,
                    worker_id, heartbeat,
                    scope_partition=scoped.plan.program.scope.partition if index else None,
                )
        heartbeat()
        return GatewayPublication.model_validate(
            self._repository.activate_gateway_publication(
                publication.id, worker_id,
                validate_model_activation=require_model_addition_activation,
            ),
        )

    def _activate_model_scope(
        self, publication: GatewayPublication, task: RoutePublicationTask,
        port: RoutePublicationPort, worker_id: str, heartbeat: Callable[[], None],
        *, scope_partition: str | None = None,
    ) -> ModelRouteHead:
        checkpoint = publication.resource_manifest.get("route_activation")
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise PolicyCompilationError("Model activation checkpoint is invalid")
        if scope_partition is not None:
            checkpoint = (checkpoint or {}).get("scopes", {}).get(scope_partition)
        journal = PersistentModelRouteActivationJournal(
            self._repository, publication.id, worker_id, task.context_sha256,
            checkpoint, heartbeat, scope_partition=scope_partition,
        )
        intent = journal.intent
        operation_id = str(uuid5(publication.id, "model-route-activation"))
        renew_before_dispatch = journal.outcome == "expired_before_dispatch"
        if intent is not None and renew_before_dispatch:
            heartbeat()
            if (port.projection.recover_model_addition(intent) != "unknown_no_replay"
                    or port.projection.read_model_active(intent.head.alias) is not None):
                raise PolicyCompilationError("Unsent model activation has unexpected live state")
            operation_id = str(uuid5(publication.id,
                                    "model-route-activation:" + intent.receipt.operation_id))
            intent = None
        snapshot = self._commit_snapshot(
            publication, task, port, worker_id, heartbeat,
            renew_before_dispatch=renew_before_dispatch, scope_partition=scope_partition,
        )
        addition = ModelRouteAddition(
            publication_id=task.publication_id, program=task.plan.program, snapshot=snapshot,
        )
        if intent is None:
            proof = port.check(task)
            try:
                port.projection.activate_model_addition(
                    addition, proof, snapshot.approval.budget_epoch,
                    operation_id, self._clock, journal,
                )
            except RouteProjectionUnknown as error:
                raise RetryablePublicationError(
                    "Model activation requires receipt recovery",
                ) from error
            intent = journal.intent
        else:
            outcome = port.projection.recover_model_addition(intent)
            journal.record_outcome(intent.receipt.operation_id, outcome)
            if outcome != "confirmed":
                raise RetryablePublicationError("Model activation requires receipt recovery")
        if intent is None:
            raise PolicyCompilationError("Model activation intent was not recorded")
        intent.head.head.approval.require_same_configuration(snapshot.approval)
        port.confirm_model(task, intent.head)
        return intent.head