from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from functions.control_plane import function_app
from tests.backend.model_platform.test_apim_onboarding import (
    governance_configuration,
    onboarding_request,
)
from tests.backend.model_platform.test_apim_target_inventory import arm_inventory
from tests.backend.model_platform.test_gateway_targets import TARGET
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.integrations.apim_onboarding_policy import render_onboarding_parent
from turnstile_core.integrations.apim_policy_components import validate_parent_policy
from turnstile_core.persistence import factory
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.budget_settlement import BudgetScopeRepository


@pytest.mark.parametrize("change", [
    {}, {"mode": "paused"}, {"protocol_version": 1}, {"api_id": "another-api"},
    {"gateway_profile_id": str(UUID(int=2))}, {"apim_service_id": "another-service"},
])
def test_budget_fence_is_read_only_from_exact_configured_target(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
) -> None:
    gateway_id = UUID(int=1)
    settings = Settings(
        budget_execution_enabled=True, budget_execution_target_ids=[gateway_id],
        ledger_table_endpoint="https://ledger.example.test", apim_service_name="Unit-Service",
        apim_api_id="unit-api",
    )
    fence = {"protocol_version": 2, "mode": "active", "gateway_profile_id": str(gateway_id),
             "apim_service_id": "unit-service", "api_id": "unit-api", **change}
    ledger = MagicMock()
    ledger.read_entity.return_value = {"Document": json.dumps(fence)}
    factory_mock = MagicMock(return_value=ledger)
    monkeypatch.setattr(function_app, "AtomicTableLedger", factory_mock)
    if change:
        with pytest.raises(ValueError, match="configured target"):
            function_app._budget_configuration(gateway_id, settings)
    else:
        assert function_app._budget_configuration(gateway_id, settings) == fence
    partition = "budget-protocol|" + hashlib.sha256(b"unit-service").hexdigest() + "|unit-api"
    ledger.read_entity.assert_called_once_with(partition, "F")
    ledger.__exit__.assert_called_once()
    ledger.upsert.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "unselected", "selected", "initialized", "missing_policy"])
def test_protocol_enrollment_only_uses_enabled_target_configuration(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    settings = Settings.model_construct(
        budget_execution_enabled=mode != "off", apim_target_registry_enabled=True,
        budget_execution_target_ids=[TARGET] if mode in {"selected", "missing_policy"} else [],
        ledger_table_endpoint="https://unitledger.table.core.windows.net",
    )
    repository, client, targets = MagicMock(), MagicMock(), MagicMock()
    before_write, ledger = MagicMock(), MagicMock()
    ledger.__enter__.return_value = ledger
    fence = {"epoch": "unit-epoch"}
    reader = MagicMock(return_value=fence)
    enroll = MagicMock(return_value=("edge",))
    ledger_factory = MagicMock(return_value=ledger)
    monkeypatch.setattr(
        function_app, "initialized_apim_targets",
        lambda _: {TARGET: onboarding_request()} if mode == "initialized" else {},
    )
    monkeypatch.setattr(function_app, "_target_budget_configuration", reader)
    monkeypatch.setattr(function_app, "AtomicTableLedger", ledger_factory)
    monkeypatch.setattr(function_app, "enroll_self_hosted_gateway_protocol", enroll)
    inventory = arm_inventory()
    if mode == "missing_policy":
        with pytest.raises(ValueError, match="canonical policy"):
            function_app._enroll_gateway_protocol(
                inventory, client, repository, settings, targets, None, before_write,
            )
        enroll.assert_not_called()
        return
    result = function_app._enroll_gateway_protocol(
        inventory, client, repository, settings, targets, "canonical-policy", before_write,
    )
    if mode in {"off", "unselected"}:
        assert result == ()
        reader.assert_not_called()
        ledger_factory.assert_not_called()
    else:
        assert result == ("edge",)
        reader.assert_called_once_with(TARGET, settings, targets, repository)
        enroll.assert_called_once_with(
            repository, ledger, inventory, fence, client, "canonical-policy",
            before_write=before_write,
        )
        ledger.__exit__.assert_called_once()


def test_disabled_publication_timer_does_not_build_dependencies(monkeypatch: Any) -> None:
    settings = SimpleNamespace(
        control_plane_enabled=True,
        gateway_publication_worker_enabled=False,
    )
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)

    def reject_repository(_: object) -> None:
        raise AssertionError("disabled publication timer created a repository")

    monkeypatch.setattr(factory, "create_repository", reject_repository)

    function_app.publish_gateway_changes(None)


def test_target_mode_does_not_construct_a_global_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_construct(apim_target_registry_enabled=True)
    repository, configs, resolver = MagicMock(), MagicMock(), MagicMock()
    config_factory = MagicMock(return_value=configs)
    global_factory = MagicMock(side_effect=AssertionError("global client is forbidden"))
    monkeypatch.setattr(function_app, "apim_target_client_config_resolver", config_factory)
    monkeypatch.setattr(function_app, "AzureApimPublisherClientResolver", lambda actual: resolver)
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", global_factory)
    assert function_app._publisher_clients(repository, settings) == (resolver, configs)
    config_factory.assert_called_once_with(repository, settings)
    global_factory.assert_not_called()


def test_budget_fence_uses_resolved_target_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_id = UUID(int=17)
    global_settings = Settings.model_construct(apim_service_name="default-service")
    scoped = Settings.model_construct(apim_service_name="target-service")
    configs = MagicMock()
    configs.resolve.return_value.settings = scoped
    read = MagicMock(return_value={"gateway_profile_id": str(gateway_id)})
    monkeypatch.setattr(function_app, "_budget_configuration", read)
    assert function_app._target_budget_configuration(gateway_id, global_settings, configs) == {
        "gateway_profile_id": str(gateway_id),
    }
    configs.resolve.assert_called_once_with(gateway_id)
    read.assert_called_once_with(gateway_id, scoped, None)


def test_initialized_parent_matches_the_public_rendered_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, config = onboarding_request(), governance_configuration()
    parts = config.ledger_table_resource_id.split("/")
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    settings = Settings.model_construct(
        apim_target_registry_enabled=True, budget_execution_enabled=True,
        ledger_table_endpoint=f"https://{parts[8]}.table.core.windows.net/",
        ledger_table_name=parts[-1],
    )
    repository = InMemoryRepository()
    monkeypatch.setattr(
        function_app, "initialized_apim_targets", lambda _: {request.gateway_profile_id: request},
    )
    parents = function_app._budget_parent_overrides(source, settings, repository)
    validate_parent_policy(
        render_onboarding_parent(request, config, source), parents[request.gateway_profile_id],
    )
    with pytest.raises(ValueError, match="manual parent override"):
        function_app._budget_parent_overrides(source, settings.model_copy(update={
            "budget_execution_target_ids": [request.gateway_profile_id],
        }), repository)


@pytest.mark.parametrize("enrollment", ["valid", "missing", "other_epoch", "other_target"])
def test_initialized_budget_reader_requires_exact_active_epoch(
    monkeypatch: pytest.MonkeyPatch, enrollment: str,
) -> None:
    request, epoch = onboarding_request(), UUID(int=24)
    settings = Settings.model_construct(
        apim_target_registry_enabled=True, budget_execution_enabled=True,
        ledger_table_endpoint="https://unitledger.table.core.windows.net",
        apim_service_name=request.service_name, apim_api_id=request.api_id,
    )
    repository = MagicMock(spec=BudgetScopeRepository)
    monkeypatch.setattr(
        function_app, "initialized_apim_targets", lambda _: {request.gateway_profile_id: request},
    )
    rollout = None if enrollment == "missing" else {
        "epoch": epoch if enrollment != "other_epoch" else UUID(int=25),
        "gateway_profile_ids": [request.gateway_profile_id] if enrollment != "other_target" else [],
    }
    repository.active_budget_execution_rollout.return_value = rollout
    fence = {
        "protocol_version": 2, "mode": "active", "epoch": str(epoch),
        "gateway_profile_id": str(request.gateway_profile_id), "api_id": request.api_id,
        "apim_service_id": request.service_name,
    }
    ledger = MagicMock()
    ledger.read_entity.return_value = {"Document": json.dumps(fence)}
    monkeypatch.setattr(function_app, "AtomicTableLedger", lambda *_, **__: ledger)
    if enrollment == "valid":
        actual = function_app._budget_configuration(
            request.gateway_profile_id, settings, repository,
        )
        assert actual == fence
    else:
        with pytest.raises(ValueError, match="not enrolled"):
            function_app._budget_configuration(request.gateway_profile_id, settings, repository)
    ledger.upsert.assert_not_called()


def test_release_timer_wires_lazy_initialization_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_release_worker_enabled=True,
        apim_target_registry_enabled=True,
    )
    repository, service, worker = MagicMock(), MagicMock(), MagicMock()
    worker.run_once.return_value = None
    worker_factory = MagicMock(return_value=worker)
    builder = MagicMock(return_value=service)
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(factory, "create_repository", lambda _: repository)
    monkeypatch.setattr(function_app, "_publisher_clients", lambda *_: (MagicMock(), None))
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: None)
    monkeypatch.setattr(function_app, "build_apim_onboarding_service", builder)
    monkeypatch.setattr(function_app, "GatewayReleaseOperationWorker", worker_factory)
    function_app.process_gateway_release_operations(None)
    builder.assert_called_once_with(repository, settings)
    assert worker_factory.call_args.kwargs["onboarding_handler"] == service.advance
    repository.close.assert_called_once()


@pytest.mark.parametrize("provisioning_enabled", (False, True))
@pytest.mark.parametrize("fails", (False, True))
def test_release_timer_never_constructs_publication_worker(
    monkeypatch: Any, provisioning_enabled: bool, fails: bool,
) -> None:
    settings = SimpleNamespace(
        control_plane_enabled=True,
        gateway_release_worker_enabled=True,
        gateway_application_provisioning_enabled=provisioning_enabled,
        ledger_table_endpoint="https://ledger.example.test",
        ledger_table_name="TurnstileLedger",
        gateway_release_retention_count=20,
        gateway_release_retention_days=180,
        gateway_failed_release_retention_days=30,
        gateway_release_protected_labels=["milestone", "rollback"],
        gateway_application_default_monthly_token_limit=100_000,
        gateway_application_default_tokens_per_minute=100_000,
        control_plane_lease_seconds=180,
        control_plane_max_attempts=30,
    )
    repository = MagicMock()
    publisher = object()
    cipher = object()
    calls: list[tuple[str, int, int]] = []
    ledger_calls: list[tuple[object, ...]] = []

    class Ledger:
        def __init__(self, endpoint: str, table: str) -> None:
            ledger_calls.append((endpoint, table))

        def __enter__(self) -> Ledger:
            return self

        def __exit__(self, *_: object) -> None:
            ledger_calls.append(("closed",))

    def prepare(*args: object) -> None:
        assert args[0] is repository
        assert isinstance(args[1], Ledger)
        ledger_calls.append(args[2:])

    class ReleaseWorker:
        def __init__(
            self,
            actual_repository: object,
            actual_publisher: object,
            _: object,
            **kwargs: object,
        ):
            assert actual_repository is repository
            assert actual_publisher is publisher
            projector = kwargs.pop("application_projector")
            if provisioning_enabled:
                assert callable(projector)
                projector(UUID(int=1), UUID(int=2))
            else:
                assert projector is None
            assert kwargs == {
                "application_default_token_limit": 100_000,
                "application_default_tokens_per_minute": 100_000,
                "cipher": cipher,
                "parent_policy": None,
            }

        def run_once(
            self, worker_id: str, lease_seconds: int, max_attempts: int
        ) -> None:
            calls.append((worker_id, lease_seconds, max_attempts))
            if fails:
                raise RuntimeError("worker failure")

    class PublicationWorker:
        def __init__(self, *_: object, **__: object) -> None:
            raise AssertionError("release timer constructed publication worker")

    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(factory, "create_repository", lambda _: repository)
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", lambda _: publisher)
    monkeypatch.setattr(
        CredentialCipher, "from_settings", lambda _: cipher
    )
    monkeypatch.setattr(function_app, "GatewayReleaseOperationWorker", ReleaseWorker)
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", PublicationWorker)
    monkeypatch.setattr(function_app, "TableStorageLedger", Ledger)
    monkeypatch.setattr(function_app, "prepare_application_ledger", prepare)
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "release-worker-instance")

    if fails:
        with pytest.raises(RuntimeError, match="worker failure"):
            function_app.process_gateway_release_operations(None)
    else:
        function_app.process_gateway_release_operations(None)

    repository.close.assert_called_once_with()
    assert calls == [("release-worker-instance", 180, 30)]
    assert ledger_calls == ([
        ("https://ledger.example.test", "TurnstileLedger"),
        (UUID(int=1), UUID(int=2)),
        ("closed",),
    ] if provisioning_enabled else [])


@pytest.mark.parametrize("fails", (False, True))
def test_publication_timer_closes_its_repository(
    monkeypatch: Any, tmp_path: Path, fails: bool,
) -> None:
    settings = Settings.model_construct(
        control_plane_enabled=True, gateway_publication_worker_enabled=True,
    )
    repository = MagicMock()
    worker = MagicMock()
    worker.run_once.return_value = None
    if fails:
        worker.run_once.side_effect = RuntimeError("worker failure")
    policy = tmp_path / "policy.xml"
    policy.write_text("<policies />")
    monkeypatch.setenv("CONTROL_PLANE_PARENT_POLICY_PATH", str(policy))
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(factory, "create_repository", lambda _: repository)
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", lambda _: object())
    monkeypatch.setattr(CredentialCipher, "from_settings", lambda _: object())
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", lambda *_, **__: worker)

    if fails:
        with pytest.raises(RuntimeError, match="worker failure"):
            function_app.publish_gateway_changes(None)
    else:
        function_app.publish_gateway_changes(None)

    repository.close.assert_called_once_with()