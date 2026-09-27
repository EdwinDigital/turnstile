from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from tests.backend.model_platform.control_plane_support import TEST_CIPHER, StubTokenProvider
from tests.backend.model_platform.test_application_provisioning import AdmissionRows
from tests.backend.model_platform.test_gateway_targets import NOW, deployment, target
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.domain.application_access import application_id_for
from turnstile_core.domain.gateway_targets import (
    ApimOnboardingConfiguration,
    ApimOnboardingPlan,
    ApimOnboardingRequest,
    ApimOnboardingStart,
    ApimTargetArmInventory,
    gateway_deployment_id_for,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_onboarding_client import (
    AzureApimOnboardingClient,
    AzureApimPlatformConfigurationClient,
    onboarding_configuration,
    onboarding_plan_digest,
)
from turnstile_core.integrations.apim_onboarding_policy import (
    onboarding_canonical_parent,
    onboarding_policy_configuration_digest,
    render_onboarding_parent,
)
from turnstile_core.integrations.apim_policy_components import (
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)
from turnstile_core.integrations.ledger import TableStorageLedger, application_partition_key
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services.apim_onboarding import (
    ApimOnboardingService,
    apim_onboarding_available,
    build_apim_onboarding_service,
    initialized_apim_targets,
    onboarding_template_path,
)
from turnstile_core.services.apim_onboarding_ledger import (
    enroll_self_hosted_gateway_protocol,
    project_onboarding_ledger,
)
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    GatewayControlPlaneService,
)
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker


def protocol_fixture() -> tuple[
    InMemoryRepository, ApimTargetArmInventory, dict[str, Any], MagicMock, str
]:
    request = onboarding_request()
    repository = InMemoryRepository()
    repository.gateways.append({**repository.gateways[0], "id": request.gateway_profile_id})
    metadata = target(
        gateway_profile_id=request.gateway_profile_id,
        tenant_id=request.tenant_id,
        subscription_id=request.subscription_id,
        resource_group=request.resource_group,
        service_name=request.service_name,
        arm_resource_id=request.resource_id,
        api_id=request.api_id,
        api_path=request.api_path,
        principal_id=UUID(int=47),
        capabilities={"self_hosted_gateway": "supported"},
    )
    repository.upsert_apim_target(metadata)
    repository.upsert_gateway_deployment(
        deployment(
            id=gateway_deployment_id_for(request.gateway_profile_id, "edge"),
            apim_target_id=request.gateway_profile_id,
            native_gateway_id="edge",
            kind="self_hosted",
            hosting_mode="docker",
            endpoint_url="https://edge.example.test/unit/llm",
        )
    )
    inventory = ApimTargetArmInventory.model_validate(
        {
            **{
                name: value
                for name, value in metadata.model_dump().items()
                if name in ApimTargetArmInventory.model_fields
            },
            "api_revision": "1",
            "managed_gateway_url": "https://unit-service.azure-api.net",
            "source_api_version": "2024-05-01",
            "discovered_at": NOW,
            "gateways": [
                {
                    "native_gateway_id": "edge",
                    "arm_resource_id": request.resource_id + "/gateways/edge",
                    "associated_api_ids": [request.api_id],
                }
            ],
        }
    )
    GatewayControlPlaneService(repository).apply_target_inventory(inventory)
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    config = governance_configuration()
    canonical = onboarding_canonical_parent(source, request.api_id, config)
    client = MagicMock()
    client.current_api_policy.return_value = (
        "1",
        render_onboarding_parent(request, config, source),
    )
    client.current_revision.return_value = "1"
    fence: dict[str, Any] = {
        "protocol_version": 2,
        "epoch": str(UUID(int=72)),
        "mode": "active",
        "gateway_profile_id": str(request.gateway_profile_id),
        "api_id": request.api_id,
        "apim_service_id": request.service_name,
        "gateways": {"managed": ["1", "previous"]},
        "model_contracts": {"unit-model": {"counting": "strict"}},
        "extension": {"keep": True},
    }
    return repository, inventory, fence, client, canonical


class ProtocolFenceRows:
    def __init__(self, fence: dict[str, Any]) -> None:
        self.document = deepcopy(fence)
        self.version = 1
        self.writes: list[dict[str, Any]] = []
        self.reads = 0
        self.before_replace: Callable[[dict[str, Any]], bool] | None = None

    def read_versioned_entity(self, partition: str, row_key: str) -> tuple[dict[str, Any], str]:
        assert partition.startswith("budget-protocol|") and row_key == "F"
        self.reads += 1
        return {
            "Document": json.dumps(self.document),
            "Preserved": "unit",
            "Timestamp": "old",
            "odata.etag": "metadata-only",
        }, f'W/"{self.version}"'

    def replace_if_match(
        self,
        partition: str,
        row_key: str,
        entity: dict[str, Any],
        etag: str,
    ) -> bool:
        assert partition.startswith("budget-protocol|") and row_key == "F"
        assert etag == f'W/"{self.version}"'
        assert set(entity) == {"Document", "Preserved"} and entity["Preserved"] == "unit"
        self.writes.append(deepcopy(entity))
        if self.before_replace is not None and not self.before_replace(entity):
            return False
        self.document = json.loads(entity["Document"])
        self.version += 1
        return True


@pytest.mark.parametrize("retired", [False, True])
def test_gateway_protocol_changes_only_membership_and_is_idempotent(retired: bool) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    if retired:
        fence["gateways"]["edge"] = ["1"]
        repository.gateway_deployments[
            gateway_deployment_id_for(
                inventory.gateway_profile_id,
                "edge",
            )
        ]["retired_at"] = NOW
    store = ProtocolFenceRows(fence)
    before_write = MagicMock()
    for _ in range(2):
        result = enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
            before_write=before_write,
        )
        assert result == (() if retired else ("edge",))
    assert len(store.writes) == 1
    before_write.assert_called_once()
    assert store.document["gateways"]["managed"] == ["1", "previous"]
    assert ("edge" in store.document["gateways"]) is not retired
    assert {key: value for key, value in store.document.items() if key != "gateways"} == {
        key: value for key, value in fence.items() if key != "gateways"
    }


@pytest.mark.parametrize("committed", [False, True])
def test_gateway_protocol_unknown_write_reads_back_without_replaying(committed: bool) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)

    def lost_response(entity: dict[str, Any]) -> bool:
        if committed:
            store.document = json.loads(entity["Document"])
            store.version += 1
        raise httpx.ReadTimeout("Unknown conditional write outcome")

    store.before_replace = lost_response
    if committed:
        assert enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        ) == ("edge",)
    else:
        with pytest.raises(RetryablePublicationError, match="outcome is uncertain"):
            enroll_self_hosted_gateway_protocol(
                repository,
                cast(TableStorageLedger, store),
                inventory,
                fence,
                client,
                canonical,
            )
    assert len(store.writes) == 1 and store.reads == 2


def test_gateway_protocol_conflict_preserves_concurrent_additions_and_rechecks_lease() -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)

    def conflict(_: dict[str, Any]) -> bool:
        store.document["gateways"]["concurrent"] = ["1"]
        store.version += 1
        store.before_replace = None
        return False

    store.before_replace = conflict
    before_write = MagicMock()
    assert enroll_self_hosted_gateway_protocol(
        repository,
        cast(TableStorageLedger, store),
        inventory,
        fence,
        client,
        canonical,
        before_write=before_write,
    ) == ("edge",)
    assert before_write.call_count == 2
    assert store.document["gateways"] == {
        "managed": ["1", "previous"],
        "concurrent": ["1"],
        "edge": ["1"],
    }


@pytest.mark.parametrize(
    "change", ["epoch", "contracts", "revision", "target", "deployment", "lease"]
)
def test_gateway_protocol_rechecks_protected_state_before_conditional_write(
    change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    callback = None
    if change == "epoch":
        store.document["epoch"] = str(UUID(int=73))
    elif change == "contracts":
        store.document["model_contracts"] = {}
    elif change == "revision":
        client.current_revision.return_value = "2"
    elif change == "target":
        original = repository.get_apim_target(inventory.gateway_profile_id)
        assert original is not None
        monkeypatch.setattr(repository, "get_apim_target", MagicMock(
            side_effect=[original, {**original, "api_path": "other"}]
        ))
    elif change == "deployment":
        monkeypatch.setattr(repository, "get_gateway_deployment", MagicMock(return_value=None))
    else:
        callback = MagicMock(side_effect=RetryablePublicationError("Lease expired"))
    with pytest.raises((PolicyCompilationError, RetryablePublicationError)):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
            before_write=callback,
        )
    assert store.writes == []


@pytest.mark.parametrize(
    "change,message",
    [
        ("stale", "latest inventory"),
        ("missing", "missing from Azure inventory"),
        ("foreign", "foreign Gateway"),
        ("association", "not associated"),
        ("unsupported", "does not support"),
        ("revision", "current API revision changed"),
    ],
)
def test_gateway_protocol_rejects_unproven_inventory(change: str, message: str) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    values = inventory.model_dump()
    if change == "stale":
        values["discovered_at"] = NOW - timedelta(seconds=1)
    elif change == "missing":
        values["gateways"] = []
    elif change == "foreign":
        values["gateways"][0]["arm_resource_id"] += "/foreign"
    elif change == "association":
        values["gateways"][0]["associated_api_ids"] = ["foreign-api"]
    elif change == "unsupported":
        values["capabilities"]["self_hosted_gateway"] = "unsupported"
    else:
        client.current_api_policy.return_value = ("2", client.current_api_policy.return_value[1])
    with pytest.raises((PolicyCompilationError, RetryablePublicationError), match=message):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            ApimTargetArmInventory.model_validate(values),
            fence,
            client,
            canonical,
        )
    assert store.writes == [] and store.reads == 0


@pytest.mark.parametrize(
    "change,message",
    [
        ("mode", "active target fence"),
        ("epoch", "valid epoch"),
        ("protocol", "active target fence"),
        ("identity", "active target fence"),
        ("revision", "changed concurrently"),
        ("case", "different casing"),
    ],
)
def test_gateway_protocol_refuses_fence_identity_and_existing_membership_drift(
    change: str,
    message: str,
) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    if change == "mode":
        fence["mode"] = "paused"
    elif change == "epoch":
        fence["epoch"] = "not-an-epoch"
    elif change == "protocol":
        fence["protocol_version"] = True
    elif change == "identity":
        fence["gateway_profile_id"] = str(UUID(int=99))
    store = ProtocolFenceRows(fence)
    if change == "revision":
        store.document["gateways"]["managed"] = ["different"]
    elif change == "case":
        store.document["gateways"]["EDGE"] = ["1"]
    with pytest.raises(PolicyCompilationError, match=message):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        )
    assert store.writes == []


def test_gateway_protocol_conditional_conflicts_have_a_fixed_retry_bound() -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    store.before_replace = lambda _: False
    with pytest.raises(RetryablePublicationError, match="fresh conditional retry"):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        )
    assert len(store.writes) == 3 and store.reads == 4
    assert store.document == fence


def test_gateway_protocol_refuses_parent_policy_drift_before_reading_fence() -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    policy = parse_policy(client.current_api_policy.return_value[1])
    version = policy.find("./inbound/set-variable[@name='budgetExecutionVersion']")
    assert version is not None
    version.set("value", "@(1)")
    client.current_api_policy.return_value = ("1", serialize_policy(policy))
    with pytest.raises(PolicyCompilationError):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        )
    assert store.reads == 0 and store.writes == []


def service_fixture() -> tuple[ApimOnboardingService, InMemoryRepository, MagicMock, MagicMock]:
    repository, client, projector = InMemoryRepository(), MagicMock(), MagicMock()
    client.inspect.return_value = onboarding_plan()
    client.ensure_governance_dependencies.return_value = UUID(int=47)
    client.api_resource_path = f"/apis/{onboarding_request().api_id}"
    context = MagicMock()
    context.__enter__.return_value = client
    service = ApimOnboardingService(
        repository,
        enabled=True,
        configuration=governance_configuration(),
        canonical_parent=(REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        cipher=TEST_CIPHER,
        budget_ready=lambda: True,
        client_factory=lambda _: context,
        projector=projector,
    )
    return service, repository, client, projector


def test_initialization_state_machine_keeps_profile_disabled_until_verified() -> None:
    service, repository, client, projector = service_fixture()
    plan = service.preview(onboarding_request())
    write = ApimOnboardingStart(request=plan.request, confirmation_sha256=plan.confirmation_sha256)
    accepted = service.start(write, "unit-owner")
    assert service.start(write, "unit-owner").operation.id == accepted.operation.id
    profile = next(row for row in repository.gateways if row["id"] == plan.gateway_profile_id)
    for expected in (
        "validating_dependencies",
        "promoting",
        "verifying_readback",
        "preflight_probing",
        "succeeded",
    ):
        assert not profile["enabled"]
        row = repository.claim_gateway_release_operation("worker", 180)
        assert row is not None
        service.advance(row, "worker")
        actual = repository.get_gateway_release_operation(accepted.operation.id)
        assert actual is not None and actual["status"] == expected
    assert profile["enabled"] and profile["config"]["onboarding_ready"]
    assert accepted.operation.id not in repository.gateway_release_operation_secrets
    client.verify_initialization_permissions.assert_called_once()
    client.verify_managed_gateway.assert_called_once()
    projector.assert_called_once()
    assert service.operations().items[0].status == "succeeded"
    assert initialized_apim_targets(repository) == {plan.gateway_profile_id: plan.request}
    repository.apim_targets[plan.gateway_profile_id]["api_id"] = "changed"
    with pytest.raises(PolicyCompilationError, match="no longer matches"):
        initialized_apim_targets(repository)


@pytest.mark.parametrize("failure", ["project", "probe"])
def test_initialization_failure_never_enables_target_or_discards_credentials(failure: str) -> None:
    service, repository, client, projector = service_fixture()
    failing_step = projector if failure == "project" else client.verify_managed_gateway
    failing_step.side_effect = RetryablePublicationError("unit dependency unavailable")
    plan = service.preview(onboarding_request())
    accepted = service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner",
    )
    with pytest.raises(RetryablePublicationError, match="unit dependency unavailable"):
        for _ in range(5):
            row = repository.claim_gateway_release_operation("worker", 180)
            assert row is not None
            service.advance(row, "worker")
    profile = next(row for row in repository.gateways if row["id"] == plan.gateway_profile_id)
    assert not profile["enabled"]
    assert accepted.operation.id in repository.gateway_release_operation_secrets
    assert initialized_apim_targets(repository) == {}


def test_new_target_ledger_projection_preserves_history_and_rejects_foreign_fence() -> None:
    service, repository, _, _ = service_fixture()
    plan = service.preview(onboarding_request())
    accepted = service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner",
    )
    service._materialize(plan, UUID(int=47), False)
    store, prepare_scope = AdmissionRows(), MagicMock()
    application_id = application_id_for(plan.gateway_profile_id, plan.dashboard_subscription_id)
    partition = application_partition_key(application_id, datetime.now(UTC).strftime("%Y-%m"))
    retained: dict[tuple[str, str], dict[str, Any]] = {
        (partition, "C"): {"ConfirmedUsed": 37},
        (partition, "R|pending|unit"): {"Reserved": 90},
        ("other-target", "F"): {"Document": "unrelated"},
    }
    store.rows.update(retained)
    epoch = UUID(int=86)
    with patch.object(
        repository,
        "enroll_apim_onboarding_budget_target",
        return_value={
            "epoch": epoch,
            "gateway_profile_ids": [plan.gateway_profile_id],
        },
    ) as enroll:
        for _ in range(2):
            project_onboarding_ledger(
                repository,
                cast(TableStorageLedger, store),
                plan,
                accepted.operation.id,
                "worker",
                prepare_scope,
            )
        enroll.assert_called_with(accepted.operation.id, "worker")
        assert all(store.rows[key] == value for key, value in retained.items())
        assert prepare_scope.call_args_list == [
            ((application_id, epoch),),
            ((application_id, epoch),),
        ]
        fences = [key for key in store.rows if key[0].startswith("budget-protocol|")]
        assert len(fences) == 1
        fence = json.loads(store.rows[fences[0]]["Document"])
        assert fence["epoch"] == str(epoch) and fence["gateways"] == {"managed": ["1"]}
        assert fence["model_contracts"] == {}
        changed = {**fence, "model_contracts": {"existing": {"version": 2}}}
        store.rows[fences[0]] = {"Document": json.dumps(changed)}
        with pytest.raises(PolicyCompilationError, match="another configuration"):
            project_onboarding_ledger(
                repository,
                cast(TableStorageLedger, store),
                plan,
                accepted.operation.id,
                "worker",
                prepare_scope,
            )
        assert json.loads(store.rows[fences[0]]["Document"]) == changed


@pytest.mark.parametrize("outcome", ["success", "dependency_error", "exhausted", "disabled"])
def test_worker_dispatches_initialization_without_resolving_an_unready_target(outcome: str) -> None:
    service, repository, client, _ = service_fixture()
    plan = service.preview(onboarding_request())
    accepted = service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner",
    )
    resolver = MagicMock()
    resolver.resolve.side_effect = AssertionError("Unready target must not use ordinary resolver")
    if outcome == "dependency_error":
        client.ensure_governance_dependencies.side_effect = PolicyCompilationError("unit failure")
    worker = GatewayReleaseOperationWorker(
        repository,
        resolver,
        onboarding_handler=None if outcome == "disabled" else service.advance,
    )
    result = None
    for _ in range(5):
        result = worker.run_once(
            "onboarding-worker", max_attempts=2 if outcome == "exhausted" else 30
        )
        assert result is not None
        if result.status.value in {"succeeded", "failed"}:
            break
    assert result is not None
    assert result.status.value == ("succeeded" if outcome == "success" else "failed")
    resolver.resolve.assert_not_called()
    if outcome == "success":
        assert accepted.operation.id not in repository.gateway_release_operation_secrets
    else:
        assert accepted.operation.id in repository.gateway_release_operation_secrets
    if outcome == "exhausted":
        assert result.error_code == "max_attempts_exceeded"


def test_onboarding_factory_is_lazy_and_requires_the_configured_ledger() -> None:
    request, config = onboarding_request(), governance_configuration()
    parts = config.ledger_table_resource_id.split("/")
    settings = Settings.model_construct(
        apim_target_registry_enabled=True,
        gateway_release_worker_enabled=True,
        budget_execution_enabled=True,
        credential_encryption_key=SecretStr("unit-key"),
        azure_subscription_id=str(request.subscription_id),
        apim_resource_group=request.resource_group,
        apim_service_name=request.service_name,
        ledger_table_endpoint=f"https://{parts[8]}.table.core.windows.net",
        ledger_table_name=parts[-1],
    )
    assert (
        onboarding_template_path(settings)
        == REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml"
    )
    assert apim_onboarding_available(settings)
    for field, value in (
        ("apim_target_registry_enabled", False),
        ("gateway_release_worker_enabled", False),
        ("budget_execution_enabled", False),
        ("credential_encryption_key", None),
        ("ledger_table_endpoint", None),
        ("data_backend", "demo"),
        ("azure_subscription_id", None),
    ):
        assert not apim_onboarding_available(settings.model_copy(update={field: value}))
    with patch(
        "turnstile_core.services.apim_onboarding.AzureApimPlatformConfigurationClient"
    ) as platform:
        platform.return_value.configuration.return_value = config
        with patch(
            "turnstile_core.services.apim_onboarding.CredentialCipher.from_settings",
            return_value=TEST_CIPHER,
        ):
            service = build_apim_onboarding_service(InMemoryRepository(), settings)
            platform.assert_not_called()
            assert len(service._configuration_digest()) == 64
            service._configuration_digest()
            platform.assert_called_once()
            changed = build_apim_onboarding_service(
                InMemoryRepository(),
                settings.model_copy(
                    update={
                        "ledger_table_name": "DifferentLedger",
                    }
                ),
            )
            with pytest.raises(PolicyCompilationError, match="does not match its budget ledger"):
                changed._configuration_digest()


def test_initialization_rechecks_confirmation_and_preserves_failed_plan_for_linked_retry() -> None:
    service, repository, client, _ = service_fixture()
    plan = service.preview(onboarding_request())
    with pytest.raises(ControlPlaneConflictError, match="review the plan"):
        service.start(
            ApimOnboardingStart(request=plan.request, confirmation_sha256="b" * 64), "owner"
        )
    assert repository.gateway_release_operations == []
    accepted = service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner",
    )
    claimed = repository.claim_gateway_release_operation("worker", 180)
    assert claimed is not None
    repository.transition_gateway_release_operation(
        accepted.operation.id,
        "queued",
        "failed",
        {"error_code": "permission_missing"},
        "worker",
    )
    retried = service.retry(accepted.operation.id, "owner")
    assert retried.operation.id != accepted.operation.id
    assert retried.operation.checkpoint["retry_of"] == str(accepted.operation.id)
    assert (
        repository.gateway_release_operation_secrets[retried.operation.id]
        == repository.gateway_release_operation_secrets[accepted.operation.id]
    )
    client.ensure_api.assert_not_called()


@pytest.mark.parametrize("bearer,ready", [(None, True), ("Authorization", False)])
def test_onboarding_preview_is_read_only_and_accepts_only_null_bearer(
    bearer: str | None,
    ready: bool,
) -> None:
    request = onboarding_request()
    calls = []
    client: AzureApimOnboardingClient

    def handler(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        assert call.method == "GET"
        if call.url.path == request.resource_id:
            return httpx.Response(
                200,
                json={
                    "id": request.resource_id,
                    "location": "eastus",
                    "sku": {"name": "Developer"},
                    "properties": {
                        "provisioningState": "Succeeded",
                        "gatewayUrl": "https://unit-service.azure-api.net",
                    },
                },
            )
        if call.url.path.endswith("/apis"):
            properties = client._api_properties()
            properties["subscriptionKeyParameterNames"] = {
                "header": "Ocp-Apim-Subscription-Key",
                "query": "subscription-key",
                "bearer": bearer,
            }
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": request.resource_id + client.api_resource_path,
                            "properties": properties,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    plan = client.inspect(management_tenant_id=request.tenant_id, configuration_digest="c" * 64)
    assert plan.ready == ready
    assert plan.blockers == ([] if ready else ["dedicated_api_conflict"])
    assert plan.confirmation_sha256 == onboarding_plan_digest(plan, "c" * 64)
    assert len(calls) == 4
    with pytest.raises(PolicyCompilationError, match="another tenant"):
        client.inspect(management_tenant_id=UUID(int=9), configuration_digest="c" * 64)
    assert len(calls) == 4


@pytest.mark.parametrize("secret,tagged", [(False, True), (True, True), (False, False)])
def test_shared_governance_configuration_must_be_nonsecret_and_owned(
    secret: bool,
    tagged: bool,
) -> None:
    request = onboarding_request()

    def handler(call: httpx.Request) -> httpx.Response:
        assert call.method == "GET"
        return httpx.Response(
            200,
            json={
                "id": request.resource_id + "/namedValues/turnstile-platform-governance",
                "properties": {
                    "displayName": "turnstile-platform-governance",
                    "secret": secret,
                    "tags": ["turnstile-platform-configuration"] if tagged else [],
                    "value": governance_configuration().model_dump_json(),
                },
            },
        )

    client = AzureApimPlatformConfigurationClient(
        onboarding_configuration(request),
        cast(Any, StubTokenProvider()),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if not secret and tagged:
        assert client.configuration() == governance_configuration()
    else:
        with pytest.raises(PolicyCompilationError, match="identity is invalid"):
            client.configuration()


@pytest.mark.parametrize("secret", [False, True])
@pytest.mark.parametrize("resolved", ["systemAssigned", "otherIdentity"])
def test_logger_identity_reference_is_read_back_without_accepting_drift(
    secret: bool,
    resolved: str,
) -> None:
    request = onboarding_request()
    calls = []

    def handler(call: httpx.Request) -> httpx.Response:
        calls.append((call.method, call.url.path))
        path = request.resource_id + "/namedValues/logger-identity"
        if call.method == "POST":
            assert call.url.path == path + "/listValue"
            return httpx.Response(200, json={"value": resolved})
        assert call.url.path == path
        return httpx.Response(
            200,
            json={
                "id": path,
                "properties": {"secret": secret, "value": None if secret else resolved},
            },
        )

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    expected = client._logger_properties(governance_configuration())
    credentials = cast(dict[str, object], expected["credentials"])
    logger_path = f"/loggers/{request.api_id}-usage"
    value = {
        "id": request.resource_id + logger_path,
        "properties": {
            **expected,
            "credentials": {**credentials, "identityClientId": "{{logger-identity}}"},
        },
    }
    if resolved == "systemAssigned":
        client._require_owned_logger(value, logger_path, expected)
    else:
        with pytest.raises(PolicyCompilationError, match="identity has drifted"):
            client._require_owned_logger(value, logger_path, expected)
    assert len(calls) == (2 if secret else 1)


@pytest.mark.parametrize(
    "inventory,allowed",
    [
        ({"value": [{"actions": ["Microsoft.ApiManagement/*"], "notActions": []}]}, True),
        ({"value": [{"actions": ["*"], "notActions": ["*/write"]}]}, False),
        ({"value": [{"actions": ["*"], "notActions": []}], "nextLink": "more"}, False),
        ({"value": [{"actions": [1]}]}, False),
        ({"value": [{"actions": ["*"], "notActions": [], "condition": "true"}]}, False),
        ({"value": [{"actions": ["*"], "notActions": []}, None]}, False),
    ],
)
def test_initialization_permissions_require_complete_scoped_grants(
    inventory: dict[str, Any],
    allowed: bool,
) -> None:
    request = onboarding_request()

    def handler(call: httpx.Request) -> httpx.Response:
        assert call.method == "GET"
        assert (
            call.url.path == request.resource_id + "/providers/Microsoft.Authorization/permissions"
        )
        return httpx.Response(200, json=inventory)

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    required = {"Microsoft.ApiManagement/service/apis/write"}
    if allowed:
        client._require_permissions(request.resource_id, required)
    else:
        with pytest.raises(PolicyCompilationError):
            client._require_permissions(request.resource_id, required)


def test_initialization_checks_publication_permissions_before_configuration() -> None:
    request = onboarding_request()
    calls: list[httpx.Request] = []

    def handler(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        assert call.method == "GET"
        if call.url.path == request.resource_id:
            return httpx.Response(200, json={
                "id": request.resource_id, "identity": {"type": "SystemAssigned"},
            })
        assert call.url.path.endswith("/providers/Microsoft.Authorization/permissions")
        return httpx.Response(200, json={"value": [{
            "actions": ["*"],
            "notActions": ["Microsoft.ApiManagement/service/namedValues/write"],
        }]})

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PolicyCompilationError, match="namedValues/write"):
        client.verify_initialization_permissions(governance_configuration())
    assert len(calls) == 2


def test_initialization_subscriptions_stage_then_activate_with_exact_etag() -> None:
    request = onboarding_request()
    keys = {
        purpose: {"primary": purpose * 8, "secondary": "second-" + purpose * 8}
        for purpose in ("dashboard", "probe")
    }
    stored: dict[str, dict[str, Any]] = {}
    writes = []
    client: AzureApimOnboardingClient

    def handler(call: httpx.Request) -> httpx.Response:
        path = call.url.path.removeprefix(request.resource_id)
        if path == client.api_resource_path:
            return httpx.Response(
                200, json={"id": request.resource_id + path, "properties": client._api_properties()}
            )
        if call.method == "PUT":
            properties = json.loads(call.content)["properties"]
            if properties["state"] == "suspended":
                assert call.headers["If-None-Match"] == "*" and path not in stored
            else:
                assert call.headers["If-Match"] == '"unit-etag"'
            stored[path] = properties
            writes.append(properties["state"])
        if call.method == "POST":
            assert path.endswith("/listSecrets")
            pair = stored[path.removesuffix("/listSecrets")]
            return httpx.Response(
                200, json={"primaryKey": pair["primaryKey"], "secondaryKey": pair["secondaryKey"]}
            )
        return (
            httpx.Response(
                200,
                json={"properties": stored[path]},
                headers={
                    "ETag": '"unit-etag"',
                },
            )
            if path in stored
            else httpx.Response(404)
        )

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ensure_subscriptions(keys)
    client.ensure_subscriptions(keys)
    assert writes == ["suspended", "suspended"]
    client.ensure_subscriptions(keys, activate=True)
    client.ensure_subscriptions(keys, activate=True)
    assert writes == ["suspended", "suspended", "active", "active"]
    stored[f"/subscriptions/{request.dashboard_subscription_id}"]["primaryKey"] = "changed"
    with pytest.raises(PolicyCompilationError, match="credentials changed"):
        client.ensure_subscriptions(keys, activate=True)
    assert len(writes) == 4


def test_governance_roles_are_resource_scoped_and_existing_logger_is_read_only() -> None:
    request, config = onboarding_request(), governance_configuration()
    resources: dict[str, dict[str, Any]] = {}
    writes = []
    principal_id = UUID(int=47)

    def handler(call: httpx.Request) -> httpx.Response:
        path = call.url.path
        if path == request.resource_id:
            assert call.method == "GET"
            return httpx.Response(
                200,
                json={
                    "id": path,
                    "identity": {
                        "type": "SystemAssigned",
                        "principalId": str(principal_id),
                    },
                },
            )
        if call.method == "PUT":
            assert call.headers["If-None-Match"] == "*"
            resource = json.loads(call.content)
            if "/roleAssignments/" in path:
                scope = path.split("/providers/Microsoft.Authorization/roleAssignments/")[0]
                assert scope in {config.event_hub_resource_id, config.ledger_table_resource_id}
                resource["properties"]["scope"] = scope
            resources[path] = {"id": path, **resource}
            writes.append(path)
        return (
            httpx.Response(200, json=resources[path]) if path in resources else httpx.Response(404)
        )

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.ensure_governance_dependencies(config) == principal_id
    assert client.ensure_governance_dependencies(config) == principal_id
    assert len(writes) == 3
    assert sum("/roleAssignments/" in path for path in writes) == 2


def test_identity_initialization_preserves_user_assigned_identities_with_exact_etag() -> None:
    request = onboarding_request()
    user_identity = "/subscriptions/unit/resourceGroups/unit/providers/ManagedIdentity/unit"
    writes = []

    def handler(call: httpx.Request) -> httpx.Response:
        assert call.url.path == request.resource_id
        if call.method == "PATCH":
            assert call.headers["If-Match"] == '"identity-etag"'
            writes.append(json.loads(call.content))
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "id": request.resource_id,
                "identity": {
                    "type": "UserAssigned",
                    "userAssignedIdentities": {user_identity: {"clientId": "unit"}},
                },
            },
            headers={"ETag": '"identity-etag"'},
        )

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryablePublicationError, match="being initialized"):
        client.ensure_governance_dependencies(governance_configuration())
    assert writes == [
        {
            "identity": {
                "type": "SystemAssigned, UserAssigned",
                "userAssignedIdentities": {user_identity: {}},
            }
        }
    ]


def test_initialization_creates_only_owned_fixed_routes_and_is_repeatable() -> None:
    request = onboarding_request()
    resources: dict[str, Any] = {}
    writes = []

    def handler(call: httpx.Request) -> httpx.Response:
        path = call.url.path.removeprefix(request.resource_id)
        if call.method == "PUT":
            assert call.headers["If-None-Match"] == "*"
            assert path.startswith(f"/apis/{request.api_id}")
            resources[path] = {"id": request.resource_id + path, **json.loads(call.content)}
            writes.append(path)
            return httpx.Response(201, json=resources[path])
        assert call.method == "GET"
        resource = resources.get(path)
        return httpx.Response(200, json=resource) if resource else httpx.Response(404)

    client = AzureApimOnboardingClient(
        request,
        token_provider=cast(Any, StubTokenProvider()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ensure_api()
    client.ensure_catalog()
    assert len(writes) == 15
    client.ensure_api()
    client.ensure_catalog()
    assert len(writes) == 15
    images = resources[f"/apis/{request.api_id}/operations/images-generations"]
    assert images["properties"]["urlTemplate"] == "/images/generations"
    with pytest.raises(PolicyCompilationError, match="outside its own API"):
        client.ensure_policy("/apis/other", "<policies />")


@pytest.mark.parametrize("anonymous_allowed", [False, True])
def test_managed_gateway_verification_requires_identity_and_auth_denial(
    anonymous_allowed: bool,
) -> None:
    request = onboarding_request()
    calls = []
    keys = {
        purpose: {"primary": purpose * 8, "secondary": "second-" + purpose * 8}
        for purpose in ("dashboard", "probe")
    }

    def handler(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        assert call.method == "GET" and call.url.host == "unit-service.azure-api.net"
        token = call.headers.get("Ocp-Apim-Subscription-Key")
        if token not in {pair["primary"] for pair in keys.values()} and not anonymous_allowed:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={"object": "list", "data": []},
            headers={
                "x-turnstile-onboarding": str(request.gateway_profile_id),
                "x-turnstile-native-gateway": "managed",
            },
        )

    client = AzureApimOnboardingClient(
        request,
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
        ),
    )
    if anonymous_allowed:
        with pytest.raises(PolicyCompilationError, match="unauthenticated"):
            client.verify_managed_gateway(onboarding_plan(), keys)
    else:
        client.verify_managed_gateway(onboarding_plan(), keys)
        assert len(calls) == 4


def test_onboarding_policy_preserves_public_auth_budget_and_target_binding() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    request = onboarding_request()
    configuration = governance_configuration(organization_name='Unit "quoted" & team')
    policy = render_onboarding_parent(request, configuration, source)
    validate_parent_policy(
        policy,
        onboarding_canonical_parent(source, request.api_id, configuration),
    )
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", policy)
    root = parse_policy(policy)
    validator = root.find(".//validate-azure-ad-token")
    assert validator is not None
    assert validator.attrib["tenant-id"] == str(configuration.employee_tenant_id)
    assert root.find(".//set-variable[@name='budgetExecutionVersion']") is not None
    assert len(root.findall(".//set-variable[@name='executionPlan']")) == 2
    assert request.api_id in policy and str(request.gateway_profile_id) in policy
    assert "https://unitstorage.table.core.windows.net/UnitLedger" in policy
    digest = onboarding_policy_configuration_digest(configuration, source)
    changed = configuration.model_copy(update={"employee_audience": "api://another"})
    assert digest != onboarding_policy_configuration_digest(changed, source)


def onboarding_request(**changes: Any) -> ApimOnboardingRequest:
    return ApimOnboardingRequest.model_validate(
        {
            "tenant_id": UUID(int=3),
            "resource_id": target().arm_resource_id,
            "name": "Unit APIM",
            **changes,
        }
    )


def onboarding_plan(**changes: Any) -> ApimOnboardingPlan:
    request = onboarding_request()
    return ApimOnboardingPlan.model_validate(
        {
            "request": request,
            **{
                name: getattr(request, name)
                for name in (
                    "gateway_profile_id",
                    "service_name",
                    "api_id",
                    "api_path",
                    "dashboard_subscription_id",
                    "probe_subscription_id",
                )
            },
            "service_tier": "Developer",
            "location": "eastus",
            "managed_gateway_origin": "https://unit-service.azure-api.net",
            "ready": True,
            "confirmation_sha256": "a" * 64,
            **changes,
        }
    )


def governance_configuration(**changes: Any) -> ApimOnboardingConfiguration:
    prefix = f"/subscriptions/{UUID(int=2)}/resourceGroups/unit-platform/providers"
    return ApimOnboardingConfiguration.model_validate(
        {
            "management_tenant_id": UUID(int=3),
            "employee_tenant_id": UUID(int=3),
            "employee_client_id": UUID(int=4),
            "employee_audience": "api://unit-client",
            "organization_id": "unit-org",
            "organization_name": "Unit organization",
            "event_hub_resource_id": prefix
            + "/Microsoft.EventHub/namespaces/unit-ns/eventhubs/usage",
            "ledger_table_resource_id": prefix
            + "/Microsoft.Storage/storageAccounts/unitstorage"
            + "/tableServices/default/tables/UnitLedger",
            **changes,
        }
    )


@pytest.mark.parametrize("state", ["verified", "expired", "wrong-worker", "unverified"])
def test_onboarding_activation_requires_verified_target_and_current_lease(state: str) -> None:
    repository = InMemoryRepository()
    plan = onboarding_plan()
    operation = repository.create_apim_onboarding_operation(plan, b"unit-encrypted", "owner")
    row = repository.gateway_release_operations[-1]
    now = datetime.now(UTC)
    row.update(
        status="preflight_probing",
        lease_owner="worker",
        lease_expires_at=now + timedelta(seconds=-1 if state == "expired" else 180),
    )
    repository.upsert_apim_target(target(gateway_profile_id=plan.gateway_profile_id))
    if state != "unverified":
        repository.upsert_gateway_deployment(
            deployment(
                apim_target_id=plan.gateway_profile_id,
                endpoint_url="https://unit.example.test",
                reachability_state="reachable",
                last_probe_at=now,
            )
        )
    worker = "other" if state == "wrong-worker" else "worker"
    if state == "unverified":
        with pytest.raises(ValueError, match="unverified or changed"):
            repository.finish_apim_onboarding_operation(
                operation["id"],
                worker,
                b"saved",
                "***",
                "b" * 64,
            )
    else:
        result = repository.finish_apim_onboarding_operation(
            operation["id"],
            worker,
            b"saved",
            "***",
            "b" * 64,
        )
        assert (result is not None) == (state == "verified")
    profile = next(item for item in repository.gateways if item["id"] == plan.gateway_profile_id)
    assert profile["enabled"] == (state == "verified")
    secret_removed = operation["id"] not in repository.gateway_release_operation_secrets
    assert secret_removed == (state == "verified")


def test_postgres_onboarding_activation_checks_lease_before_writing() -> None:
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    context, connection = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    with patch.object(repository, "_connection", return_value=context):
        assert (
            repository.finish_apim_onboarding_operation(
                UUID(int=8), "worker", b"key", "***", "b" * 64
            )
            is None
        )
    assert connection.execute.call_count == 1
    assert "lease_expires_at > now() FOR UPDATE" in connection.execute.call_args.args[0]


def test_onboarding_creation_is_idempotent_and_retry_keeps_original_failure() -> None:
    repository = InMemoryRepository()
    plan = onboarding_plan()
    first = repository.create_apim_onboarding_operation(plan, b"unit-encrypted", "unit-owner")
    assert repository.create_apim_onboarding_operation(plan, b"never-replaced", "other") == first
    profile = next(row for row in repository.gateways if row["id"] == plan.gateway_profile_id)
    assert not profile["enabled"] and profile["credential_ciphertext"] is None
    with pytest.raises(ValueError, match="plan has changed"):
        repository.create_apim_onboarding_operation(
            plan.model_copy(update={"confirmation_sha256": "b" * 64}),
            b"unit",
            "owner",
        )
    with pytest.raises(ValueError, match="latest failed"):
        repository.retry_apim_onboarding_operation(first["id"], "owner")
    repository.gateway_release_operations[-1]["status"] = "failed"
    retried = repository.retry_apim_onboarding_operation(first["id"], "retry-owner")
    assert retried["id"] != first["id"] and retried["checkpoint"]["retry_of"] == str(first["id"])
    assert retried["semantic_preview"] == first["semantic_preview"]
    assert repository.gateway_release_operations[0]["status"] == "failed"
    assert repository.gateway_release_operation_secrets[retried["id"]] == b"unit-encrypted"
    assert len(repository.list_apim_onboarding_operations()) == 1
    with pytest.raises(ValueError, match="latest failed"):
        repository.retry_apim_onboarding_operation(first["id"], "owner")


def test_postgres_reuses_same_plan_under_resource_lock_without_writes() -> None:
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    context, connection = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    expected = {"id": UUID(int=8), "confirmation_sha256": "a" * 64}
    connection.execute.return_value.fetchone.return_value = expected
    with patch.object(repository, "_connection", return_value=context):
        assert (
            repository.create_apim_onboarding_operation(
                onboarding_plan(),
                b"unit",
                "owner",
            )
            == expected
        )
    connection.transaction.assert_called_once()
    assert connection.execute.call_count == 2
    assert "pg_advisory_xact_lock" in connection.execute.call_args_list[0].args[0]
    assert all("INSERT" not in call.args[0] for call in connection.execute.call_args_list)


def test_onboarding_identity_is_stable_and_display_name_does_not_change_resources() -> None:
    original = onboarding_request()
    changed = onboarding_request(
        resource_id=" " + original.resource_id.upper() + "/ ", name="  Renamed  "
    )
    assert changed.gateway_profile_id == original.gateway_profile_id
    assert changed.api_id == original.api_id and changed.name == "Renamed"
    assert original.api_id.startswith("turnstile-")
    other_tenant = onboarding_request(tenant_id=UUID(int=5))
    assert original.gateway_profile_id != other_tenant.gateway_profile_id
    assert onboarding_plan().steps[-1] == "activate_target"


@pytest.mark.parametrize("suffix", ["/apis/other", "?key=value", "#fragment", "%2F", "\\other"])
def test_onboarding_request_rejects_children_and_encoded_resource_ids(suffix: str) -> None:
    with pytest.raises(ValueError):
        onboarding_request(resource_id=target().arm_resource_id + suffix)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "   "},
        {"resource_id": "https://management.azure.com" + target().arm_resource_id},
        {"api_id": "caller-owned"},
        {"tenant_id": "invalid"},
    ],
)
def test_onboarding_request_rejects_invalid_or_extra_fields(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        onboarding_request(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"api_id": "another"},
        {"gateway_profile_id": UUID(int=6)},
        {"probe_subscription_id": "unrelated"},
        {"blockers": ["permission_missing"]},
        {"managed_gateway_origin": "https://other.azure-api.net"},
        {"managed_gateway_origin": "https://unit-service.azure-api.net/path"},
        {"managed_gateway_origin": "https://unit-service.azure-api.net:444"},
        {"confirmation_sha256": "not-a-hash"},
    ],
)
def test_onboarding_plan_rejects_tampered_resources(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        onboarding_plan(**change)


def test_governance_configuration_has_no_secrets_and_rejects_url_resource_refs() -> None:
    config = governance_configuration()
    assert "credential" not in config.model_dump() and "key" not in config.model_dump()
    with pytest.raises(ValueError):
        governance_configuration(
            event_hub_resource_id="https://management.azure.com" + config.event_hub_resource_id
        )
    with pytest.raises(ValueError):
        governance_configuration(surface_map=[{"name": "unit", "tokens": [" "]}])
