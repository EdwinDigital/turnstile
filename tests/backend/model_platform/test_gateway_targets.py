from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from turnstile_core.domain.gateway_targets import (
    ApimTarget,
    ApimTargetCapabilitySnapshot,
    ApimTargetRouteBinding,
    ApimTargetWrite,
    GatewayArmInventory,
    GatewayDeployment,
    GatewayDeploymentScope,
    GatewayInstanceObservation,
    gateway_deployment_id_for,
)

TARGET = UUID("10000000-0000-4000-8000-000000000001")
SUBSCRIPTION = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 23, tzinfo=UTC)


def target(**changes: Any) -> ApimTarget:
    return ApimTarget.model_validate(
        {
            "gateway_profile_id": TARGET,
            "tenant_id": UUID(int=3),
            "subscription_id": SUBSCRIPTION,
            "resource_group": "unit-group",
            "service_name": "unit-service",
            "api_id": "unit-api",
            "api_path": "unit/llm",
            "arm_resource_id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/unit-group"
            "/providers/Microsoft.ApiManagement/service/unit-service",
            "service_tier": "Developer",
            "tier_family": "classic",
            "service_location": "eastus",
            "ownership_mode": "adopted",
            **changes,
        }
    )


def deployment(**changes: Any) -> GatewayDeployment:
    return GatewayDeployment.model_validate(
        {
            "id": gateway_deployment_id_for(TARGET, "managed"),
            "apim_target_id": TARGET,
            "native_gateway_id": "managed",
            "kind": "managed",
            "display_name": "Managed",
            "hosting_mode": "azure_managed",
            "lifecycle_ownership": "customer",
            **changes,
        }
    )


def test_gateway_defaults_do_not_claim_connectivity_or_capabilities() -> None:
    assert target().capabilities.table_ledger == "unknown"
    assert deployment().reachability_state == "unknown"
    assert deployment().declared_state == "unknown"
    assert gateway_deployment_id_for(TARGET, "MANAGED") == gateway_deployment_id_for(
        TARGET, "managed"
    )
    assert gateway_deployment_id_for(UUID(int=9), "managed") != deployment().id


@pytest.mark.parametrize(
    "changes",
    [
        {"subscription_id": UUID(int=99)},
        {"api_path": "/unit/llm"},
        {"api_path": "unit/llm/"},
        {"tier_family": "v2", "capabilities": {"self_hosted_gateway": "supported"}},
    ],
)
def test_gateway_target_rejects_conflicting_identity(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        target(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"native_gateway_id": "edge"},
        {"hosting_mode": "docker"},
        {"kind": "self_hosted"},
        {"declared_state": "present"},
        {"reachability_state": "reachable"},
        {"observation_state": "observed"},
        {"endpoint_url": "https://user:secret@unit.test"},
        {"endpoint_url": "http://unit.test"},
        {"endpoint_url": "https://unit.test/?token=unit"},
    ],
)
def test_gateway_deployment_rejects_unproved_or_unsafe_state(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        deployment(**changes)


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "selected"},
        {"deployment_ids": [TARGET]},
        {"mode": "selected", "deployment_ids": [TARGET, TARGET]},
    ],
)
def test_gateway_scope_rejects_ambiguous_selection(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GatewayDeploymentScope.model_validate(values)


def test_gateway_capability_and_instance_evidence_is_coherent() -> None:
    with pytest.raises(ValueError):
        ApimTargetCapabilitySnapshot(source_api_version="2024-05-01")
    observation = {
        "deployment_id": deployment().id,
        "native_instance_id": "unit-instance",
        "source": "apim_context",
        "confidence": "observed",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
    }
    assert GatewayInstanceObservation.model_validate(observation).last_seen_at == NOW
    with pytest.raises(ValueError):
        GatewayInstanceObservation.model_validate(
            {
                **observation,
                "last_seen_at": NOW - timedelta(seconds=1),
            }
        )


@pytest.mark.parametrize(
    "change",
    [
        {"management_endpoint": "https://foreign.example.test"},
        {"gateway_endpoint": "http://unit.example.test"},
        {"gateway_endpoint": "https://unit.example.test/#credential"},
        {"subscription_id": str(UUID(int=99))},
    ],
)
def test_target_registration_rejects_unbound_endpoints(change: dict[str, Any]) -> None:
    values = {
        "name": "Unit target",
        "tenant_id": str(UUID(int=3)),
        "subscription_id": str(SUBSCRIPTION),
        "resource_group": "unit-group",
        "service_name": "unit-service",
        "api_id": "unit-api",
        "management_endpoint": "https://management.azure.com" + target().arm_resource_id,
        "gateway_endpoint": "https://unit.example.test/llm",
    }
    assert ApimTargetWrite.model_validate(values).dashboard_subscription_id == "turnstile-dashboard"
    with pytest.raises(ValueError):
        ApimTargetWrite.model_validate({**values, **change})


def test_route_and_inventory_reject_disabled_default_and_duplicate_identifiers() -> None:
    with pytest.raises(ValueError, match="must be enabled"):
        ApimTargetRouteBinding.model_validate(
            {
                "id": UUID(int=6),
                "logical_model_id": UUID(int=7),
                "apim_target_id": TARGET,
                "runtime_id": UUID(int=8),
                "enabled": False,
                "is_default_serving": True,
            }
        )
    with pytest.raises(ValueError, match="must be unique"):
        GatewayArmInventory(
            native_gateway_id="edge",
            arm_resource_id="/unit/edge",
            associated_api_ids=("unit-api", "UNIT-API"),
        )
