from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from ..domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationSubscriptionProvisionSpec,
)
from ..domain.billable_requests import BillableRequestOutcome
from ..domain.control_plane import (
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
)
from ..domain.gateway_targets import GatewayEndpointProbeResult
from ..domain.runtime_models import OAuthClientCredentialsConfig


class PolicyCompilationError(ValueError):
    def __init__(self, message: str, *, billable_outcome: BillableRequestOutcome | None = None):
        super().__init__(message)
        self.billable_outcome = billable_outcome


class InfrastructureUpgradeRequiredError(PolicyCompilationError):
    pass


class CredentialSourceMissingError(PolicyCompilationError):
    def __init__(self, named_value_id: str) -> None:
        self.named_value_id = named_value_id
        super().__init__(f"Named value {named_value_id} has no credential source")


class PublisherPermissionError(PolicyCompilationError):
    def __init__(
        self, *, missing_actions: Sequence[str] = (), request_id: str | None = None,
    ) -> None:
        self.missing_actions = tuple(sorted(set(missing_actions)))
        self.request_id = request_id
        super().__init__("Publisher permission preflight failed: " + json.dumps({
            "code": "publisher_permissions_missing"
            if self.missing_actions else "publisher_permissions_unverified",
            "missing_actions": list(self.missing_actions), "request_id": request_id,
        }, separators=(",", ":")))


def missing_management_actions(payload: object, required: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(payload, dict) or payload.get("nextLink"):
        raise ValueError("Permission inventory is incomplete")
    permissions = payload.get("value")
    if not isinstance(permissions, list):
        raise ValueError("Permission inventory is invalid")
    granted: set[str] = set()
    for permission in permissions:
        if not isinstance(permission, dict) or permission.get("condition"):
            raise ValueError("Permission conditions cannot be verified")
        actions = permission.get("actions")
        excluded = permission.get("notActions", [])
        if (not isinstance(actions, list) or not isinstance(excluded, list)
                or any(not isinstance(action, str) for action in [*actions, *excluded])):
            raise ValueError("Permission inventory has invalid actions")
        granted.update(action for action in required if any(
            fnmatchcase(action.casefold(), allowed.casefold()) for allowed in actions
        ) and not any(
            fnmatchcase(action.casefold(), denied.casefold()) for denied in excluded
        ))
    return tuple(sorted(set(required) - granted))


class RetryablePublicationError(RuntimeError):
    pass


class AuthorizationRequiredError(RuntimeError):
    def __init__(self, message: str, *, billable_outcome: BillableRequestOutcome | None = None):
        super().__init__(message)
        self.billable_outcome = billable_outcome


@dataclass(frozen=True)
class ResolvedApimPublisherClient:
    gateway_profile_id: UUID
    target_identity_sha256: str
    enforce_identity_snapshot: bool
    client: ApimPublisherClient
    usage_observer: tuple[str | None, str | None] | None = None


class ApimPublisherClientResolver(Protocol):
    def resolve(
        self, gateway_profile_id: UUID, *, include_probe_credentials: bool = True,
    ) -> ResolvedApimPublisherClient: ...


class ApimSubscriptionKeyClientResolver(Protocol):
    def resolve(self, gateway_profile_id: UUID) -> ApimSubscriptionKeyClient: ...


class FixedApimPublisherClientResolver:
    def __init__(self, client: ApimPublisherClient) -> None:
        self._client = client

    def resolve(
        self, gateway_profile_id: UUID, *, include_probe_credentials: bool = True,
    ) -> ResolvedApimPublisherClient:
        del include_probe_credentials
        return ResolvedApimPublisherClient(
            gateway_profile_id=gateway_profile_id,
            target_identity_sha256=hashlib.sha256(
                f"fixed-client:{gateway_profile_id}".encode()
            ).hexdigest(),
            enforce_identity_snapshot=False, client=self._client,
        )


def coerce_apim_publisher_client_resolver(
    value: ApimPublisherClient | ApimPublisherClientResolver,
) -> ApimPublisherClientResolver:
    if callable(getattr(value, "resolve", None)):
        return cast(ApimPublisherClientResolver, value)
    return FixedApimPublisherClientResolver(cast(ApimPublisherClient, value))


class ImageProbeJournal(Protocol):
    def heartbeat(self) -> None: ...

    def run(
        self,
        binding: GatewayModelBinding,
        revision: str,
        body: Mapping[str, Any],
        send: Callable[[str, str], dict[str, Any]],
        *,
        gateway_endpoint: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class BackendResource:
    id: str
    title: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    circuit_breaker: BackendCircuitBreakerResource | None = None


@dataclass(frozen=True)
class BackendCircuitBreakerResource:
    failure_count: int
    interval_seconds: int
    trip_duration_seconds: int
    accept_retry_after: bool
    status_code_ranges: tuple[tuple[int, int], ...] = (
        (429, 429),
        (408, 408),
        (500, 599),
    )
    error_reasons: tuple[str, ...] = ("BackendConnectionFailure", "Timeout")


@dataclass(frozen=True)
class BackendPoolMemberResource:
    backend_id: str
    priority: int
    weight: int


@dataclass(frozen=True)
class BackendPoolResource(BackendResource):
    members: tuple[BackendPoolMemberResource, ...] = ()
    session_cookie_name: str | None = None


@dataclass(frozen=True)
class NamedValueResource:
    id: str
    key_vault_secret_id: str | None
    owner_publication_id: str | None = None
    value: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OAuthCredentialResource:
    config: OAuthClientCredentialsConfig
    client_secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OperationResource:
    id: str
    display_name: str
    method: Literal["GET", "POST"]
    path: str


@dataclass(frozen=True)
class CompiledGatewayRelease:
    chat_completions_policy: str
    responses_policy: str
    responses_compact_policy: str
    messages_policy: str
    count_tokens_policy: str
    models_policy: str
    policy_sha256: str
    backends: tuple[BackendResource, ...]
    named_values: tuple[NamedValueResource, ...]
    images_generations_policy: str | None = None
    operations: tuple[OperationResource, ...] = ()
    oauth_credentials: tuple[OAuthCredentialResource, ...] = ()


@dataclass(frozen=True)
class ReleaseGcPlanEvidence:
    current_non_release_references: dict[str, object]
    candidates: tuple[dict[str, object], ...]
    reference_graph_sha256: str


class ApimSubscriptionKeyClient(Protocol):
    def application_subscription_keys(
        self, apim_subscription_id: str
    ) -> tuple[str, str]: ...

    def regenerate_application_subscription_key(
        self,
        apim_subscription_id: str,
        key_kind: Literal["primary", "secondary"],
    ) -> None: ...


class ApimPublisherClient(Protocol):
    def verify_publication_permissions(
        self, compiled: CompiledGatewayRelease, *, recovery_only: bool = False,
    ) -> None: ...

    def ensure_backend(self, backend: BackendResource) -> None: ...

    def ensure_named_value(self, named_value: NamedValueResource) -> None: ...

    def ensure_oauth_credential(self, credential: OAuthCredentialResource) -> None: ...

    def ensure_revision(self, revision: str, description: str) -> None: ...

    def put_api_policy(self, revision: str, policy: str) -> None: ...

    def ensure_operation(self, revision: str, operation: OperationResource) -> None: ...

    def put_operation_policy(self, revision: str, operation: str, policy: str) -> None: ...

    def probe_revision(
        self,
        revision: str,
        publication: GatewayPublication,
        *,
        journal: ImageProbeJournal | None = None,
    ) -> tuple[GatewayEndpointProbeResult, ...] | None: ...

    def promote_revision(self, revision: str, release_name: str) -> None: ...

    def current_revision(self) -> str | None: ...

    def current_api_policy(self) -> tuple[str, str]: ...

    def revision_api_policy(self, revision: str) -> str: ...

    def inspect_revision_dependencies(
        self, publication: GatewayPublication
    ) -> GatewayReleaseDependencies: ...

    def plan_release_garbage_collection(
        self,
        releases: Sequence[GatewayPublication],
        retained_release_ids: set[str],
    ) -> ReleaseGcPlanEvidence: ...

    def discover_gateway_applications(
        self, gateway_profile_id: UUID
    ) -> GatewayApplicationDiscovery: ...

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None: ...

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None: ...