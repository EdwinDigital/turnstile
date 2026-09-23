from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import azure.functions as func

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.control_plane import GatewayReleaseRetentionPolicy
from turnstile_core.integrations.apim_budget_execution import compose_budget_execution_parent
from turnstile_core.integrations.apim_control_plane import (
    ApimPolicyCompiler,
    AzureApimPublisherClient,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    ApimPublisherClient,
    ApimPublisherClientResolver,
)
from turnstile_core.integrations.apim_target_client_factory import (
    ApimTargetClientConfigResolver,
    AzureApimPublisherClientResolver,
    apim_target_client_config_resolver,
)
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.ledger import TableStorageLedger
from turnstile_core.persistence.factory import repository_session
from turnstile_core.persistence.repository_contract import QueryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_provisioning_ledger import prepare_application_ledger
from turnstile_core.services.budget_settlement import BudgetScopeRepository, BudgetScopeService
from turnstile_core.services.gateway_publication_worker import GatewayPublicationWorker
from turnstile_core.services.gateway_release_operation_worker import (
    GatewayReleaseOperationWorker,
)

app = func.FunctionApp()
logger = logging.getLogger(__name__)


def _publisher_clients(
    repository: QueryRepository, settings: Settings,
) -> tuple[
    ApimPublisherClient | ApimPublisherClientResolver, ApimTargetClientConfigResolver | None,
]:
    if getattr(settings, "apim_target_registry_enabled", False):
        targets = apim_target_client_config_resolver(repository, settings)
        return AzureApimPublisherClientResolver(targets), targets
    return AzureApimPublisherClient(settings), None


def _target_budget_configuration(
    gateway_id: UUID, settings: Settings, targets: ApimTargetClientConfigResolver | None,
) -> dict[str, object]:
    scoped = targets.resolve(gateway_id).settings if targets is not None else settings
    return _budget_configuration(gateway_id, scoped)


def _budget_parent_overrides(source: str, settings: Settings) -> dict[UUID, str]:
    targets = getattr(settings, "budget_execution_target_ids", [])
    if not targets:
        return {}
    if not settings.budget_execution_enabled:
        raise ValueError("Budget publication requires the execution reader")
    return {target: compose_budget_execution_parent(
        source, source, enabled=True, delegation=settings.budget_delegation_trust.get(target),
    ) for target in targets}


def _budget_configuration(gateway_id: UUID, settings: Settings) -> dict[str, object]:
    if (not settings.budget_execution_enabled
            or gateway_id not in settings.budget_execution_target_ids
            or not settings.ledger_table_endpoint or not settings.apim_service_name):
        raise ValueError("Budget publication target is not enabled")
    service = settings.apim_service_name.lower()
    partition = (
        f"budget-protocol|{hashlib.sha256(service.encode()).hexdigest()}|{settings.apim_api_id}"
    )
    ledger = AtomicTableLedger(
        settings.ledger_table_endpoint, settings.ledger_table_name, timeout=2,
    )
    with ledger:
        entity = ledger.read_entity(partition, "F")
    fence = json.loads(entity["Document"]) if entity is not None else None
    if (not isinstance(fence, dict) or fence.get("protocol_version") != 2
            or fence.get("mode") != "active" or fence.get("gateway_profile_id") != str(gateway_id)
            or fence.get("api_id") != settings.apim_api_id
            or str(fence.get("apim_service_id", "")).lower() != service):
        raise ValueError("Budget publication fence does not match its configured target")
    return fence


@app.timer_trigger(
    arg_name="timer",
    schedule="*/30 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def publish_gateway_changes(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not (
        settings.control_plane_enabled
        and settings.gateway_publication_worker_enabled
    ):
        return
    with repository_session(settings) as repository:
        publisher, targets = _publisher_clients(repository, settings)
        worker_id = os.environ.get("WEBSITE_INSTANCE_ID", "control-plane-local")[:128]
        policy_path = Path(
            os.environ.get(
                "CONTROL_PLANE_PARENT_POLICY_PATH",
                "policies/foundry-finops-policy.xml",
            )
        )
        if not policy_path.is_file():
            logger.error("Gateway publication skipped: parent policy baseline is unavailable")
        else:
            parent_policy = policy_path.read_text(encoding="utf-8")
            overrides = _budget_parent_overrides(parent_policy, settings)
            budget_options: dict[str, Any] = {}
            if overrides:
                budget_options = {
                    "parent_policy_overrides": overrides,
                    "budget_configuration": lambda gateway_id: _target_budget_configuration(
                        gateway_id, settings, targets,
                    ),
                }
            worker = GatewayPublicationWorker(
                repository,
                publisher,
                parent_policy,
                ApimPolicyCompiler(
                    settings.apim_probe_subscription_id,
                    settings.apim_usage_observer_url,
                    settings.apim_usage_observer_key_named_value,
                ),
                CredentialCipher.from_settings(settings),
                chat_completions_operation_id=settings.apim_chat_completions_operation_id,
                responses_operation_id=settings.apim_responses_operation_id,
                responses_compact_operation_id=settings.apim_responses_compact_operation_id,
                messages_operation_id=settings.apim_messages_operation_id,
                count_tokens_operation_id=settings.apim_count_tokens_operation_id,
                models_operation_id=settings.apim_models_operation_id,
                **budget_options,
            )
            result = worker.run_once(
                worker_id=worker_id,
                lease_seconds=settings.control_plane_lease_seconds,
                max_attempts=settings.control_plane_max_attempts,
            )
            if result is not None:
                logger.info(
                    "Gateway publication %s advanced to %s",
                    result.id,
                    result.status.value,
                )


@app.timer_trigger(
    arg_name="timer",
    schedule="15,45 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)
def process_gateway_release_operations(timer: func.TimerRequest) -> None:
    del timer
    settings = get_settings()
    if not (
        settings.control_plane_enabled
        and settings.gateway_release_worker_enabled
    ):
        return
    with repository_session(settings) as repository:
        publisher, _ = _publisher_clients(repository, settings)
        worker_id = os.environ.get("WEBSITE_INSTANCE_ID", "control-plane-local")[:128]

        def project_application(gateway_id: UUID, application_id: UUID) -> None:
            if not settings.ledger_table_endpoint:
                raise RuntimeError("Application admission ledger is not configured")
            if gateway_id in getattr(settings, "budget_execution_target_ids", []):
                if not isinstance(repository, BudgetScopeRepository):
                    raise RuntimeError("Application execution needs a durable repository")
                atomic_store = AtomicTableLedger(
                    settings.ledger_table_endpoint, settings.ledger_table_name,
                )
                with atomic_store:
                    prepare_application_ledger(repository, atomic_store, gateway_id, application_id)
                    BudgetScopeService(repository, atomic_store).prepare(
                        application_id=application_id, gateway_profile_id=gateway_id,
                    )
                return
            with TableStorageLedger(
                settings.ledger_table_endpoint, settings.ledger_table_name
            ) as store:
                prepare_application_ledger(repository, store, gateway_id, application_id)

        policy_path = Path(
            os.environ.get("CONTROL_PLANE_PARENT_POLICY_PATH", "policies/foundry-finops-policy.xml")
        )
        parent_policy = policy_path.read_text(encoding="utf-8") if policy_path.is_file() else None
        overrides = _budget_parent_overrides(parent_policy or "", settings)
        release_worker = GatewayReleaseOperationWorker(
            repository,
            publisher,
            GatewayReleaseRetentionPolicy(
                retained_count=settings.gateway_release_retention_count,
                retained_days=settings.gateway_release_retention_days,
                failed_retained_days=settings.gateway_failed_release_retention_days,
                protected_labels=settings.gateway_release_protected_labels,
            ),
            application_default_token_limit=(settings.gateway_application_default_monthly_token_limit),
            application_default_tokens_per_minute=(
                settings.gateway_application_default_tokens_per_minute
            ),
            cipher=CredentialCipher.from_settings(settings),
            parent_policy=parent_policy,
            **({"parent_policy_overrides": overrides} if overrides else {}),
            application_projector=(
                project_application
                if (
                    settings.gateway_application_provisioning_enabled
                    and settings.ledger_table_endpoint
                )
                else None
            ),
        )
        operation = release_worker.run_once(
            worker_id=worker_id,
            lease_seconds=settings.control_plane_lease_seconds,
            max_attempts=settings.control_plane_max_attempts,
        )
        if operation is not None:
            logger.info(
                "Gateway release operation %s advanced to %s",
                operation.id,
                operation.status.value,
            )