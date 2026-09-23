from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from tests.backend.model_platform.control_plane_support import (
    ROOT,
    TEST_CIPHER,
    FakeApimClient,
    GatewayPublicationWorker,
    bedrock_publication,
)
from tests.backend.model_platform.test_gateway_targets import NOW, TARGET, deployment, target
from turnstile_core.domain.budget_counting import BudgetPublicationContracts
from turnstile_core.domain.gateway_targets import (
    ApimTargetRouteBinding,
    GatewayDeploymentInventory,
    GatewayDeploymentScope,
    GatewayDeploymentScopeMode,
    GatewayEndpointProbeResult,
    GatewayInstanceObservation,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services.control_plane import GatewayControlPlaneService


def test_registered_target_activation_persists_logical_identity_and_selected_route() -> None:
    repository = InMemoryRepository()
    repository.models = []
    repository.upsert_apim_target(target(drift_state="in_sync"))
    repository.upsert_gateway_deployment(deployment(
        endpoint_url="https://unit.example.test/unit/llm", declared_state="present",
        last_arm_sync_at=NOW, reachability_state="reachable", last_probe_at=NOW,
        release_gate="required",
    ))
    service = GatewayControlPlaneService(
        repository, TEST_CIPHER, deployment_scoping_enabled=True, target_registry_enabled=True,
    )
    request = bedrock_publication().model_copy(update={"deployment_scope": GatewayDeploymentScope(
        mode=GatewayDeploymentScopeMode.SELECTED, deployment_ids=(deployment().id,),
    )})
    publication = service.publish(request, "unit-owner")
    binding = publication.desired_spec.bindings[-1]
    contracts = BudgetPublicationContracts.capture(publication, {"models": []}, {
        "protocol_version": 2, "mode": "active", "epoch": str(UUID(int=10)),
        "gateway_profile_id": str(TARGET), "model_contracts": {},
    })
    assert contracts.model_contracts[binding.model.model_key].model_id == binding.logical_model_id
    client = FakeApimClient()
    worker = GatewayPublicationWorker(
        repository, client,
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        cipher=TEST_CIPHER,
    )
    evidence = GatewayEndpointProbeResult(
        deployment_id=deployment().id, native_gateway_id="managed", required=True,
        status="succeeded",
    )
    with patch.object(client, "probe_revision", return_value=(evidence,)):
        for _ in range(6):
            current = worker.run_once("unit-worker")
    assert current is not None and current.status == "active"
    assert current.resource_manifest["gateway_probe_results"] == [evidence.model_dump(mode="json")]
    assert repository.models[0]["id"] == binding.logical_model_id
    routes = repository.list_apim_target_route_bindings(apim_target_id=TARGET)
    assert len(routes) == 1 and routes[0]["logical_model_id"] == binding.logical_model_id
    assert routes[0]["deployment_scope"]["deployment_ids"] == (deployment().id,)
    assert routes[0]["is_default_serving"] and routes[0]["current_publication_id"] == publication.id


def test_inventory_replaces_atomically_and_older_observations_do_not_regress() -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(target())
    repository.upsert_gateway_deployment(deployment())
    snapshot = GatewayDeploymentInventory(
        deployment_id=deployment().id,
        source_api_version="2024-05-01",
        synced_at=NOW,
        associated_api_ids=("unit-api",),
    )
    original = repository.upsert_gateway_deployment_inventory(snapshot)
    assert (
        repository.upsert_gateway_deployment_inventory(
            snapshot.model_copy(
                update={
                    "synced_at": NOW - timedelta(seconds=1),
                    "associated_api_ids": ("obsolete",),
                }
            )
        )
        == original
    )
    assert repository.get_gateway_deployment_inventory(deployment().id) == original
    observation = GatewayInstanceObservation.model_validate(
        {
            "deployment_id": deployment().id,
            "native_instance_id": "unit-instance",
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "runtime_version": "newer",
            "source": "apim_context",
            "confidence": "observed",
        }
    )
    repository.upsert_gateway_instance_observation(observation)
    result = repository.upsert_gateway_instance_observation(
        observation.model_copy(
            update={
                "first_seen_at": NOW - timedelta(seconds=1),
                "last_seen_at": NOW - timedelta(seconds=1),
                "runtime_version": "older",
            }
        )
    )
    assert result["runtime_version"] == "newer" and result["last_seen_at"] == NOW
    assert repository.list_gateway_deployments_for_targets([]) == []
    assert repository.list_apim_target_route_bindings(apim_target_ids=[]) == []


def test_postgres_stale_inventory_never_deletes_newer_child_rows() -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    inventory = GatewayDeploymentInventory(
        deployment_id=deployment().id,
        source_api_version="2024-05-01",
        synced_at=NOW,
    )
    with (
        patch.object(repository, "_connection", return_value=context),
        patch.object(repository, "_deployment_inventories", return_value=[{"stored": True}]),
    ):
        assert repository.upsert_gateway_deployment_inventory(inventory) == {"stored": True}
    assert connection.execute.call_count == 1
    assert "EXCLUDED.synced_at >= existing.synced_at" in connection.execute.call_args.args[0]
    connection.cursor.assert_not_called()


def test_deployment_keeps_saved_name_and_retirement_during_delayed_inventory() -> None:
    repository = InMemoryRepository()
    repository.models = []
    repository.upsert_apim_target(target())
    edge = deployment(native_gateway_id="edge", kind="self_hosted", hosting_mode="docker")
    repository.upsert_gateway_deployment(edge)
    assert repository.rename_gateway_deployment(edge.id, "Confirmed name")
    repository.upsert_gateway_deployment(
        edge.model_copy(update={"retired_at": NOW}), preserve_display_name=True
    )
    stored = repository.upsert_gateway_deployment(edge, preserve_display_name=True)
    assert stored["display_name"] == "Confirmed name" and stored["retired_at"] == NOW
    assert repository.list_gateway_deployments(TARGET) == []
    assert len(repository.list_gateway_deployments(TARGET, include_retired=True)) == 1


@pytest.mark.parametrize("defect", ["runtime", "model", "deployment", "retired", "publication"])
def test_route_binding_checks_all_cross_target_dependencies(defect: str) -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(target())
    repository.upsert_gateway_deployment(deployment())
    runtime_id = UUID(int=81)
    model_id = UUID(int=82)
    repository.runtimes.append({"id": runtime_id, "gateway_profile_id": TARGET})
    repository.models.append({"id": model_id, "runtime_id": runtime_id})
    binding = ApimTargetRouteBinding(
        id=UUID(int=83),
        logical_model_id=model_id,
        runtime_id=runtime_id,
        apim_target_id=TARGET,
        deployment_scope=GatewayDeploymentScope(
            mode=GatewayDeploymentScopeMode.SELECTED,
            deployment_ids=(deployment().id,),
        ),
    )
    if defect == "runtime":
        binding.runtime_id = UUID(int=99)
    elif defect == "model":
        binding.logical_model_id = UUID(int=99)
    elif defect == "deployment":
        binding.deployment_scope = GatewayDeploymentScope(
            mode=GatewayDeploymentScopeMode.SELECTED, deployment_ids=(UUID(int=99),)
        )
    elif defect == "retired":
        repository.gateway_deployments[deployment().id]["retired_at"] = NOW
    else:
        binding.current_publication_id = UUID(int=99)
    with pytest.raises(ValueError):
        repository.upsert_apim_target_route_binding(binding)
    assert repository.list_apim_target_route_bindings() == []


def test_retirement_checks_published_dependencies_before_mutating() -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(target())
    edge = deployment(native_gateway_id="edge", kind="self_hosted", hosting_mode="docker")
    original = repository.upsert_gateway_deployment(edge)
    repository.gateway_publications.append({"gateway_profile_id": TARGET, "status": "queued"})
    with pytest.raises(ValueError, match="publication"):
        repository.upsert_gateway_deployment(
            edge.model_copy(update={"retired_at": NOW}), preserve_display_name=True
        )
    assert repository.get_gateway_deployment(edge.id) == original


def test_target_persistence_keeps_identity_and_returns_detached_snapshots() -> None:
    repository = InMemoryRepository()
    original = repository.upsert_apim_target(target())
    changed = repository.upsert_apim_target(target(service_tier="Basic"))
    assert changed["created_at"] == original["created_at"]
    assert changed["service_tier"] == "Basic"
    changed["service_name"] = "not-persisted"
    stored = repository.get_apim_target(TARGET)
    assert stored is not None and stored["service_name"] == "unit-service"
    with pytest.raises(ValueError, match="identity cannot be changed"):
        repository.upsert_apim_target(target(api_id="another-api"))
    assert len(repository.list_apim_targets()) == 1


def test_target_persistence_requires_profile_and_unique_service() -> None:
    repository = InMemoryRepository()
    with pytest.raises(ValueError, match="existing APIM"):
        repository.upsert_apim_target(target(gateway_profile_id=UUID(int=88)))
    repository.upsert_apim_target(target())
    repository.gateways.append({**repository.gateways[0], "id": UUID(int=88)})
    with pytest.raises(ValueError, match="identity already exists"):
        repository.upsert_apim_target(target(gateway_profile_id=UUID(int=88)))


@pytest.mark.parametrize(
    "gateway,updated",
    [
        (None, None),
        ({"implementation": "litellm"}, None),
        ({"implementation": "apim"}, None),
        ({"implementation": "apim"}, {"gateway_profile_id": TARGET}),
    ],
)
def test_postgres_target_upsert_fences_identity_in_transaction(
    gateway: dict[str, Any] | None,
    updated: dict[str, Any] | None,
) -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.side_effect = [gateway, updated]
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with patch.object(repository, "_connection", return_value=context):
        if updated is None:
            with pytest.raises(ValueError):
                repository.upsert_apim_target(target())
        else:
            assert repository.upsert_apim_target(target()) == updated
    connection.transaction.assert_called_once()
    if gateway is None or gateway["implementation"] != "apim":
        assert connection.execute.call_count == 1
    else:
        sql, values = connection.execute.call_args.args
        assert "existing.tenant_id = EXCLUDED.tenant_id" in sql
        assert "existing.api_path = EXCLUDED.api_path" in sql
        assert values["gateway_profile_id"] == str(TARGET)
