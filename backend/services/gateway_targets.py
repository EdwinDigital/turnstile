from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from turnstile_core.config import Settings
from turnstile_core.domain.gateway_targets import (
    ApimTarget,
    ApimTargetDriftState,
    ApimTargetOwnershipMode,
    ApimTargetTopology,
    ApimTargetWrite,
    CapabilitySupport,
    GatewayDeploymentKind,
    GatewayReachabilityState,
)
from turnstile_core.integrations.apim_onboarding_client import (
    AzureApimPlatformConfigurationClient,
)
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.integrations.apim_subscription_key_client import AzureApimSubscriptionKeyClient
from turnstile_core.integrations.apim_target_client_factory import resolve_apim_target_settings
from turnstile_core.integrations.gateway_connection_probe import GatewayConnectionProbe
from turnstile_core.persistence.repository_contract import QueryRepository
from turnstile_core.security import CredentialCipher, credential_hint
from turnstile_core.services.control_plane import GatewayControlPlaneService

DEFAULT_SEEDED_APIM_GATEWAY_ID = UUID("10000000-0000-4000-8000-000000000001")


def register_existing_apim_target(
    registration: ApimTargetWrite,
    *,
    settings: Settings,
    repository: QueryRepository,
) -> ApimTargetTopology:
    registry = repository.registry()
    endpoint = str(registration.gateway_endpoint).rstrip("/")
    matches = [
        row
        for row in registry["gateways"]
        if row["implementation"] == "apim"
        and str(row.get("base_url") or "").rstrip("/") == endpoint
    ]
    if len(matches) > 1:
        raise ValueError("The existing Gateway endpoint identity is ambiguous")
    if matches:
        registration = registration.model_copy(
            update={
                "gateway_profile_id": UUID(str(matches[0]["id"])),
            }
        )
    else:
        registered_target_ids = {
            UUID(str(row["gateway_profile_id"])) for row in repository.list_apim_targets()
        }
        seeded = next(
            (
                row
                for row in registry["gateways"]
                if UUID(str(row["id"])) == DEFAULT_SEEDED_APIM_GATEWAY_ID
                and row["implementation"] == "apim"
                and row.get("base_url") is None
                and DEFAULT_SEEDED_APIM_GATEWAY_ID not in registered_target_ids
            ),
            None,
        )
        if seeded is not None:
            registration = registration.model_copy(
                update={"gateway_profile_id": DEFAULT_SEEDED_APIM_GATEWAY_ID}
            )
    resource_id = (registration.management_endpoint.path or "").rstrip("/")
    for profile in registry["gateways"]:
        configuration = profile.get("config") or {}
        initializing = configuration.get("onboarding_resource_id")
        if (
            initializing
            and configuration.get("onboarding_ready") is not True
            and (
                str(initializing).casefold() == resource_id.casefold()
                or str(profile["id"]) == str(registration.gateway_profile_id)
            )
        ):
            raise ValueError("This APIM initialization is incomplete; retry its existing task")
    previous = next(
        (
            row
            for row in repository.list_apim_targets()
            if str(row["arm_resource_id"]).casefold() == resource_id.casefold()
        ),
        None,
    )
    if previous is not None:
        previous_id = UUID(str(previous["gateway_profile_id"]))
        previous_profile = next(
            (row for row in registry["gateways"] if UUID(str(row["id"])) == previous_id), None
        )
        if previous_profile is not None and previous_profile["enabled"]:
            raise ValueError("The APIM instance is already registered")
        registration = registration.model_copy(update={"gateway_profile_id": previous_id})
    resolved = resolve_apim_target_settings(repository, settings)
    observer_url, observer_key = resolved.usage_observer_for(registration.gateway_profile_id)
    scoped = resolved.model_copy(
        update={
            "apim_gateway_profile_id": registration.gateway_profile_id,
            "azure_subscription_id": str(registration.subscription_id),
            "apim_resource_group": registration.resource_group,
            "apim_service_name": registration.service_name,
            "apim_api_id": registration.api_id,
            "apim_gateway_url": str(registration.gateway_endpoint),
            "apim_probe_subscription_key": None,
            "apim_probe_subscription_id": registration.probe_subscription_id,
            "apim_usage_observer_url": observer_url,
            "apim_usage_observer_key_named_value": observer_key,
        }
    )
    existing = next(
        (
            row
            for row in registry["gateways"]
            if UUID(str(row["id"])) == registration.gateway_profile_id
        ),
        None,
    )
    credential_values: dict[str, Any] = {}
    cipher = CredentialCipher.from_settings(resolved)
    with httpx.Client(timeout=30.0, follow_redirects=False) as management_http:
        inventory = AzureApimPublisherClient(
            scoped, client=management_http
        ).discover_apim_target_inventory(registration.gateway_profile_id)
        target_identity = (
            str(inventory.subscription_id),
            inventory.resource_group.casefold(),
            inventory.service_name.casefold(),
            inventory.api_id.casefold(),
        )
        if (
            resolved.legacy_apim_identity is not None
            and inventory.principal_id is not None
            and target_identity != resolved.legacy_apim_identity
        ):
            platform_settings = resolved.model_copy(
                update={
                    "apim_gateway_url": resolved.apim_gateway_url
                    or f"https://{resolved.apim_service_name}.azure-api.net",
                }
            )
            platform_client = AzureApimPlatformConfigurationClient(
                platform_settings, client=management_http
            )
            platform_client.ensure_apim_dependency_roles(
                registration.gateway_profile_id,
                inventory.principal_id,
                platform_client.configuration(),
            )
        if existing is None or existing.get("credential_ciphertext") is None:
            primary_key, _ = AzureApimSubscriptionKeyClient(
                scoped,
                client=management_http,
            ).application_subscription_keys(registration.dashboard_subscription_id)
            credential_values = {
                "credential_ciphertext": cipher.encrypt(primary_key),
                "credential_hint": credential_hint(primary_key),
            }
        else:
            if existing["enabled"] and (
                existing["name"] != registration.name
                or str(existing.get("base_url") or "").rstrip("/") != endpoint
            ):
                raise ValueError("The existing Gateway profile does not match this target")
            stored_key = cipher.decrypt(existing["credential_ciphertext"])
            if stored_key is None:
                raise ValueError("The existing Gateway credential is unavailable")
            primary_key = stored_key
        GatewayConnectionProbe().validate(
            registration.gateway_endpoint,
            header_name=str(
                (existing or {})
                .get("config", {})
                .get(
                    "header_name",
                    "Ocp-Apim-Subscription-Key",
                )
            ),
            credential=primary_key,
        )
        probed_at = datetime.now(UTC)
    values = {
        "name": registration.name,
        "implementation": "apim",
        "base_url": endpoint,
        "auth_type": "api_key",
        "enabled": True,
        "is_default": False,
        "config": {"header_name": "Ocp-Apim-Subscription-Key"},
        **credential_values,
    }
    if existing is None:
        repository.create_registry_item(
            "gateway", {"id": registration.gateway_profile_id, **values}
        )
    else:
        if existing["implementation"] != "apim":
            raise ValueError("The target ID belongs to a non-APIM Gateway profile")
        if existing["enabled"] and existing.get("base_url") is not None:
            if (
                existing["name"] != registration.name
                or str(existing.get("base_url") or "").rstrip("/") != endpoint
            ):
                raise ValueError("The existing Gateway profile does not match this target")
            if credential_values:
                repository.update_registry_item(
                    "gateway", registration.gateway_profile_id, credential_values
                )
        else:
            repository.update_registry_item(
                "gateway",
                registration.gateway_profile_id,
                {**values, "is_default": bool(existing.get("is_default"))},
            )
    if registration.gateway_profile_id != DEFAULT_SEEDED_APIM_GATEWAY_ID:
        registered_target_ids = {
            UUID(str(row["gateway_profile_id"])) for row in repository.list_apim_targets()
        }
        seeded_placeholder = next(
            (
                row
                for row in registry["gateways"]
                if UUID(str(row["id"])) == DEFAULT_SEEDED_APIM_GATEWAY_ID
                and row["implementation"] == "apim"
                and row.get("base_url") is None
                and row.get("enabled")
                and DEFAULT_SEEDED_APIM_GATEWAY_ID not in registered_target_ids
            ),
            None,
        )
        if seeded_placeholder is not None:
            repository.update_registry_item(
                "gateway",
                DEFAULT_SEEDED_APIM_GATEWAY_ID,
                {"enabled": False, "is_default": False},
            )
    capabilities = inventory.capabilities.model_copy(
        update={
            "response_observer": CapabilitySupport.SUPPORTED
            if observer_url
            else CapabilitySupport.UNSUPPORTED,
        }
    )
    inventory = inventory.model_copy(update={"capabilities": capabilities})
    repository.upsert_apim_target(
        ApimTarget(
            gateway_profile_id=registration.gateway_profile_id,
            arm_resource_id=inventory.arm_resource_id,
            tenant_id=registration.tenant_id,
            subscription_id=inventory.subscription_id,
            resource_group=inventory.resource_group,
            service_name=inventory.service_name,
            api_id=inventory.api_id,
            api_path=inventory.api_path,
            service_tier=inventory.service_tier,
            tier_family=inventory.tier_family,
            service_location=inventory.service_location,
            principal_id=inventory.principal_id,
            management_auth_profile_ref=registration.management_auth_profile_ref,
            probe_subscription_id=registration.probe_subscription_id,
            ownership_mode=ApimTargetOwnershipMode.ADOPTED,
            capabilities=capabilities,
            last_validated_at=inventory.discovered_at,
            drift_state=ApimTargetDriftState.IN_SYNC,
        )
    )
    service = GatewayControlPlaneService(repository)
    topology = service.apply_target_inventory(inventory)
    managed = next(
        item.deployment
        for item in topology.deployments
        if item.deployment.kind is GatewayDeploymentKind.MANAGED
    )
    repository.upsert_gateway_deployment(
        managed.model_copy(
            update={
                "reachability_state": GatewayReachabilityState.REACHABLE,
                "last_probe_at": probed_at,
            }
        )
    )
    return service.apim_target_topology(registration.gateway_profile_id)
