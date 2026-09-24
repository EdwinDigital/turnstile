from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field, HttpUrl, model_validator

from .models import StrictModel

_GATEWAY_DEPLOYMENT_NAMESPACE = UUID("559ea680-1788-44df-95c5-05f60387e1e1")
_APIM_TARGET_ROUTE_NAMESPACE = UUID("fb2fc0a2-1d9b-409d-a52b-1f49ff530082")
_APIM_ONBOARDING_NAMESPACE = UUID("accb7b42-78c1-4c4f-89f0-54f857c8ae38")


def _require_https_gateway_endpoint(endpoint: HttpUrl) -> None:
    if (
        endpoint.scheme != "https"
        or endpoint.query is not None
        or endpoint.fragment is not None
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise ValueError("A Gateway endpoint must use HTTPS without credentials, query or fragment")


class CapabilitySupport(StrEnum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class ApimTierFamily(StrEnum):
    CLASSIC = "classic"
    V2 = "v2"


class ApimTargetOwnershipMode(StrEnum):
    TURNSTILE_MANAGED = "turnstile_managed"
    ADOPTED = "adopted"
    READ_ONLY = "read_only"


class ApimTargetDriftState(StrEnum):
    UNKNOWN = "unknown"
    IN_SYNC = "in_sync"
    DRIFTED = "drifted"


class ApimTargetConnectionState(StrEnum):
    UNKNOWN = "unknown"
    CONNECTED = "connected"
    UNAVAILABLE = "unavailable"


class GatewayDeploymentKind(StrEnum):
    MANAGED = "managed"
    SELF_HOSTED = "self_hosted"


class GatewayHostingMode(StrEnum):
    AZURE_MANAGED = "azure_managed"
    AKS = "aks"
    KUBERNETES = "kubernetes"
    ARC = "arc"
    CONTAINER_APPS = "container_apps"
    DOCKER = "docker"
    OTHER = "other"


class GatewayLifecycleOwnership(StrEnum):
    CUSTOMER = "customer"
    TURNSTILE_IAC = "turnstile_iac"
    EXTERNAL = "external"


class GatewayReleaseGate(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class GatewayDeclaredState(StrEnum):
    UNKNOWN = "unknown"
    PRESENT = "present"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"


class GatewayReachabilityState(StrEnum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class GatewayObservationState(StrEnum):
    UNKNOWN = "unknown"
    OBSERVED = "observed"
    STALE = "stale"


class GatewayObservationSource(StrEnum):
    APIM_CONTEXT = "apim_context"
    CLUSTER_TELEMETRY = "cluster_telemetry"
    LOCAL_LOG = "local_log"
    PROBE = "probe"


class GatewayObservationConfidence(StrEnum):
    DECLARED = "declared"
    REACHABLE = "reachable"
    OBSERVED = "observed"


class GatewayDeploymentScopeMode(StrEnum):
    ALL = "all"
    SELECTED = "selected"


def gateway_deployment_id_for(apim_target_id: UUID, native_gateway_id: str) -> UUID:
    return uuid5(_GATEWAY_DEPLOYMENT_NAMESPACE, f"{apim_target_id}:{native_gateway_id.casefold()}")


def apim_target_route_binding_id_for(logical_model_id: UUID, apim_target_id: UUID) -> UUID:
    return uuid5(_APIM_TARGET_ROUTE_NAMESPACE, f"{logical_model_id}:{apim_target_id}")


class TargetRolloutKind(StrEnum):
    MODEL_ADD = "model_add"
    MODEL_REMOVE = "model_remove"
    CREDENTIAL_ROTATION = "credential_rotation"
    ROUTE_RECONCILE = "route_reconcile"


class TargetRolloutChildStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class TargetRolloutStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class TargetRolloutChild(StrictModel):
    apim_target_id: UUID
    publication_id: UUID
    required: bool = True
    status: TargetRolloutChildStatus


def derive_target_rollout_status(children: tuple[TargetRolloutChild, ...]) -> TargetRolloutStatus:
    if not children:
        raise ValueError("A multi-target rollout requires at least one child publication")
    statuses = {child.status for child in children}
    if statuses == {TargetRolloutChildStatus.QUEUED}:
        return TargetRolloutStatus.QUEUED
    if TargetRolloutChildStatus.ROLLING_BACK in statuses:
        return TargetRolloutStatus.ROLLING_BACK
    if statuses & {TargetRolloutChildStatus.QUEUED, TargetRolloutChildStatus.IN_PROGRESS}:
        return TargetRolloutStatus.IN_PROGRESS
    if statuses == {TargetRolloutChildStatus.SUCCEEDED}:
        return TargetRolloutStatus.SUCCEEDED
    if statuses == {TargetRolloutChildStatus.ROLLED_BACK}:
        return TargetRolloutStatus.ROLLED_BACK
    if TargetRolloutChildStatus.SUCCEEDED in statuses:
        return TargetRolloutStatus.PARTIAL
    return TargetRolloutStatus.FAILED


class MultiTargetRollout(StrictModel):
    schema_version: Literal[1] = 1
    id: UUID
    kind: TargetRolloutKind
    logical_model_id: UUID | None = None
    status: TargetRolloutStatus
    children: tuple[TargetRolloutChild, ...] = Field(min_length=1)
    created_by: str = Field(min_length=1, max_length=320)
    created_at: datetime

    @model_validator(mode="after")
    def validate_children_and_status(self) -> MultiTargetRollout:
        targets = [child.apim_target_id for child in self.children]
        if len(targets) != len(set(targets)):
            raise ValueError("A multi-target rollout cannot repeat an APIM target")
        expected = derive_target_rollout_status(self.children)
        if self.status is not expected:
            raise ValueError(f"Multi-target rollout status must be derived as {expected.value}")
        if self.kind in {TargetRolloutKind.MODEL_ADD, TargetRolloutKind.MODEL_REMOVE} \
                and self.logical_model_id is None:
            raise ValueError("A model rollout requires a logical model identity")
        return self


class MultiTargetRolloutList(StrictModel):
    items: list[MultiTargetRollout]


class ApimTargetCapabilitySnapshot(StrictModel):
    schema_version: Literal[1] = 1
    self_hosted_gateway: CapabilitySupport = CapabilitySupport.UNKNOWN
    backend_pools: CapabilitySupport = CapabilitySupport.UNKNOWN
    managed_identity_policy: CapabilitySupport = CapabilitySupport.UNKNOWN
    event_hub_logger: CapabilitySupport = CapabilitySupport.UNKNOWN
    table_ledger: CapabilitySupport = CapabilitySupport.UNKNOWN
    responses: CapabilitySupport = CapabilitySupport.UNKNOWN
    anthropic_messages: CapabilitySupport = CapabilitySupport.UNKNOWN
    response_observer: CapabilitySupport = CapabilitySupport.UNKNOWN
    source_api_version: str | None = Field(default=None, min_length=1, max_length=64)
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def require_complete_evidence_identity(self) -> ApimTargetCapabilitySnapshot:
        if (self.source_api_version is None) != (self.observed_at is None):
            raise ValueError(
                "Capability API version and observation time must be recorded together"
            )
        return self


class ApimTarget(StrictModel):
    schema_version: Literal[1] = 1
    gateway_profile_id: UUID
    arm_resource_id: str = Field(min_length=1, max_length=2048)
    tenant_id: UUID
    subscription_id: UUID
    resource_group: str = Field(min_length=1, max_length=90)
    service_name: str = Field(min_length=1, max_length=50)
    api_id: str = Field(min_length=1, max_length=256)
    api_path: str = Field(min_length=1, max_length=400)
    service_tier: str = Field(min_length=1, max_length=64)
    tier_family: ApimTierFamily
    service_location: str = Field(min_length=1, max_length=128)
    principal_id: UUID | None = None
    management_auth_profile_ref: str | None = Field(default=None, min_length=1, max_length=255)
    probe_subscription_id: str | None = Field(default=None, min_length=1, max_length=255)
    probe_secret_ref: HttpUrl | None = None
    ownership_mode: ApimTargetOwnershipMode
    capabilities: ApimTargetCapabilitySnapshot = Field(default_factory=ApimTargetCapabilitySnapshot)
    last_inventory_sync_at: datetime | None = None
    last_validated_at: datetime | None = None
    drift_state: ApimTargetDriftState = ApimTargetDriftState.UNKNOWN

    @model_validator(mode="after")
    def validate_target_identity(self) -> ApimTarget:
        expected = (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.service_name}"
        )
        if self.arm_resource_id.rstrip("/").casefold() != expected.casefold():
            raise ValueError("APIM ARM resource ID does not match the target identity")
        if self.api_path.startswith("/") or self.api_path.endswith("/"):
            raise ValueError("APIM API path must not start or end with a slash")
        if (
            self.tier_family is ApimTierFamily.V2
            and self.capabilities.self_hosted_gateway is CapabilitySupport.SUPPORTED
        ):
            raise ValueError("APIM v2 tiers cannot advertise self-hosted Gateway support")
        return self


class SelfHostedGatewayWrite(StrictModel):
    native_gateway_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    endpoint_url: HttpUrl
    location: str | None = Field(default=None, min_length=1, max_length=128)
    hosting_mode: GatewayHostingMode = GatewayHostingMode.OTHER
    release_gate: GatewayReleaseGate = GatewayReleaseGate.REQUIRED

    @model_validator(mode="after")
    def require_customer_hosting(self) -> SelfHostedGatewayWrite:
        _require_https_gateway_endpoint(self.endpoint_url)
        if self.native_gateway_id.casefold() == "managed":
            raise ValueError("A Self-hosted Gateway cannot use native ID 'managed'")
        if self.hosting_mode is GatewayHostingMode.AZURE_MANAGED:
            raise ValueError("A Self-hosted Gateway cannot use Azure-managed hosting")
        return self


class SelfHostedGatewayNameWrite(StrictModel):
    display_name: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def require_display_name(self) -> SelfHostedGatewayNameWrite:
        self.display_name = self.display_name.strip()
        if not self.display_name:
            raise ValueError("The Gateway display name cannot be blank")
        return self


class SelfHostedGatewayAssociationChange(SelfHostedGatewayWrite):
    apim_target_id: UUID


class GatewayDeployment(StrictModel):
    schema_version: Literal[1] = 1
    id: UUID
    apim_target_id: UUID
    native_gateway_id: str = Field(min_length=1, max_length=80)
    kind: GatewayDeploymentKind
    display_name: str = Field(min_length=1, max_length=120)
    location: str | None = Field(default=None, min_length=1, max_length=128)
    endpoint_url: HttpUrl | None = None
    hosting_mode: GatewayHostingMode
    lifecycle_ownership: GatewayLifecycleOwnership
    release_gate: GatewayReleaseGate = GatewayReleaseGate.REQUIRED
    declared_state: GatewayDeclaredState = GatewayDeclaredState.UNKNOWN
    reachability_state: GatewayReachabilityState = GatewayReachabilityState.UNKNOWN
    observation_state: GatewayObservationState = GatewayObservationState.UNKNOWN
    runtime_version_support: CapabilitySupport = CapabilitySupport.UNKNOWN
    rate_limit_synchronization: CapabilitySupport = CapabilitySupport.UNKNOWN
    last_arm_sync_at: datetime | None = None
    last_probe_at: datetime | None = None
    last_observed_at: datetime | None = None
    retired_at: datetime | None = None

    @model_validator(mode="after")
    def validate_deployment_identity_and_evidence(self) -> GatewayDeployment:
        if self.endpoint_url is not None:
            _require_https_gateway_endpoint(self.endpoint_url)
        managed = self.kind is GatewayDeploymentKind.MANAGED
        if managed != (self.native_gateway_id.casefold() == "managed"):
            raise ValueError("Gateway kind conflicts with its native identity")
        if managed != (self.hosting_mode is GatewayHostingMode.AZURE_MANAGED):
            raise ValueError("Gateway kind conflicts with its hosting mode")
        if (
            self.declared_state is not GatewayDeclaredState.UNKNOWN
            and self.last_arm_sync_at is None
        ):
            raise ValueError("A known declared state requires an ARM inventory timestamp")
        if self.reachability_state is not GatewayReachabilityState.UNKNOWN and (
            self.endpoint_url is None or self.last_probe_at is None
        ):
            raise ValueError("A known reachability state requires an endpoint and probe timestamp")
        if (
            self.observation_state is not GatewayObservationState.UNKNOWN
            and self.last_observed_at is None
        ):
            raise ValueError("A known observation state requires an observation timestamp")
        if (
            self.hosting_mode is GatewayHostingMode.CONTAINER_APPS
            and self.rate_limit_synchronization is CapabilitySupport.SUPPORTED
        ):
            raise ValueError("Azure Container Apps cannot advertise UDP rate-limit synchronization")
        return self


class GatewayInstanceObservation(StrictModel):
    schema_version: Literal[1] = 1
    deployment_id: UUID
    native_instance_id: str = Field(min_length=1, max_length=255)
    runtime_version: str | None = Field(default=None, min_length=1, max_length=64)
    processor_count: int | None = Field(default=None, ge=1)
    first_seen_at: datetime
    last_seen_at: datetime
    last_successful_request_at: datetime | None = None
    source: GatewayObservationSource
    confidence: GatewayObservationConfidence

    @model_validator(mode="after")
    def validate_observation_window(self) -> GatewayInstanceObservation:
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("Gateway instance last-seen time cannot precede first-seen time")
        if (
            self.last_successful_request_at is not None
            and self.last_successful_request_at > self.last_seen_at
        ):
            raise ValueError("Gateway instance success time cannot follow last-seen time")
        return self


class GatewayDeploymentScope(StrictModel):
    mode: GatewayDeploymentScopeMode = GatewayDeploymentScopeMode.ALL
    deployment_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> GatewayDeploymentScope:
        if len(self.deployment_ids) != len(set(self.deployment_ids)):
            raise ValueError("A Gateway deployment scope cannot repeat a deployment")
        if self.mode is GatewayDeploymentScopeMode.ALL and self.deployment_ids:
            raise ValueError("An all-deployments scope cannot list selected deployments")
        if self.mode is GatewayDeploymentScopeMode.SELECTED and not self.deployment_ids:
            raise ValueError("A selected-deployments scope requires at least one deployment")
        return self


class GatewayServingDeployment(StrictModel):
    deployment_id: UUID
    native_gateway_id: str = Field(min_length=1, max_length=80)
    kind: GatewayDeploymentKind
    endpoint_url: HttpUrl
    release_gate: GatewayReleaseGate

    @model_validator(mode="after")
    def validate_native_identity(self) -> GatewayServingDeployment:
        if (self.native_gateway_id.casefold() == "managed") != (
            self.kind is GatewayDeploymentKind.MANAGED
        ):
            raise ValueError("Gateway serving deployment kind conflicts with native ID")
        _require_https_gateway_endpoint(self.endpoint_url)
        return self


class GatewayEndpointProbeResult(StrictModel):
    deployment_id: UUID
    native_gateway_id: str = Field(min_length=1, max_length=80)
    required: bool
    status: Literal["succeeded", "optional_pending"]
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=1000)


class ApimOnboardingRequest(StrictModel):
    tenant_id: UUID
    resource_id: str = Field(min_length=1, max_length=1024)
    name: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_resource_identity(self) -> ApimOnboardingRequest:
        parts = self.resource_id.strip().rstrip("/").split("/")
        if (
            len(parts) != 9 or parts[0] != ""
            or parts[6].casefold() != "microsoft.apimanagement"
            or [parts[index].casefold() for index in (1, 3, 5, 7)]
            != ["subscriptions", "resourcegroups", "providers", "service"]
            or not re.fullmatch(r"[A-Za-z0-9_().-]{1,90}", parts[4])
            or not re.fullmatch(r"[A-Za-z](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?", parts[8])
        ):
            raise ValueError("Provide the ARM resource ID of an existing API Management service")
        subscription_id = UUID(parts[2])
        self.resource_id = (
            f"/subscriptions/{subscription_id}/resourceGroups/{parts[4]}/"
            f"providers/Microsoft.ApiManagement/service/{parts[8]}"
        )
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("The APIM display name cannot be blank")
        return self

    @property
    def subscription_id(self) -> UUID:
        return UUID(self.resource_id.split("/")[2])

    @property
    def resource_group(self) -> str:
        return self.resource_id.split("/")[4]

    @property
    def service_name(self) -> str:
        return self.resource_id.split("/")[8]

    @property
    def gateway_profile_id(self) -> UUID:
        return uuid5(_APIM_ONBOARDING_NAMESPACE, f"{self.tenant_id}:{self.resource_id.casefold()}")

    @property
    def api_id(self) -> str:
        return f"turnstile-{self.gateway_profile_id.hex[:12]}"

    @property
    def api_path(self) -> str:
        return f"turnstile/{self.gateway_profile_id.hex[:12]}"

    @property
    def dashboard_subscription_id(self) -> str:
        return f"{self.api_id}-dashboard"

    @property
    def probe_subscription_id(self) -> str:
        return f"{self.api_id}-publisher-probe"


class EmployeeClientSurface(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    tokens: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def require_nonempty_tokens(self) -> EmployeeClientSurface:
        if any(not token.strip() for token in self.tokens):
            raise ValueError("Employee client surface tokens must not be empty")
        return self


class ApimOnboardingConfiguration(StrictModel):
    management_tenant_id: UUID
    event_hub_resource_id: str = Field(pattern=(
        r"(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/"
        r"Microsoft\.EventHub/namespaces/[A-Za-z0-9-]+/eventhubs/[A-Za-z0-9._-]+$"
    ))
    ledger_table_resource_id: str = Field(pattern=(
        r"(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/"
        r"Microsoft\.Storage/storageAccounts/[a-z0-9]+/tableServices/default/tables/[A-Za-z0-9]+$"
    ))
    employee_tenant_id: UUID
    employee_client_id: UUID
    employee_audience: str = Field(min_length=1, max_length=256)
    organization_id: str = Field(min_length=1, max_length=120)
    organization_name: str = Field(min_length=1, max_length=120)
    employee_tokens_per_minute: int = Field(default=100_000, ge=1, le=2_147_483_647)
    default_max_output_tokens: int = Field(default=1024, ge=1, le=2_147_483_647)
    department_map: dict[str, str] = Field(default_factory=dict)
    client_map: dict[str, str] = Field(default_factory=dict)
    surface_map: list[EmployeeClientSurface] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_configuration(self) -> ApimOnboardingConfiguration:
        for resource_id in (self.event_hub_resource_id, self.ledger_table_resource_id):
            UUID(resource_id.split("/")[2])
            if any(character in resource_id for character in ("?", "#", "%", "\\")):
                raise ValueError("Onboarding dependencies require plain ARM resource IDs")
        return self


class ApimOnboardingPlan(StrictModel):
    request: ApimOnboardingRequest
    gateway_profile_id: UUID
    service_name: str
    service_tier: str
    location: str
    managed_gateway_origin: HttpUrl
    api_id: str
    api_path: str
    dashboard_subscription_id: str
    probe_subscription_id: str
    initial_monthly_token_limit: int = Field(default=100_000, ge=1)
    initial_tokens_per_minute: int = Field(default=100_000, ge=1, le=2_147_483_647)
    ready: bool
    blockers: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=lambda: [
        "validate_target", "create_api", "configure_governance", "create_subscriptions",
        "project_access", "verify_managed_gateway", "activate_target",
    ])
    confirmation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_derived_resources(self) -> ApimOnboardingPlan:
        fields = ("gateway_profile_id", "service_name", "api_id", "api_path",
                  "dashboard_subscription_id", "probe_subscription_id")
        if any(getattr(self, name) != getattr(self.request, name) for name in fields):
            raise ValueError("APIM initialization resources must match the requested service")
        origin = self.managed_gateway_origin
        _require_https_gateway_endpoint(origin)
        if (
            origin.host != f"{self.request.service_name.lower()}.azure-api.net"
            or origin.path not in {None, "", "/"} or origin.port not in {None, 443}
        ):
            raise ValueError("APIM initialization requires its own Managed Gateway origin")
        if self.ready and self.blockers:
            raise ValueError("Blocked initialization cannot be ready")
        return self


class ApimOnboardingStart(StrictModel):
    request: ApimOnboardingRequest
    confirmation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApimTargetWrite(StrictModel):
    gateway_profile_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=120)
    management_endpoint: HttpUrl
    tenant_id: UUID
    subscription_id: UUID
    resource_group: str = Field(min_length=1, max_length=90)
    service_name: str = Field(min_length=1, max_length=50)
    api_id: str = Field(min_length=1, max_length=256)
    gateway_endpoint: HttpUrl
    management_auth_profile_ref: Literal["system_managed_identity"] = "system_managed_identity"
    dashboard_subscription_id: str = Field(
        default="turnstile-dashboard", min_length=1, max_length=255
    )
    probe_subscription_id: str = Field(
        default="turnstile-publisher-probe", min_length=1, max_length=255
    )

    @model_validator(mode="after")
    def validate_existing_apim_endpoints(self) -> ApimTargetWrite:
        expected = (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.service_name}"
        )
        management = self.management_endpoint
        _require_https_gateway_endpoint(management)
        if (management.host or "").casefold() != "management.azure.com" or (
            management.path or ""
        ).rstrip("/").casefold() != expected.casefold():
            raise ValueError(
                "APIM Management endpoint must match the supplied Azure resource identity"
            )
        _require_https_gateway_endpoint(self.gateway_endpoint)
        return self


class ApimTargetRouteBinding(StrictModel):
    schema_version: Literal[1] = 1
    id: UUID
    logical_model_id: UUID
    apim_target_id: UUID
    runtime_id: UUID
    enabled: bool = True
    is_default_serving: bool = False
    deployment_scope: GatewayDeploymentScope = Field(default_factory=GatewayDeploymentScope)
    current_publication_id: UUID | None = None
    capability_state: CapabilitySupport = CapabilitySupport.UNKNOWN

    @model_validator(mode="after")
    def require_enabled_default(self) -> ApimTargetRouteBinding:
        if self.is_default_serving and not self.enabled:
            raise ValueError("A default APIM target route binding must be enabled")
        return self


class GatewayHostnameConfigurationInventory(StrictModel):
    configuration_id: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=255)
    hostname: str = Field(min_length=1, max_length=255)
    certificate_id: str | None = Field(default=None, min_length=1, max_length=2048)
    negotiate_client_certificate: bool = False


class GatewayCertificateAuthorityInventory(StrictModel):
    authority_id: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=255)
    is_trusted: bool


class GatewayArmInventory(StrictModel):
    native_gateway_id: str = Field(min_length=1, max_length=80)
    arm_resource_id: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=1000)
    location_name: str | None = Field(default=None, max_length=256)
    city: str | None = Field(default=None, max_length=256)
    district: str | None = Field(default=None, max_length=256)
    country_or_region: str | None = Field(default=None, max_length=256)
    associated_api_ids: tuple[str, ...] = ()
    hostname_configurations: tuple[GatewayHostnameConfigurationInventory, ...] = ()
    certificate_authorities: tuple[GatewayCertificateAuthorityInventory, ...] = ()

    @model_validator(mode="after")
    def require_unique_inventory_identity(self) -> GatewayArmInventory:
        values = (
            self.associated_api_ids,
            tuple(item.configuration_id for item in self.hostname_configurations),
            tuple(item.authority_id for item in self.certificate_authorities),
        )
        if any(len(items) != len({item.casefold() for item in items}) for items in values):
            raise ValueError("Gateway ARM inventory identities must be unique")
        return self


class ApimTargetArmInventory(StrictModel):
    gateway_profile_id: UUID
    arm_resource_id: str = Field(min_length=1, max_length=2048)
    subscription_id: UUID
    resource_group: str = Field(min_length=1, max_length=90)
    service_name: str = Field(min_length=1, max_length=50)
    api_id: str = Field(min_length=1, max_length=256)
    api_path: str = Field(min_length=1, max_length=400)
    api_revision: str = Field(min_length=1, max_length=256)
    service_tier: str = Field(min_length=1, max_length=64)
    tier_family: ApimTierFamily
    service_location: str = Field(min_length=1, max_length=128)
    principal_id: UUID | None = None
    managed_gateway_url: HttpUrl
    capabilities: ApimTargetCapabilitySnapshot
    gateways: tuple[GatewayArmInventory, ...] = ()
    source_api_version: str = Field(min_length=1, max_length=64)
    discovered_at: datetime


class GatewayDeploymentInventory(StrictModel):
    deployment_id: UUID
    associated_api_ids: tuple[str, ...] = ()
    hostname_configurations: tuple[GatewayHostnameConfigurationInventory, ...] = ()
    certificate_authorities: tuple[GatewayCertificateAuthorityInventory, ...] = ()
    source_api_version: str = Field(min_length=1, max_length=64)
    synced_at: datetime

    @model_validator(mode="after")
    def require_unique_inventory_identity(self) -> GatewayDeploymentInventory:
        values = (
            self.associated_api_ids,
            tuple(item.configuration_id for item in self.hostname_configurations),
            tuple(item.authority_id for item in self.certificate_authorities),
        )
        if any(len(items) != len({item.casefold() for item in items}) for items in values):
            raise ValueError("Gateway deployment inventory identities must be unique")
        return self


class GatewayPublishedModelSummary(StrictModel):
    id: UUID
    model_key: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)


class GatewayDeploymentTopology(StrictModel):
    deployment: GatewayDeployment
    inventory: GatewayDeploymentInventory | None = None
    configuration_endpoint: HttpUrl
    published_models: tuple[GatewayPublishedModelSummary, ...] = ()


class ApimTargetTopology(StrictModel):
    target: ApimTarget
    deployments: list[GatewayDeploymentTopology]
    management_endpoint: HttpUrl
    gateway_endpoint: HttpUrl | None = None
    configuration_endpoint: HttpUrl
    connection_state: ApimTargetConnectionState
    self_hosted_gateway_count: int = Field(ge=0)
    published_model_count: int = Field(ge=0)
    published_models: tuple[GatewayPublishedModelSummary, ...] = ()


class SelfHostedGatewaySummary(StrictModel):
    id: UUID
    native_gateway_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)


class ApimTargetRemovalPreview(StrictModel):
    gateway_profile_id: UUID
    service_name: str
    self_hosted_gateway_count: int = Field(ge=0)
    runtime_count: int = Field(ge=0)
    model_count: int = Field(ge=0)
    self_hosted_gateways: tuple[SelfHostedGatewaySummary, ...] = ()
    models: tuple[GatewayPublishedModelSummary, ...] = ()
    removal_allowed: bool
    blockers: tuple[str, ...] = ()


class SelfHostedGatewayRemovalPreview(StrictModel):
    deployment_id: UUID
    display_name: str
    model_count: int = Field(ge=0)
    models: tuple[GatewayPublishedModelSummary, ...] = ()
    removal_allowed: bool
    blockers: tuple[str, ...] = ()


class ApimTargetTopologyList(StrictModel):
    items: list[ApimTargetTopology]
    total: int = Field(ge=0)
    inventory_sync_available: bool
    inventory_sync_unavailable_reason: str | None = None
    resource_lifecycle_available: bool
    deployment_scoping_available: bool = False
    onboarding_available: bool = False
    self_hosted_gateway_name_edit_supported: bool = False
