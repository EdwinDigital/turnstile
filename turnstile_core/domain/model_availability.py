from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, HttpUrl, StrictBool, model_validator

from .models import StrictModel

if TYPE_CHECKING:
    from .control_plane import GatewayPublication

AVAILABILITY_PARTITION = "model-availability|v1"
AVAILABILITY_NODE_PARTITION = "model-availability-nodes|v1"
AVAILABILITY_CONFIRMATION_PARTITION = "model-availability-confirmation|v1"
MAX_NODE_LEASE_SECONDS = 120
MAX_CATALOG_MODELS = 500
MAX_AVAILABILITY_REVISION = 2_147_483_647
AvailabilityRevision = Annotated[int, Field(strict=True, ge=1, le=MAX_AVAILABILITY_REVISION)]
AvailabilityDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
AvailabilityStatus = Literal["pending", "projected", "partial", "succeeded", "failed", "unknown"]


class ModelAvailabilityConflict(ValueError):
    pass


class ModelAvailabilityUnavailable(RuntimeError):
    pass


class _AvailabilityContract(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelAvailabilityCommand(_AvailabilityContract):
    enabled: StrictBool
    expected_revision: AvailabilityRevision
    idempotency_key: UUID


class ModelAvailabilityRetry(_AvailabilityContract):
    expected_revision: AvailabilityRevision


class ModelAvailabilityState(_AvailabilityContract):
    model_id: UUID
    revision: AvailabilityRevision
    enabled: StrictBool
    lifecycle: Literal["staged", "active", "deleted"] = "active"

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ModelAvailabilityState:
        if self.lifecycle == "deleted" and self.enabled:
            raise ValueError("A deleted model cannot be available")
        return self

    def entity(self) -> dict[str, Any]:
        return {
            "PartitionKey": AVAILABILITY_PARTITION,
            "RowKey": str(self.model_id),
            "SchemaVersion": 1,
            "ModelId": str(self.model_id),
            "Revision": self.revision,
            "Enabled": self.enabled,
            "Lifecycle": self.lifecycle,
        }

    @classmethod
    def from_entity(
        cls,
        entity: Mapping[str, Any],
        model_id: UUID,
    ) -> ModelAvailabilityState:
        if (
            entity.get("PartitionKey") != AVAILABILITY_PARTITION
            or entity.get("RowKey") != str(model_id)
            or entity.get("ModelId") != str(model_id)
            or type(entity.get("SchemaVersion")) is not int
            or entity["SchemaVersion"] != 1
        ):
            raise ModelAvailabilityUnavailable("Model availability identity or schema is invalid")
        return cls(
            model_id=model_id,
            revision=entity["Revision"],
            enabled=entity["Enabled"],
            lifecycle=entity["Lifecycle"],
        )


class ModelAvailabilityCoverage(_AvailabilityContract):
    target_id: UUID
    inventory_digest: AvailabilityDigest
    policy_digest: AvailabilityDigest
    serving_surfaces: tuple[str, ...] = Field(min_length=1, max_length=2000)
    verified_surfaces: tuple[str, ...] = Field(min_length=1, max_length=2000)
    verified_at: AwareDatetime
    expires_at: AwareDatetime
    read_contract: Literal["primary_no_cache"] = "primary_no_cache"

    @model_validator(mode="after")
    def validate_coverage(self) -> ModelAvailabilityCoverage:
        for surfaces in (self.serving_surfaces, self.verified_surfaces):
            if len(set(surfaces)) != len(surfaces) or any(
                not surface.strip() or len(surface) > 512 or "*" in surface for surface in surfaces
            ):
                raise ValueError("Availability coverage must identify every distinct surface")
        if set(self.serving_surfaces) != set(self.verified_surfaces):
            raise ValueError("Availability enforcement coverage is incomplete")
        lifetime = (self.expires_at - self.verified_at).total_seconds()
        if not 0 < lifetime <= 300:
            raise ValueError("Availability capability evidence must expire within five minutes")
        return self

    def current(self, now: datetime, inventory_digest: str) -> bool:
        return (
            self.verified_at <= now < self.expires_at and self.inventory_digest == inventory_digest
        )


def _exact_https(endpoint: HttpUrl) -> bool:
    return (
        endpoint.scheme == "https"
        and endpoint.username is None
        and endpoint.password is None
        and endpoint.query is None
        and endpoint.fragment is None
        and ";rev=" not in (endpoint.path or "")
    )


class ModelAvailabilityNodeEndpoint(_AvailabilityContract):
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
    endpoint_url: HttpUrl

    @model_validator(mode="after")
    def validate_endpoint(self) -> ModelAvailabilityNodeEndpoint:
        if self.instance_id.casefold() == "managed" or not _exact_https(self.endpoint_url):
            raise ValueError("Node endpoints require an exact HTTPS self-hosted identity")
        return self


class ModelAvailabilityNodeEnrollment(_AvailabilityContract):
    enrollment_id: UUID
    target_id: UUID
    deployment_id: UUID
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    native_gateway_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    revision: AvailabilityRevision
    controller_evidence_sha256: AvailabilityDigest
    fenced_installation_sha256: AvailabilityDigest
    rejoin_contract: Literal["enrolled_nodes_only_guarded_bootstrap"]
    enrolled_at: AwareDatetime
    nodes: tuple[ModelAvailabilityNodeEndpoint, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_nodes(self) -> ModelAvailabilityNodeEnrollment:
        if self.native_gateway_id.casefold() == "managed":
            raise ValueError("Managed gateways do not use a self-hosted enrollment")
        identities = {node.instance_id for node in self.nodes}
        endpoints = {str(node.endpoint_url).rstrip("/") for node in self.nodes}
        if len(identities) != len(self.nodes) or len(endpoints) != len(self.nodes):
            raise ValueError("Node enrollment must identify distinct nodes and direct endpoints")
        return self

    def digest(self) -> str:
        content = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode()).hexdigest()


class ModelAvailabilityNodeLease(_AvailabilityContract):
    target_id: UUID
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    native_gateway_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
    capability_digest: AvailabilityDigest
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_lease(self) -> ModelAvailabilityNodeLease:
        if self.native_gateway_id == "managed" or self.instance_id == "managed":
            raise ValueError("Node leases require a distinct self-hosted identity")
        if not 0 < (self.expires_at - self.issued_at).total_seconds() <= MAX_NODE_LEASE_SECONDS:
            raise ValueError("Node verification leases must expire within two minutes")
        return self

    @property
    def partition_key(self) -> str:
        return f"{AVAILABILITY_NODE_PARTITION}|{self.target_id}|{self.api_id}"

    @property
    def row_key(self) -> str:
        identity = json.dumps(
            [self.revision, self.native_gateway_id, self.instance_id],
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    def current(self, now: datetime, capability_digest: str) -> bool:
        return (
            self.issued_at <= now < self.expires_at and self.capability_digest == capability_digest
        )

    def entity(self) -> dict[str, Any]:
        return {
            "PartitionKey": self.partition_key,
            "RowKey": self.row_key,
            "SchemaVersion": 1,
            "TargetId": str(self.target_id),
            "ApiId": self.api_id,
            "ApiRevision": self.revision,
            "NativeGatewayId": self.native_gateway_id,
            "InstanceId": self.instance_id,
            "CapabilityDigest": self.capability_digest,
            "IssuedEpoch": str(int(self.issued_at.timestamp())),
            "ExpiresEpoch": str(int(self.expires_at.timestamp())),
        }

    @classmethod
    def from_entity(cls, entity: Mapping[str, Any]) -> ModelAvailabilityNodeLease:
        epochs = []
        for key in ("IssuedEpoch", "ExpiresEpoch"):
            value = entity.get(key)
            if (
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 12
                or str(int(value)) != value
            ):
                raise ModelAvailabilityUnavailable("Node lease timestamp is invalid")
            epochs.append(datetime.fromtimestamp(int(value), UTC))
        lease = cls(
            target_id=entity["TargetId"],
            api_id=entity["ApiId"],
            revision=entity["ApiRevision"],
            native_gateway_id=entity["NativeGatewayId"],
            instance_id=entity["InstanceId"],
            capability_digest=entity["CapabilityDigest"],
            issued_at=epochs[0],
            expires_at=epochs[1],
        )
        if (
            type(entity.get("SchemaVersion")) is not int
            or entity["SchemaVersion"] != 1
            or entity.get("PartitionKey") != lease.partition_key
            or entity.get("RowKey") != lease.row_key
        ):
            raise ModelAvailabilityUnavailable("Node lease identity or schema is invalid")
        return lease


class ModelAvailabilityCatalogCheck(_AvailabilityContract):
    endpoint_url: HttpUrl
    revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    route: Literal["/models", "/v1/models"]
    gateway_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
    models: dict[str, UUID] = Field(max_length=MAX_CATALOG_MODELS)
    capability_digest: AvailabilityDigest
    admission_surfaces: dict[str, AvailabilityDigest] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_check(self) -> ModelAvailabilityCatalogCheck:
        if (
            not _exact_https(self.endpoint_url)
            or (self.gateway_id == "managed") != (self.instance_id == "managed")
            or len({alias.casefold() for alias in self.models}) != len(self.models)
            or any(not alias.strip() for alias in self.models)
            or any(
                not route.startswith("/") or any(part in route for part in ("..", "?", "#", "{"))
                for route in self.admission_surfaces
            )
        ):
            raise ValueError("Confirmation requires an exact HTTPS catalog and model scope")
        return self

    @property
    def surface_prefix(self) -> str:
        if self.gateway_id == "managed":
            return f"managed/{self.revision}"
        return f"{self.gateway_id}/{self.instance_id}/{self.revision}"


class ModelAvailabilityConfirmationPlan(_AvailabilityContract):
    resource_id: str = Field(min_length=1, max_length=2048)
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    gateway_url: HttpUrl
    table_endpoint: HttpUrl
    table_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")
    coverage: ModelAvailabilityCoverage
    enrollments: tuple[ModelAvailabilityNodeEnrollment, ...] = Field(max_length=128)
    catalogs: tuple[ModelAvailabilityCatalogCheck, ...] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_plan(self) -> ModelAvailabilityConfirmationPlan:
        endpoints = {("managed", "managed"): str(self.gateway_url).rstrip("/")}
        for enrollment in self.enrollments:
            if enrollment.target_id != self.coverage.target_id or enrollment.api_id != self.api_id:
                raise ValueError("Confirmation enrollment does not match its target")
            for node in enrollment.nodes:
                identity = (enrollment.native_gateway_id, node.instance_id)
                if identity in endpoints:
                    raise ValueError("Confirmation repeats a node identity")
                endpoints[identity] = str(node.endpoint_url).rstrip("/")
        identities = set()
        admission_by_prefix: dict[str, tuple[str, dict[str, str]]] = {}
        for check in self.catalogs:
            if endpoints.get((check.gateway_id, check.instance_id)) != str(
                check.endpoint_url
            ).rstrip("/"):
                raise ValueError("Confirmation endpoint is not enrolled")
            identity = (check.surface_prefix, check.route)
            if identity in identities:
                raise ValueError("Confirmation repeats a catalog")
            identities.add(identity)
            admission = (check.capability_digest, check.admission_surfaces)
            if admission_by_prefix.setdefault(check.surface_prefix, admission) != admission:
                raise ValueError("Confirmation catalogs disagree about admission coverage")
        prefixes = {surface.rsplit("/", 1)[0] for surface in self.coverage.serving_surfaces}
        if prefixes != {check.surface_prefix for check in self.catalogs}:
            raise ValueError("Confirmation must cover every verified revision and instance")
        return self

    @property
    def partition_key(self) -> str:
        return f"{AVAILABILITY_CONFIRMATION_PARTITION}|{self.coverage.target_id}"

    @staticmethod
    def context_key(
        resource_id: str,
        api_id: str,
        gateway_url: str,
        table_endpoint: str,
        table_name: str,
    ) -> str:
        content = json.dumps(
            [
                resource_id.casefold(),
                api_id,
                gateway_url.rstrip("/"),
                table_endpoint.rstrip("/"),
                table_name,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(content.encode()).hexdigest()

    @property
    def row_key(self) -> str:
        return self.context_key(
            self.resource_id,
            self.api_id,
            str(self.gateway_url),
            str(self.table_endpoint),
            self.table_name,
        )

    def entity(self) -> dict[str, Any]:
        document = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if len(document) > 524288:
            raise ModelAvailabilityUnavailable("Confirmation plan exceeds its storage bound")
        parts = [document[offset : offset + 24000] for offset in range(0, len(document), 24000)]
        return {
            "PartitionKey": self.partition_key,
            "RowKey": self.row_key,
            "SchemaVersion": 1,
            "PartCount": len(parts),
            "Digest": hashlib.sha256(document.encode()).hexdigest(),
            **{f"Part{index:03d}": part for index, part in enumerate(parts)},
        }

    @classmethod
    def from_entity(cls, entity: Mapping[str, Any]) -> ModelAvailabilityConfirmationPlan:
        count = entity.get("PartCount")
        if (
            type(entity.get("SchemaVersion")) is not int
            or entity["SchemaVersion"] != 1
            or type(count) is not int
            or not 1 <= count <= 22
        ):
            raise ModelAvailabilityUnavailable("Confirmation plan schema is invalid")
        parts: list[str] = []
        for index in range(count):
            part = entity.get(f"Part{index:03d}")
            if not isinstance(part, str) or not 0 < len(part) <= 24000:
                raise ModelAvailabilityUnavailable("Confirmation plan parts are incomplete")
            parts.append(part)
        document = "".join(parts)
        if len(document) > 524288 or hashlib.sha256(document.encode()).hexdigest() != entity.get(
            "Digest"
        ):
            raise ModelAvailabilityUnavailable("Confirmation plan digest is invalid")
        plan = cls.model_validate_json(document)
        if entity.get("PartitionKey") != plan.partition_key or entity.get("RowKey") != plan.row_key:
            raise ModelAvailabilityUnavailable("Confirmation plan storage identity is invalid")
        return plan


class ModelAvailabilityTargetResult(_AvailabilityContract):
    target_id: UUID
    inventory_digest: AvailabilityDigest
    policy_digest: AvailabilityDigest | None = None
    status: Literal["pending", "verified", "failed", "unknown"] = "pending"
    observed_revision: AvailabilityRevision | None = None
    observed_enabled: StrictBool | None = None
    verified_at: AwareDatetime | None = None
    reason: str | None = Field(default=None, max_length=500)


class ModelAvailabilityOperation(_AvailabilityContract):
    id: UUID
    model_id: UUID
    idempotency_key: UUID
    expected_revision: AvailabilityRevision
    desired_revision: AvailabilityRevision
    desired_enabled: StrictBool
    created_by: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    status: AvailabilityStatus = "pending"
    targets: tuple[ModelAvailabilityTargetResult, ...] = ()
    projected_revision: AvailabilityRevision | None = None
    projected_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> ModelAvailabilityOperation:
        if self.desired_revision != self.expected_revision + 1:
            raise ValueError("Availability transitions must advance exactly one revision")
        if len({target.target_id for target in self.targets}) != len(self.targets):
            raise ValueError("Availability targets must be unique")
        if self.status == "succeeded" and (
            not self.targets
            or self.projected_revision != self.desired_revision
            or self.projected_at is None
            or self.completed_at is None
            or any(
                target.status != "verified"
                or target.policy_digest is None
                or target.observed_revision != self.desired_revision
                or target.observed_enabled != self.desired_enabled
                or target.verified_at is None
                or target.verified_at < self.created_at
                for target in self.targets
            )
        ):
            raise ValueError("Availability completion requires exact per-target evidence")
        return self


class ModelAvailabilityReadback(_AvailabilityContract):
    model_id: UUID
    desired_enabled: StrictBool
    desired_revision: AvailabilityRevision
    effective_enabled: StrictBool | None = None
    effective_revision: AvailabilityRevision | None = None
    verified_at: AwareDatetime | None = None
    status: AvailabilityStatus = "unknown"
    command_ready: StrictBool = False
    reason: str | None = None
    operation: ModelAvailabilityOperation | None = None


class ModelAvailabilityList(_AvailabilityContract):
    items: list[ModelAvailabilityReadback]


class ModelAvailabilityAccepted(_AvailabilityContract):
    operation: ModelAvailabilityOperation
    status_url: str


class ModelAvailabilityPolicyAdoption(_AvailabilityContract):
    adoption_id: UUID
    target_id: UUID
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    models: dict[str, UUID] = Field(max_length=MAX_CATALOG_MODELS)
    serving_gateways: dict[str, tuple[str, ...] | None] = Field(max_length=MAX_CATALOG_MODELS)
    original_policy_digests: dict[str, AvailabilityDigest] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_mapping(self) -> ModelAvailabilityPolicyAdoption:
        if (
            set(self.models) != set(self.serving_gateways)
            or len({alias.casefold() for alias in self.models}) != len(self.models)
            or any(not alias.strip() for alias in self.models)
        ):
            raise ValueError("Policy adoption requires an unambiguous complete model scope")
        for gateways in self.serving_gateways.values():
            if gateways is not None and (
                not gateways
                or any(not value.strip() for value in gateways)
                or len({value.casefold() for value in gateways}) != len(gateways)
            ):
                raise ValueError("Policy adoption serving scope is invalid")
        return self

    def validate_source(self, operation_id: str, source: str) -> None:
        if (
            self.original_policy_digests.get(operation_id)
            != hashlib.sha256(source.encode()).hexdigest()
        ):
            raise ModelAvailabilityConflict("Policy adoption source changed")


class ModelAvailabilityPublication(_AvailabilityContract):
    version: Literal[1] = 1
    publication_id: UUID
    target_id: UUID
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    spec_digest: AvailabilityDigest
    models: dict[str, UUID] = Field(max_length=MAX_CATALOG_MODELS)

    @staticmethod
    def digest(publication: GatewayPublication) -> str:
        document = json.dumps(
            publication.desired_spec.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(document.encode()).hexdigest()

    def validate_publication(self, publication: GatewayPublication) -> None:
        if (
            self.publication_id != publication.id
            or self.target_id != publication.gateway_profile_id
            or self.spec_digest != self.digest(publication)
            or set(self.models) != {item.id for item in publication.desired_spec.discovery_models}
            or self.revision
            != (
                publication.apim_revision
                or f"turnstile-{publication.generation}-{publication.id.hex[:8]}"
            )
        ):
            raise ModelAvailabilityConflict("Publication availability identity snapshot changed")
        for binding in publication.desired_spec.bindings:
            if (
                binding.logical_model_id is not None
                and self.models[binding.model.model_key] != binding.logical_model_id
            ):
                raise ModelAvailabilityConflict(
                    "Publication logical model identity is inconsistent"
                )

    @classmethod
    def capture(
        cls,
        publication: GatewayPublication,
        registry: Mapping[str, Sequence[dict[str, Any]]],
        api_id: str,
    ) -> ModelAvailabilityPublication:
        from .control_plane import publication_model_id

        bindings = {
            binding.model.model_key: binding for binding in publication.desired_spec.bindings
        }
        models = {}
        for item in publication.desired_spec.discovery_models:
            binding = bindings.get(item.id)
            current = [
                model
                for model in registry["models"]
                if str(model["model_key"]).casefold() == item.id.casefold()
            ]
            if len(current) > 1:
                raise ModelAvailabilityConflict("Publication model identity is ambiguous")
            if binding and binding.logical_model_id:
                model_id = binding.logical_model_id
                if current and UUID(str(current[0]["id"])) != model_id:
                    raise ModelAvailabilityConflict("Publication alias belongs to another model")
            elif current:
                model_id = UUID(str(current[0]["id"]))
            elif (
                binding is not None
                and publication.publication_kind.value == "model_add"
                and binding is publication.desired_spec.bindings[-1]
            ):
                model_id = publication_model_id(publication.id, item.id)
            else:
                raise ModelAvailabilityConflict(
                    "Publication model needs an explicit identity mapping"
                )
            models[item.id] = model_id
        snapshot = cls(
            publication_id=publication.id,
            target_id=publication.gateway_profile_id,
            api_id=api_id,
            revision=publication.apim_revision
            or f"turnstile-{publication.generation}-{publication.id.hex[:8]}",
            spec_digest=cls.digest(publication),
            models=models,
        )
        snapshot.validate_publication(publication)
        return snapshot
