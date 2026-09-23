from __future__ import annotations

from copy import deepcopy
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
    gateway_deployment_id_for,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_support import _validate_gateway_deployment_snapshots
from turnstile_core.services.control_plane import GatewayControlPlaneService


@pytest.mark.parametrize("change", ["retired", "missing", "target", "native", "endpoint"])
@pytest.mark.parametrize("retry", [False, True])
def test_publication_queue_and_retry_reject_stale_gateway_snapshots(
    change: str, retry: bool,
) -> None:
    repository = InMemoryRepository()
    repository.models = []
    repository.upsert_apim_target(target(drift_state="in_sync"))
    gateway = deployment(
        endpoint_url="https://unit.example.test/unit/llm", declared_state="present",
        last_arm_sync_at=NOW, reachability_state="reachable", last_probe_at=NOW,
    )
    repository.upsert_gateway_deployment(gateway)
    service = GatewayControlPlaneService(repository, TEST_CIPHER, deployment_scoping_enabled=True)
    publication = service.publish(bedrock_publication(), "unit-owner")
    if retry:
        repository.gateway_publications[0]["status"] = "failed"
    else:
        repository.gateway_publications.clear()
        repository.gateway_publication_outbox.clear()
        repository.gateway_publication_audit.clear()
        repository.gateway_publication_secrets.clear()
    row = repository.gateway_deployments[gateway.id]
    if change == "missing":
        repository.gateway_deployments.clear()
    elif change == "retired":
        row["retired_at"] = NOW
    elif change == "target":
        row["apim_target_id"] = UUID(int=99)
    elif change == "native":
        row["native_gateway_id"] = "another-gateway"
    else:
        row["endpoint_url"] = "https://other.example.test/unit/llm"
    before = deepcopy((
        repository.gateway_publications, repository.gateway_publication_outbox,
        repository.gateway_publication_audit, repository.gateway_publication_secrets,
    ))
    with pytest.raises(ValueError, match="Gateway deployment changed"):
        if retry:
            repository.requeue_gateway_publication(publication.id, "unit-owner", None)
        else:
            repository.create_gateway_publication(
                TARGET, publication.desired_spec.model_dump(mode="json"),
                publication.desired_spec_sha256, "model_add", "unit-owner",
            )
    assert before == (repository.gateway_publications, repository.gateway_publication_outbox,
                      repository.gateway_publication_audit, repository.gateway_publication_secrets)


def test_snapshot_validation_preserves_legacy_and_normalized_native_identity() -> None:
    _validate_gateway_deployment_snapshots(TARGET, {}, [])
    gateway = deployment(endpoint_url="https://unit.example.test/unit/llm")
    snapshot = {"gateway_deployments": [{
        "deployment_id": str(gateway.id), "native_gateway_id": "MANAGED",
        "endpoint_url": "https://unit.example.test/unit/llm/",
    }]}
    _validate_gateway_deployment_snapshots(TARGET, snapshot, [gateway.model_dump()])


def test_postgres_queue_checks_snapshot_after_target_lock_and_before_any_insert() -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    calls = []
    gateway = deployment(endpoint_url="https://unit.example.test/unit/llm", retired_at=NOW)

    def execute(sql: str, values: Any = None) -> MagicMock:
        calls.append((sql, values))
        result = MagicMock()
        result.fetchone.return_value = {"id": TARGET, "implementation": "apim", "enabled": True} \
            if "FROM gateway_profile" in sql else None
        result.fetchall.return_value = [gateway.model_dump()]
        return result

    connection.execute.side_effect = execute
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with (
        patch.object(repository, "_connection", return_value=context),
        pytest.raises(ValueError, match="Gateway deployment changed"),
    ):
        repository.create_gateway_publication(TARGET, {"gateway_deployments": [{
            "deployment_id": str(gateway.id), "native_gateway_id": "managed",
            "endpoint_url": str(gateway.endpoint_url),
        }]}, "unit-spec", "model_add", "unit-owner")
    connection.transaction.assert_called_once()
    assert calls[0][1] == (f"gateway-publication:{TARGET}",)
    assert "pg_advisory_xact_lock" in calls[0][0]
    assert "FROM gateway_deployment" in calls[-1][0]
    assert all("INSERT" not in sql for sql, _ in calls)


@pytest.mark.parametrize("blocked", ["publication", "model", "destination", None])
def test_postgres_reassociation_checks_both_target_locks_before_destination_insert(
    blocked: str | None,
) -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    destination_target = UUID(int=4)
    source = deployment(native_gateway_id="edge", kind="self_hosted", hosting_mode="docker")
    destination = source.model_copy(update={
        "id": gateway_deployment_id_for(destination_target, "edge"),
        "apim_target_id": destination_target,
    })
    calls = []

    def execute(sql: str, values: Any = None) -> MagicMock:
        calls.append((sql, values))
        result = MagicMock()
        actual = None
        if "FOR UPDATE" in sql:
            actual = {"id": source.id, "retired_at": None}
        elif "FROM gateway_publication" in sql and blocked == "publication":
            actual = {"id": UUID(int=82)}
        elif "FROM managed_model" in sql and blocked == "model":
            actual = {"id": UUID(int=83)}
        elif "endpoint_url IS NOT NULL" in sql and blocked == "destination":
            actual = {"id": destination.id}
        elif "INSERT INTO gateway_deployment" in sql:
            actual = destination.model_dump()
        result.fetchone.return_value = actual
        return result

    connection.execute.side_effect = execute
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with patch.object(repository, "_connection", return_value=context):
        if blocked:
            with pytest.raises(ValueError):
                repository.upsert_gateway_deployment(destination, retiring_deployment=source)
        else:
            result = repository.upsert_gateway_deployment(destination, retiring_deployment=source)
            assert result["id"] == destination.id
    assert [values for sql, values in calls if "pg_advisory_xact_lock" in sql] == [
        (f"gateway-publication:{identity}",)
        for identity in sorted([TARGET, destination_target], key=str)
    ]
    assert "FOR UPDATE" in calls[2][0]
    connection.transaction.assert_called_once()
    inserts = [index for index, (sql, _) in enumerate(calls) if sql.lstrip().startswith("INSERT")]
    if blocked:
        assert inserts == []
        assert all(not sql.lstrip().startswith("UPDATE") for sql, _ in calls)
    else:
        assert len(inserts) == 1
        assert all(index < inserts[0] for index, (sql, _) in enumerate(calls)
                   if "FROM gateway_publication" in sql or "FROM managed_model" in sql)
        assert "UPDATE gateway_deployment SET retired_at" in calls[-1][0]
        assert calls[-1][1] == (source.id, TARGET)


def test_reassociation_rechecks_arriving_publication_before_creating_destination() -> None:
    repository = InMemoryRepository()
    repository.models = []
    repository.upsert_apim_target(target())
    other_id = UUID(int=94)
    repository.gateways.append({**repository.gateways[0], "id": other_id})
    repository.upsert_apim_target(target(
        gateway_profile_id=other_id, service_name="other-service",
        arm_resource_id=target().arm_resource_id.replace("unit-service", "other-service"),
    ))
    source = deployment(native_gateway_id="edge", kind="self_hosted", hosting_mode="docker")
    before = repository.upsert_gateway_deployment(source)
    destination = source.model_copy(update={
        "id": gateway_deployment_id_for(other_id, "edge"), "apim_target_id": other_id,
    })
    repository.gateway_publications.append({"gateway_profile_id": TARGET, "status": "queued"})
    with pytest.raises(ValueError, match="publication"):
        repository.upsert_gateway_deployment(destination, retiring_deployment=source)
    assert repository.get_gateway_deployment(source.id) == before
    assert repository.get_gateway_deployment(destination.id) is None


def test_postgres_delayed_discovery_preserves_saved_name_and_retirement_atomically() -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    edge = deployment(native_gateway_id="edge", kind="self_hosted", hosting_mode="docker")
    connection.execute.return_value.fetchone.return_value = edge.model_dump()
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with patch.object(repository, "_connection", return_value=context):
        repository.upsert_gateway_deployment(edge, preserve_display_name=True)
    sql, values = connection.execute.call_args.args
    assert values["preserve_display_name"] is True
    assert "THEN existing.display_name ELSE EXCLUDED.display_name END" in sql
    assert "THEN COALESCE(existing.retired_at, EXCLUDED.retired_at)" in sql
    assert "existing.apim_target_id = EXCLUDED.apim_target_id" in sql
    connection.transaction.assert_called_once()


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
