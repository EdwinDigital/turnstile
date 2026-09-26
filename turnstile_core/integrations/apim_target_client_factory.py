from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from ..config import Settings
from ..domain.gateway_targets import (
    ApimTarget,
    ApimTargetDriftState,
    GatewayDeclaredState,
    GatewayDeployment,
    GatewayDeploymentKind,
    GatewayReachabilityState,
)
from ..persistence.repository_contract import QueryRepository
from .apim_control_plane_contract import (
    ApimPublisherClient,
    ApimSubscriptionKeyClient,
    PolicyCompilationError,
    ResolvedApimPublisherClient,
)
from .apim_publisher_client import AzureApimPublisherClient
from .apim_subscription_key_client import AzureApimSubscriptionKeyClient


@dataclass(frozen=True)
class ApimTargetClientConfig:
    gateway_profile_id: UUID
    target_identity_sha256: str
    enforce_identity_snapshot: bool
    settings: Settings = field(repr=False)


class ApimTargetClientConfigResolver(Protocol):
    def resolve(self, gateway_profile_id: UUID) -> ApimTargetClientConfig: ...


class TargetProbeCredentialResolver(Protocol):
    def resolve(self, target: ApimTarget) -> SecretStr | None: ...


class AzureApimTargetProbeCredentialResolver:
    def resolve(self, target: ApimTarget) -> SecretStr:
        if not target.probe_subscription_id:
            raise PolicyCompilationError("The APIM target has no probe subscription identity")
        settings = Settings.model_construct(
            azure_subscription_id=str(target.subscription_id),
            apim_resource_group=target.resource_group,
            apim_service_name=target.service_name,
            apim_api_id=target.api_id,
        )
        primary_key, _ = AzureApimSubscriptionKeyClient(settings).application_subscription_keys(
            target.probe_subscription_id
        )
        return SecretStr(primary_key)


class CompatibilityTargetProbeCredentialResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, target: ApimTarget) -> SecretStr | None:
        if target.gateway_profile_id != self._settings.apim_gateway_profile_id:
            raise PolicyCompilationError(
                "Target-specific probe credential resolution is not configured"
            )
        return self._settings.apim_probe_subscription_key


def _identity_sha256(values: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resolve_apim_target_settings(repository: QueryRepository, settings: Settings) -> Settings:
    if (
        settings.apim_gateway_profile_id is not None
        or not settings.apim_target_registry_enabled
        or not (
            settings.apim_usage_observer_url or settings.apim_legacy_target_compatibility_enabled
        )
    ):
        return settings
    identity = settings.legacy_apim_identity
    if identity is None:
        raise PolicyCompilationError("Legacy APIM resource identity is incomplete")
    matches = []
    for row in repository.list_apim_targets():
        target = ApimTarget.model_validate({name: row[name] for name in ApimTarget.model_fields})
        if (
            str(target.subscription_id),
            target.resource_group.casefold(),
            target.service_name.casefold(),
            target.api_id.casefold(),
        ) == identity:
            matches.append(target)
    if len(matches) != 1:
        raise PolicyCompilationError(
            "Legacy APIM identity must match exactly one registered target"
        )
    if matches[0].drift_state is ApimTargetDriftState.DRIFTED:
        raise PolicyCompilationError("Legacy APIM target identity is drifted")
    return settings.model_copy(update={"apim_gateway_profile_id": matches[0].gateway_profile_id})


class LegacySettingsApimTargetClientConfigResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, gateway_profile_id: UUID) -> ApimTargetClientConfig:
        if gateway_profile_id != self._settings.apim_gateway_profile_id:
            raise PolicyCompilationError(
                "Publication target does not match the configured APIM target"
            )
        observer_url, observer_key = self._settings.usage_observer_for(gateway_profile_id)
        identity = {
            name: getattr(self._settings, name)
            for name in (
                "azure_subscription_id",
                "apim_resource_group",
                "apim_service_name",
                "apim_api_id",
                "apim_gateway_url",
                "apim_principal_id",
                "apim_probe_subscription_id",
            )
        }
        identity.update(
            gateway_profile_id=str(gateway_profile_id),
            usage_observer_url=observer_url,
            usage_observer_key_named_value=observer_key,
        )
        return ApimTargetClientConfig(
            gateway_profile_id,
            _identity_sha256(identity),
            False,
            self._settings.model_copy(
                update={
                    "apim_usage_observer_url": observer_url,
                    "apim_usage_observer_key_named_value": observer_key,
                }
            ),
        )


class RepositoryApimTargetClientConfigResolver:
    def __init__(
        self,
        repository: QueryRepository,
        settings: Settings,
        probe_credentials: TargetProbeCredentialResolver | None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._probe_credentials = probe_credentials

    def resolve(self, gateway_profile_id: UUID) -> ApimTargetClientConfig:
        settings = resolve_apim_target_settings(self._repository, self._settings)
        row = self._repository.get_apim_target(gateway_profile_id)
        if row is None:
            raise PolicyCompilationError("APIM target metadata is unavailable")
        target = ApimTarget.model_validate({name: row[name] for name in ApimTarget.model_fields})
        if target.gateway_profile_id != gateway_profile_id:
            raise PolicyCompilationError("Resolved APIM target identity does not match the request")
        if target.drift_state is ApimTargetDriftState.DRIFTED:
            raise PolicyCompilationError("APIM target identity is drifted")
        if target.management_auth_profile_ref != "system_managed_identity":
            raise PolicyCompilationError("APIM target management authentication is not supported")
        managed = [
            deployment
            for item in self._repository.list_gateway_deployments(gateway_profile_id)
            if (
                deployment := GatewayDeployment.model_validate(
                    {name: item[name] for name in GatewayDeployment.model_fields}
                )
            ).kind
            is GatewayDeploymentKind.MANAGED
            and deployment.retired_at is None
        ]
        if len(managed) != 1:
            raise PolicyCompilationError("APIM target requires exactly one active managed Gateway")
        deployment = managed[0]
        if (
            deployment.declared_state is not GatewayDeclaredState.PRESENT
            or deployment.reachability_state is not GatewayReachabilityState.REACHABLE
            or deployment.endpoint_url is None
        ):
            raise PolicyCompilationError(
                "APIM target managed Gateway is not declared and reachable"
            )
        legacy_target = (
            settings.apim_legacy_target_compatibility_enabled
            and target.gateway_profile_id == settings.apim_gateway_profile_id
        )
        if legacy_target and any(
            (
                (
                    str(target.subscription_id),
                    target.resource_group.casefold(),
                    target.service_name.casefold(),
                    target.api_id.casefold(),
                )
                != settings.legacy_apim_identity,
                str(target.principal_id) != str(settings.apim_principal_id),
                str(deployment.endpoint_url).rstrip("/")
                != str(settings.apim_gateway_url).rstrip("/"),
            )
        ):
            raise PolicyCompilationError("Legacy APIM target must match the configured identity")
        observer_url, observer_key = settings.usage_observer_for(gateway_profile_id)
        probe_subscription_id = target.probe_subscription_id or settings.apim_probe_subscription_id
        identity = {
            "gateway_profile_id": str(gateway_profile_id),
            "arm_resource_id": target.arm_resource_id.casefold(),
            "tenant_id": str(target.tenant_id),
            "api_id": target.api_id.casefold(),
            "api_path": target.api_path,
            "principal_id": str(target.principal_id) if target.principal_id else None,
            "management_auth_profile_ref": target.management_auth_profile_ref,
            "probe_subscription_id": probe_subscription_id,
            "probe_secret_ref": str(target.probe_secret_ref) if target.probe_secret_ref else None,
            "managed_gateway_id": deployment.native_gateway_id.casefold(),
            "managed_gateway_url": str(deployment.endpoint_url).rstrip("/"),
            "usage_observer_url": observer_url,
            "usage_observer_key_named_value": observer_key,
        }
        scoped = settings.model_copy(
            update={
                "apim_gateway_profile_id": gateway_profile_id,
                "azure_subscription_id": str(target.subscription_id),
                "apim_resource_group": target.resource_group,
                "apim_service_name": target.service_name,
                "apim_api_id": target.api_id,
                "apim_principal_id": str(target.principal_id) if target.principal_id else None,
                "apim_gateway_url": str(deployment.endpoint_url).rstrip("/"),
                "apim_dashboard_subscription_key": None,
                "apim_probe_subscription_key": (
                    self._probe_credentials.resolve(target) if self._probe_credentials else None
                ),
                "apim_probe_subscription_id": probe_subscription_id,
                "apim_usage_observer_url": observer_url,
                "apim_usage_observer_key_named_value": observer_key,
                "apim_usage_observers": {},
            }
        )
        return ApimTargetClientConfig(
            gateway_profile_id,
            _identity_sha256(identity),
            not legacy_target,
            scoped,
        )


class AzureApimPublisherClientResolver:
    def __init__(
        self,
        config_resolver: ApimTargetClientConfigResolver,
        builder: Callable[[Settings], ApimPublisherClient] | None = None,
    ) -> None:
        self._config_resolver = config_resolver
        self._builder = builder or AzureApimPublisherClient
        self._clients: dict[tuple[UUID, str], ApimPublisherClient] = {}

    def resolve(self, gateway_profile_id: UUID) -> ResolvedApimPublisherClient:
        config = self._config_resolver.resolve(gateway_profile_id)
        if config.gateway_profile_id != gateway_profile_id:
            raise PolicyCompilationError("APIM client resolver returned another target")
        key = (gateway_profile_id, config.target_identity_sha256)
        if key not in self._clients:
            self._clients[key] = self._builder(config.settings)
        return ResolvedApimPublisherClient(
            gateway_profile_id,
            config.target_identity_sha256,
            config.enforce_identity_snapshot,
            self._clients[key],
            (
                config.settings.apim_usage_observer_url,
                config.settings.apim_usage_observer_key_named_value,
            ),
        )


class AzureApimSubscriptionKeyClientResolver:
    def __init__(
        self,
        config_resolver: ApimTargetClientConfigResolver,
        builder: Callable[[Settings], ApimSubscriptionKeyClient] | None = None,
    ) -> None:
        self._config_resolver = config_resolver
        self._builder = builder or AzureApimSubscriptionKeyClient
        self._clients: dict[tuple[UUID, str], ApimSubscriptionKeyClient] = {}

    def resolve(self, gateway_profile_id: UUID) -> ApimSubscriptionKeyClient:
        config = self._config_resolver.resolve(gateway_profile_id)
        if config.gateway_profile_id != gateway_profile_id:
            raise PolicyCompilationError("APIM key client resolver returned another target")
        key = (gateway_profile_id, config.target_identity_sha256)
        if key not in self._clients:
            self._clients[key] = self._builder(config.settings)
        return self._clients[key]


def apim_target_client_config_resolver(
    repository: QueryRepository,
    settings: Settings,
    probe_credentials: TargetProbeCredentialResolver | None = None,
    *, include_probe_credentials: bool = True,
) -> ApimTargetClientConfigResolver:
    if not include_probe_credentials:
        settings = settings.model_copy(update={
            "apim_probe_subscription_key": None, "apim_dashboard_subscription_key": None,
        })
    if not settings.apim_target_registry_enabled:
        return LegacySettingsApimTargetClientConfigResolver(settings)
    return RepositoryApimTargetClientConfigResolver(
        repository,
        settings,
        (probe_credentials or AzureApimTargetProbeCredentialResolver())
        if include_probe_credentials else None,
    )
