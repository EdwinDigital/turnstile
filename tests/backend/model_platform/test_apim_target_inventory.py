from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
import pytest
from pydantic import HttpUrl

from backend.services.gateway_targets import register_existing_apim_target
from tests.backend.model_platform.control_plane_support import (
    TEST_CIPHER,
    FakeApimClient,
    bedrock_publication,
)
from tests.backend.model_platform.test_gateway_targets import NOW, TARGET, deployment, target
from turnstile_core.config import Settings
from turnstile_core.domain.gateway_targets import (
    ApimTargetArmInventory,
    ApimTargetRouteBinding,
    ApimTargetWrite,
    GatewayDeploymentInventory,
    GatewayDeploymentScope,
    GatewayDeploymentScopeMode,
    gateway_deployment_id_for,
)
from turnstile_core.integrations.apim_control_plane_contract import ResolvedApimPublisherClient
from turnstile_core.integrations.gateway_connection_probe import (
    GatewayConnectionProbe,
    GatewayConnectionProbeError,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
    GatewayControlPlaneService,
)
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker


@pytest.mark.parametrize("scope_error", [None, "feature-off", "foreign", "association", "required"])
def test_publication_deployment_scope_is_resolved_before_queueing(scope_error: str | None) -> None:
    repository = InMemoryRepository()
    repository.models = []
    repository.upsert_apim_target(target(
        drift_state="in_sync", capabilities={"self_hosted_gateway": "supported"},
    ))
    repository.upsert_gateway_deployment(deployment(
        endpoint_url="https://unit.example.test/unit/llm", declared_state="present",
        last_arm_sync_at=NOW, reachability_state="reachable", last_probe_at=NOW,
        release_gate="required",
    ))
    edge_id = gateway_deployment_id_for(TARGET, "edge")
    repository.upsert_gateway_deployment(deployment(
        id=edge_id, native_gateway_id="edge", kind="self_hosted", hosting_mode="docker",
        endpoint_url="https://edge.example.test/unit/llm", declared_state="present",
        last_arm_sync_at=NOW, release_gate="optional",
    ))
    repository.upsert_gateway_deployment_inventory(GatewayDeploymentInventory(
        deployment_id=edge_id, source_api_version="2024-05-01", synced_at=NOW,
        associated_api_ids=("other-api" if scope_error == "association" else "unit-api",),
    ))
    if scope_error == "required":
        repository.gateway_deployments[deployment().id]["reachability_state"] = "unknown"
    request = bedrock_publication().model_copy(update={"deployment_scope": GatewayDeploymentScope(
        mode=GatewayDeploymentScopeMode.SELECTED,
        deployment_ids=(UUID(int=99) if scope_error == "foreign" else edge_id,),
    )})
    service = GatewayControlPlaneService(
        repository, TEST_CIPHER, deployment_scoping_enabled=scope_error != "feature-off",
        target_registry_enabled=True,
    )
    if scope_error is not None:
        with pytest.raises((ControlPlaneConflictError, ControlPlaneUnavailableError)):
            service.publish(request, "unit-owner")
        assert repository.gateway_publications == []
    else:
        publication = service.publish(request, "unit-owner")
        binding = publication.desired_spec.bindings[-1]
        assert binding.deployment_scope.deployment_ids == (edge_id,)
        assert binding.serving_deployments[0].native_gateway_id == "edge"
        assert binding.logical_model_id is not None
        assert len(publication.desired_spec.gateway_deployments) == 2


@pytest.mark.parametrize("wrong_target", [False, True])
def test_inventory_queue_resolves_exact_target_and_persists_checkpoint(wrong_target: bool) -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(target())
    service = GatewayControlPlaneService(repository, target_inventory_sync_enabled=True)
    accepted = service.request_target_inventory_sync(TARGET, "unit-owner")
    client = Mock(spec=FakeApimClient)
    client.discover_apim_target_inventory = Mock(return_value=arm_inventory())
    resolver = Mock()
    resolver.resolve.return_value = ResolvedApimPublisherClient(
        UUID(int=99) if wrong_target else TARGET, "unit-fingerprint", True, client,
    )
    worker = GatewayReleaseOperationWorker(repository, resolver)
    first = worker.run_once("unit-worker")
    assert first is not None
    if wrong_target:
        assert first.status == "failed"
        assert "another Release operation target" in (first.error_message or "")
        client.discover_apim_target_inventory.assert_not_called()
    else:
        assert first.status == "validating_dependencies"
        result = worker.run_once("unit-worker")
        assert result is not None and result.id == accepted.operation.id
        assert result.status == "succeeded" and result.checkpoint["gateway_count"] == 1
        assert result.checkpoint["read_only_arm_discovery"] is True
        client.discover_apim_target_inventory.assert_called_once_with(TARGET)
    assert worker._active_client.get() is None


@pytest.mark.parametrize("probe_fails", [False, True])
def test_registration_persists_only_after_connection_probe(probe_fails: bool) -> None:
    repository = InMemoryRepository()
    repository.gateways = []
    registration = ApimTargetWrite.model_validate(
        {
            "gateway_profile_id": TARGET,
            "name": "Unit APIM",
            "tenant_id": str(target().tenant_id),
            "subscription_id": str(target().subscription_id),
            "resource_group": "unit-group",
            "service_name": "unit-service",
            "api_id": "unit-api",
            "management_endpoint": "https://management.azure.com" + target().arm_resource_id,
            "gateway_endpoint": "https://unit.azure-api.net/unit/llm",
        }
    )
    with (
        patch(
            "backend.services.gateway_targets.CredentialCipher.from_settings",
            return_value=TEST_CIPHER,
        ),
        patch("backend.services.gateway_targets.AzureApimPublisherClient") as publisher,
        patch("backend.services.gateway_targets.AzureApimSubscriptionKeyClient") as keys,
        patch("backend.services.gateway_targets.GatewayConnectionProbe") as probe,
    ):
        publisher.return_value.discover_apim_target_inventory.return_value = arm_inventory(
            managed_gateway_url=registration.gateway_endpoint,
        )
        keys.return_value.application_subscription_keys.return_value = ("unit-key", "unit-other")
        if probe_fails:
            probe.return_value.validate.side_effect = GatewayConnectionProbeError("unavailable")
            with pytest.raises(GatewayConnectionProbeError):
                register_existing_apim_target(
                    registration, settings=Settings(), repository=repository
                )
            assert repository.gateways == [] and repository.apim_targets == {}
        else:
            result = register_existing_apim_target(
                registration,
                settings=Settings(),
                repository=repository,
            )
            assert result.target.gateway_profile_id == TARGET
            assert result.deployments[0].deployment.reachability_state == "reachable"
            assert "unit-key" not in result.model_dump_json()
            saved = repository.gateways[0]
            assert TEST_CIPHER.decrypt(saved["credential_ciphertext"]) == "unit-key"


@pytest.mark.parametrize("status", [200, 302, 401, 503])
def test_connection_probe_is_get_only_without_redirects(status: int) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://other.example.test"})

    probe = GatewayConnectionProbe(httpx.Client(transport=httpx.MockTransport(handler)))
    arguments = {"header_name": "Ocp-Apim-Subscription-Key", "credential": "unit-key"}
    if status == 200:
        probe.validate(HttpUrl("https://unit.example.test/unit/llm"), **arguments)
    else:
        with pytest.raises(GatewayConnectionProbeError, match=f"HTTP {status}"):
            probe.validate(HttpUrl("https://unit.example.test/unit/llm"), **arguments)
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == "/unit/llm/v1/models"


def arm_inventory(**changes: Any) -> ApimTargetArmInventory:
    metadata = target().model_dump()
    return ApimTargetArmInventory.model_validate(
        {
            **{
                name: metadata[name]
                for name in ApimTargetArmInventory.model_fields
                if name in metadata
            },
            "api_revision": "1",
            "managed_gateway_url": "https://unit.azure-api.net",
            "source_api_version": "2024-05-01",
            "discovered_at": NOW,
            **changes,
        }
    )


def test_inventory_does_not_claim_reachability_or_reactivate_retired_gateway() -> None:
    repository = InMemoryRepository()
    topology_registry(repository)
    edge_id = gateway_deployment_id_for(TARGET, "edge")
    repository.gateway_deployments[edge_id]["display_name"] = "Confirmed name"
    repository.gateway_deployments[edge_id]["retired_at"] = NOW
    inventory = arm_inventory(
        gateways=[
            {
                "native_gateway_id": "edge",
                "arm_resource_id": target().arm_resource_id + "/gateways/edge",
                "location_name": "ARM name",
                "associated_api_ids": ["unit-api"],
            }
        ]
    )
    result = GatewayControlPlaneService(repository).apply_target_inventory(inventory)
    assert result.connection_state == "connected"
    assert result.self_hosted_gateway_count == 0
    assert result.deployments[0].deployment.reachability_state == "unknown"
    edge = repository.get_gateway_deployment(edge_id)
    assert edge is not None and edge["display_name"] == "Confirmed name"
    assert edge["retired_at"] == NOW and edge["declared_state"] == "present"
    stored = repository.get_gateway_deployment_inventory(edge_id)
    assert stored is not None and stored["associated_api_ids"] == ("unit-api",)


def test_inventory_rejects_identity_drift_before_deployment_changes_and_ignores_old_reads() -> None:
    repository = InMemoryRepository()
    topology_registry(repository)
    service = GatewayControlPlaneService(repository)
    before = repository.list_gateway_deployments(TARGET)
    with pytest.raises(ControlPlaneConflictError, match="immutable target identity"):
        service.apply_target_inventory(arm_inventory(api_path="other/llm"))
    assert repository.list_gateway_deployments(TARGET) == before
    result = service.apply_target_inventory(arm_inventory(discovered_at=NOW - timedelta(seconds=1)))
    assert result.connection_state == "unavailable"
    assert result.target.last_inventory_sync_at == NOW


def topology_registry(repository: InMemoryRepository) -> dict[str, Any]:
    repository.upsert_apim_target(target())
    repository.upsert_gateway_deployment(deployment(endpoint_url="https://unit.azure-api.net"))
    repository.upsert_gateway_deployment(
        deployment(
            id=gateway_deployment_id_for(TARGET, "edge"),
            native_gateway_id="edge",
            kind="self_hosted",
            hosting_mode="docker",
            display_name="Unit edge",
            endpoint_url="https://edge.example.test",
        )
    )
    return {
        "gateways": repository.gateways,
        "runtimes": [{"id": UUID(int=81), "gateway_profile_id": TARGET}],
        "models": [
            {
                "id": UUID(int=82),
                "runtime_id": UUID(int=81),
                "model_key": "unit-model",
                "display_name": "Unit model",
            }
        ],
    }


def test_topology_uses_persisted_unknown_state_and_legacy_model_membership() -> None:
    repository = InMemoryRepository()
    registry = topology_registry(repository)
    result = GatewayControlPlaneService(repository).apim_target_topologies(registry)
    assert result.total == 1
    assert not result.inventory_sync_available
    assert not result.resource_lifecycle_available
    assert not result.onboarding_available
    item = result.items[0]
    assert item.connection_state == "unknown"
    assert item.self_hosted_gateway_count == 1
    assert item.published_model_count == 1
    assert all(len(child.published_models) == 1 for child in item.deployments)
    assert str(item.gateway_endpoint) == "https://unit.azure-api.net/"


@pytest.mark.parametrize("enabled", [True, False])
def test_topology_honors_selected_or_disabled_route(enabled: bool) -> None:
    repository = InMemoryRepository()
    registry = topology_registry(repository)
    repository.models = registry["models"]
    repository.runtimes = registry["runtimes"]
    repository.upsert_apim_target_route_binding(
        ApimTargetRouteBinding(
            id=UUID(int=83),
            logical_model_id=UUID(int=82),
            apim_target_id=TARGET,
            runtime_id=UUID(int=81),
            enabled=enabled,
            deployment_scope=GatewayDeploymentScope(
                mode=GatewayDeploymentScopeMode.SELECTED,
                deployment_ids=(gateway_deployment_id_for(TARGET, "edge"),),
            ),
        )
    )
    result = GatewayControlPlaneService(repository).apim_target_topologies(registry)
    children = {item.deployment.native_gateway_id: item for item in result.items[0].deployments}
    assert children["managed"].published_models == ()
    assert len(children["edge"].published_models) == int(enabled)


def test_topology_filters_disabled_targets_and_batches_repository_reads() -> None:
    repository = InMemoryRepository()
    registry = topology_registry(repository)
    with (
        patch.object(
            repository,
            "list_gateway_deployments_for_targets",
            wraps=repository.list_gateway_deployments_for_targets,
        ) as deployments,
        patch.object(
            repository,
            "list_gateway_deployment_inventories",
            wraps=repository.list_gateway_deployment_inventories,
        ) as inventories,
        patch.object(repository, "registry", side_effect=AssertionError("unexpected full read")),
    ):
        assert GatewayControlPlaneService(repository).apim_target_topologies(registry).total == 1
    deployments.assert_called_once_with([TARGET])
    inventories.assert_called_once()
    for gateway in registry["gateways"]:
        gateway["enabled"] = False
    assert GatewayControlPlaneService(repository).apim_target_topologies(registry).total == 0
    with pytest.raises(ControlPlaneNotFoundError):
        GatewayControlPlaneService(repository).apim_target_topology(TARGET)
