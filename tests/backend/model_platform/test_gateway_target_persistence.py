from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from pydantic import HttpUrl, SecretStr

from tests.backend.model_platform.control_plane_support import (
    ROOT,
    TEST_CIPHER,
    FakeApimClient,
    GatewayPublicationWorker,
    bedrock_publication,
)
from tests.backend.model_platform.control_plane_support import (
    GatewayControlPlaneService as LegacyGatewayControlPlaneService,
)
from tests.backend.model_platform.test_gateway_targets import NOW, TARGET, deployment, target
from turnstile_core.domain.budget_counting import BudgetPublicationContracts
from turnstile_core.domain.gateway_targets import (
    ApimTargetArmInventory,
    ApimTargetRouteBinding,
    GatewayArmInventory,
    GatewayDeploymentInventory,
    GatewayDeploymentScope,
    GatewayDeploymentScopeMode,
    GatewayEndpointProbeResult,
    GatewayInstanceObservation,
    SelfHostedGatewayBootstrap,
    SelfHostedGatewayBootstrapRequest,
    SelfHostedGatewayCreate,
    SelfHostedGatewayProvisionSpec,
    gateway_deployment_id_for,
)
from turnstile_core.integrations.apim_control_plane_contract import FixedApimPublisherClientResolver
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_support import _validate_gateway_deployment_snapshots
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
    GatewayControlPlaneService,
)
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker


def test_new_gateway_creation_queues_one_identity_without_an_existing_gateway() -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(
        target(
            capabilities={"self_hosted_gateway": "supported"},
            drift_state="in_sync",
            last_inventory_sync_at=NOW,
        )
    )
    service = GatewayControlPlaneService(repository, self_hosted_gateway_provisioning_enabled=True)
    write = SelfHostedGatewayCreate(
        display_name="New gateway", endpoint_url=HttpUrl("https://new.example.test/unit/llm"),
        endpoint_control_confirmed=True,
    )
    before = deepcopy(repository.registry())
    created = service.request_self_hosted_gateway_provision(TARGET, write, UUID(int=73), "owner")
    repeated = service.request_self_hosted_gateway_provision(TARGET, write, UUID(int=73), "owner")
    assert created.operation.id == repeated.operation.id
    assert len(repository.gateway_release_operations) == 1
    stored = repository.gateway_release_operations[0]
    spec = SelfHostedGatewayProvisionSpec.model_validate(stored["semantic_preview"])
    assert spec.native_gateway_id.startswith("turnstile-")
    assert stored["operation_kind"] == "self_hosted_gateway_provision"
    assert stored["status"] == "queued"
    assert repository.gateway_deployments == {}
    assert repository.registry() == before
    assert repository.gateway_release_operation_secrets == {}
    with pytest.raises(ControlPlaneConflictError, match="differently"):
        service.request_self_hosted_gateway_provision(
            TARGET, write.model_copy(update={"display_name": "Different"}), UUID(int=73), "owner",
        )
    with pytest.raises(ControlPlaneUnavailableError):
        GatewayControlPlaneService(repository).request_self_hosted_gateway_provision(
            TARGET, write, UUID(int=74), "owner",
        )


def test_gateway_creation_worker_materializes_only_after_arm_readback() -> None:
    repository = InMemoryRepository()
    registered = target(
        capabilities={"self_hosted_gateway": "supported"},
        drift_state="in_sync",
        last_inventory_sync_at=NOW,
    )
    repository.upsert_apim_target(registered)

    class Client(FakeApimClient):
        created: SelfHostedGatewayProvisionSpec | None = None
        bootstrap_reads = 0

        def ensure_self_hosted_gateway(
            self, spec: SelfHostedGatewayProvisionSpec, before_write: Callable[[], None],
        ) -> None:
            before_write()
            self.created = spec

        def discover_apim_target_inventory(self, gateway_id: UUID) -> ApimTargetArmInventory:
            assert gateway_id == TARGET and self.created is not None
            return ApimTargetArmInventory.model_validate({
                **registered.model_dump(include=set(ApimTargetArmInventory.model_fields)),
                "api_revision": "1", "managed_gateway_url": "https://unit.azure-api.net/unit/llm",
                "source_api_version": "2024-05-01", "discovered_at": datetime.now(UTC),
                "gateways": [GatewayArmInventory(
                    native_gateway_id=self.created.native_gateway_id,
                    arm_resource_id=(registered.arm_resource_id
                                     + "/gateways/" + self.created.native_gateway_id),
                    description=f"Turnstile gateway {TARGET}/{self.created.request_id}",
                    associated_api_ids=(registered.api_id,),
                )],
            })

        def self_hosted_gateway_bootstrap(
            self, spec: SelfHostedGatewayProvisionSpec,
            request: SelfHostedGatewayBootstrapRequest, image: str,
        ) -> SelfHostedGatewayBootstrap:
            self.bootstrap_reads += 1
            assert spec == self.created
            return SelfHostedGatewayBootstrap(
                deployment_id=spec.deployment_id, native_gateway_id=spec.native_gateway_id,
                configuration_endpoint=HttpUrl("https://unit.configuration.azure-api.net"),
                expires_at=datetime.now(UTC) + timedelta(hours=request.expires_in_hours),
                image=image,
                environment_file_name=spec.native_gateway_id + ".env",
                environment_file=SecretStr("config.service.auth=GatewayKey unit-secret"),
                docker_command="docker run --env-file gateway.env " + image,
                renewal_command="docker stop unit && docker rm unit",
            )

    client = Client()
    service = GatewayControlPlaneService(
        repository, self_hosted_gateway_provisioning_enabled=True,
        gateway_bootstrap_client_resolver=FixedApimPublisherClientResolver(client),
    )
    created = service.request_self_hosted_gateway_provision(
        TARGET, SelfHostedGatewayCreate(
            display_name="New edge", endpoint_url=HttpUrl("https://edge.example.test/unit/llm"),
            endpoint_control_confirmed=True,
        ), UUID(int=74), "owner",
    )
    enrollment = MagicMock(return_value=("managed",))
    worker = GatewayReleaseOperationWorker(
        repository, client, self_hosted_gateway_provisioning_enabled=True,
        gateway_protocol_enrollment=enrollment,
    )
    for phase in ("validating_dependencies", "verifying_readback", "succeeded"):
        advanced = worker.run_once("worker")
        assert advanced is not None and advanced.status == phase
        if phase != "succeeded":
            assert repository.gateway_deployments == {}
    spec = SelfHostedGatewayProvisionSpec.model_validate(created.operation.semantic_preview)
    stored = repository.get_gateway_deployment(spec.deployment_id)
    assert stored is not None
    assert stored["provisioning_operation_id"] == created.operation.id
    assert stored["declared_state"] == "present"
    assert stored["reachability_state"] == "unknown"
    assert str(stored["endpoint_url"]) == str(spec.configuration.endpoint_url)
    enrollment.assert_not_called()
    inventory_syncs = [
        row for row in repository.gateway_release_operations
        if row["operation_kind"] == "target_inventory_sync"
    ]
    assert len(inventory_syncs) == 1
    assert inventory_syncs[0]["gateway_profile_id"] == TARGET
    assert inventory_syncs[0]["status"] == "queued"
    repository.apim_targets[TARGET]["api_path"] = "changed/llm"
    with pytest.raises(ControlPlaneConflictError, match="identity is no longer verified"):
        service.self_hosted_gateway_bootstrap(
            spec.deployment_id, SelfHostedGatewayBootstrapRequest(endpoint_control_confirmed=True),
        )
    repository.apim_targets[TARGET]["api_path"] = registered.api_path
    bootstrap = service.self_hosted_gateway_bootstrap(
        spec.deployment_id, SelfHostedGatewayBootstrapRequest(endpoint_control_confirmed=True),
    )
    assert bootstrap.environment_file.get_secret_value().endswith("unit-secret")
    assert "unit-secret" not in service.release_operation(created.operation.id).model_dump_json()
    assert repository.gateway_release_operation_secrets == {}
    repository.gateway_deployments[spec.deployment_id]["endpoint_url"] = "https://other.example.test"
    with pytest.raises(ControlPlaneConflictError, match="identity is no longer verified"):
        service.self_hosted_gateway_bootstrap(
            spec.deployment_id, SelfHostedGatewayBootstrapRequest(endpoint_control_confirmed=True),
        )
    assert client.bootstrap_reads == 1


def test_failed_gateway_creation_retries_same_task_without_release_rollback() -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(
        target(
            capabilities={"self_hosted_gateway": "supported"},
            drift_state="in_sync",
            last_inventory_sync_at=NOW,
        )
    )
    service = GatewayControlPlaneService(repository, self_hosted_gateway_provisioning_enabled=True)
    created = service.request_self_hosted_gateway_provision(
        TARGET, SelfHostedGatewayCreate(
            display_name="New gateway", endpoint_url=HttpUrl("https://edge.example.test/unit/llm"),
            endpoint_control_confirmed=True,
        ), UUID(int=75), "owner",
    )
    worker = GatewayReleaseOperationWorker(
        repository, FakeApimClient(), self_hosted_gateway_provisioning_enabled=True,
    )
    worker.run_once("worker")
    failed = worker.run_once("worker")
    assert failed is not None and failed.status == "failed"
    assert all(item.to_status != "restoring" for item in failed.audit)
    assert repository.gateway_deployments == {}
    retried = service.retry_self_hosted_gateway_provision(failed.id, "owner")
    repeated = service.retry_self_hosted_gateway_provision(failed.id, "owner")
    assert retried.operation.id == repeated.operation.id == created.operation.id
    assert retried.operation.semantic_preview == created.operation.semantic_preview
    assert len(repository.gateway_release_operations) == 1
    assert retried.operation.audit[-1].detail["error_code"] == failed.error_code


@pytest.mark.parametrize("explicit", [False, True])
def test_invocation_query_uses_active_route_deployment_scope(explicit: bool) -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"model_id": UUID(int=17)}
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with patch.object(repository, "_connection", return_value=context):
        if explicit:
            repository.invocation_route(
                None, UUID(int=17), gateway_profile_id=TARGET,
                gateway_deployment_id=UUID(int=18),
            )
        else:
            repository.invocation_route(None, UUID(int=17))
    statement, parameters = connection.execute.call_args.args
    assert parameters == (
        [TARGET, UUID(int=18), UUID(int=17), TARGET] if explicit else [UUID(int=17)]
    )
    assert statement.count("%s") == len(parameters)
    assert "FROM apim_target_route_binding" in statement
    assert "target_route.enabled" in statement
    if explicit:
        assert "COALESCE(target_route.runtime_id, model.runtime_id)" in statement
        assert "AND deployment.id = %s" in statement
    else:
        assert "target_route.is_default_serving" in statement
    assert "target_route.deployment_scope_mode = 'all'" in statement
    assert "selected.gateway_deployment_id = deployment.id" in statement
    assert "deployment.apim_target_id = gateway.id" in statement
    assert "target_route.current_publication_id = publication.id" in statement
    assert "gateway_deployment_api_association" in statement
    assert "deployment.retired_at IS NULL" in statement
    assert "deployment.declared_state = 'present'" in statement
    assert "selected_deployment.id IS NOT NULL" in statement
    assert "target_route.id IS NOT NULL OR NOT EXISTS" in statement


@pytest.mark.parametrize("explicit_id", [False, True])
def test_postgres_gateway_creation_inserts_the_registration_identity(explicit_id: bool) -> None:
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"id": TARGET}
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    values: dict[str, Any] = {
        "name": "Registered APIM", "implementation": "apim", "base_url": None,
        "auth_type": "api_key", "credential_ciphertext": None, "credential_hint": None,
        "enabled": False, "is_default": False, "config": {},
    }
    if explicit_id:
        values["id"] = TARGET
    before = deepcopy(values)
    with patch.object(repository, "_connection", return_value=context):
        repository.create_registry_item("gateway", values)
        repository.create_registry_item("gateway", values)
    first, second = connection.execute.call_args_list
    statement, parameters = first.args
    normalized = " ".join(statement.split())
    assert "INSERT INTO gateway_profile ( id, name," in normalized
    assert ") VALUES (%(id)s, %(name)s," in normalized
    if explicit_id:
        assert parameters["id"] == TARGET
        assert second.args[1]["id"] == TARGET
    else:
        assert isinstance(parameters["id"], UUID)
        assert parameters["id"] != second.args[1]["id"]
    assert values == before


@pytest.mark.parametrize("scope_mode", ["all", "selected"])
@pytest.mark.parametrize("state", [
    "present", "retired", "absent", "unreachable", "disabled", "stale_release", "unassociated",
])
def test_invocation_route_respects_serving_scope_and_retirement(
    scope_mode: str, state: str,
) -> None:
    repository = InMemoryRepository()
    repository.upsert_apim_target(target())
    runtime = next(item for item in repository.runtimes if item.get("gateway_profile_id") == TARGET)
    model = next(item for item in repository.models
                 if item["runtime_id"] == runtime["id"] and item["enabled"])
    managed = deployment(
        endpoint_url="https://managed.example.test/unit/llm",
        declared_state="present", last_arm_sync_at=NOW,
    )
    edge = deployment(
        id=gateway_deployment_id_for(TARGET, "edge"),
        native_gateway_id="edge", kind="self_hosted", hosting_mode="docker",
        endpoint_url="https://edge.example.test/unit/llm",
        declared_state="present", last_arm_sync_at=NOW,
    )
    repository.upsert_gateway_deployment(managed)
    repository.upsert_gateway_deployment(edge)
    repository.gateway_deployment_inventories[edge.id] = {
        "associated_api_ids": [target().api_id], "hostname_configurations": [],
    }
    current_publication = UUID(int=72)
    repository.gateway_publications.append({
        "id": current_publication, "gateway_profile_id": TARGET, "status": "active",
        "desired_spec": {"bindings": []},
    })
    repository.effective_gateway_releases[TARGET] = current_publication
    binding = ApimTargetRouteBinding(
        id=UUID(int=71), logical_model_id=model["id"], apim_target_id=TARGET,
        runtime_id=runtime["id"], is_default_serving=True,
        current_publication_id=current_publication,
        deployment_scope=GatewayDeploymentScope(
            mode=GatewayDeploymentScopeMode(scope_mode),
            deployment_ids=(edge.id,) if scope_mode == "selected" else (),
        ),
    )
    repository.upsert_apim_target_route_binding(binding)
    if state == "retired":
        repository.gateway_deployments[edge.id]["retired_at"] = NOW
    elif state == "absent":
        repository.gateway_deployments[edge.id]["declared_state"] = "absent"
    elif state == "unreachable":
        repository.gateway_deployments[edge.id]["reachability_state"] = "unreachable"
    elif state == "disabled":
        repository.apim_target_route_bindings[binding.id]["enabled"] = False
    elif state == "stale_release":
        repository.effective_gateway_releases[TARGET] = UUID(int=73)
    elif state == "unassociated":
        repository.gateway_deployment_inventories[edge.id]["associated_api_ids"] = []
    route = repository.invocation_route(runtime["id"], model["id"])
    denied = {"disabled", "stale_release"}
    unavailable = {"retired", "absent", "unassociated"}
    if state in denied or (scope_mode == "selected" and state in unavailable):
        assert route is None
    else:
        assert route is not None
        assert route["gateway_deployment_id"] == (managed.id if scope_mode == "all" else edge.id)
    selected = repository.invocation_route(
        runtime["id"], model["id"], gateway_profile_id=TARGET,
        gateway_deployment_id=edge.id,
    )
    if state in denied | unavailable:
        assert selected is None
    else:
        assert selected is not None
        assert selected["gateway_deployment_id"] == edge.id
        assert selected["gateway_base_url"] == str(edge.endpoint_url)
        if state == "unreachable":
            assert selected["gateway_deployment"]["reachability_state"] == "unreachable"
    selected_managed = repository.invocation_route(
        runtime["id"], model["id"], gateway_profile_id=TARGET,
        gateway_deployment_id=managed.id,
    )
    if scope_mode == "selected" or state in denied:
        assert selected_managed is None
    else:
        assert selected_managed is not None
        assert selected_managed["gateway_deployment_id"] == managed.id


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
    intent = LegacyGatewayControlPlaneService(InMemoryRepository()).seed_legacy_publication(
        bedrock_publication(), "unit-owner",
    )

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
        repository.create_gateway_publication(TARGET, {
            "gateway_profile_id": str(TARGET),
            "discovery_models": [
                item.model_dump(mode="json") for item in intent.desired_spec.discovery_models
            ],
            "bindings": [intent.desired_spec.bindings[0].model_dump(mode="json")],
            "gateway_deployments": [{
                "deployment_id": str(gateway.id), "native_gateway_id": "managed",
                "kind": "managed", "release_gate": "required",
                "endpoint_url": str(gateway.endpoint_url),
            }],
        }, "unit-spec", "model_add", "unit-owner")
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
    service = LegacyGatewayControlPlaneService(
        repository, TEST_CIPHER, deployment_scoping_enabled=True, target_registry_enabled=True,
    )
    request = bedrock_publication().model_copy(update={"deployment_scope": GatewayDeploymentScope(
        mode=GatewayDeploymentScopeMode.SELECTED, deployment_ids=(deployment().id,),
    )})
    publication = service.seed_legacy_publication(request, "unit-owner")
    binding = publication.desired_spec.bindings[-1].model_copy(update={
        "logical_model_id": UUID(int=105),
    })
    publication.desired_spec = publication.desired_spec.model_copy(update={"bindings": [binding]})
    stored = repository.get_gateway_publication(publication.id)
    assert stored is not None
    stored["desired_spec"] = publication.desired_spec.model_dump(mode="json")
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
    connection.execute.return_value.fetchone.side_effect = [None, inventory.model_dump()]
    with (
        patch.object(repository, "_connection", return_value=context),
        patch.object(repository, "_deployment_inventory_view", return_value={"stored": True}),
    ):
        assert repository.upsert_gateway_deployment_inventory(inventory) == {"stored": True}
    assert connection.execute.call_count == 2
    assert "EXCLUDED.synced_at >= existing.synced_at" in (
        connection.execute.call_args_list[0].args[0]
    )
    assert "SELECT * FROM gateway_deployment_inventory" in (
        connection.execute.call_args_list[1].args[0]
    )
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
