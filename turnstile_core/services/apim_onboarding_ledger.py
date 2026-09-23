from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from ..domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    application_id_for,
)
from ..domain.gateway_targets import ApimOnboardingPlan
from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.ledger import TableStorageLedger
from ..persistence.repository_contract import QueryRepository
from .application_access import ApplicationAccessService
from .application_provisioning_ledger import prepare_application_ledger


def project_onboarding_ledger(
    repository: QueryRepository,
    ledger: TableStorageLedger,
    plan: ApimOnboardingPlan,
    operation_id: UUID,
    worker_id: str,
    prepare_execution_scope: Callable[[UUID, UUID], None],
) -> None:
    rollout = repository.enroll_apim_onboarding_budget_target(operation_id, worker_id)
    epoch = UUID(str(rollout["epoch"]))
    if plan.gateway_profile_id not in rollout["gateway_profile_ids"]:
        raise PolicyCompilationError("Budget execution did not enroll the expected APIM target")
    operation = repository.get_gateway_release_operation(operation_id)
    if operation is None:
        raise PolicyCompilationError("APIM initialization operation disappeared")
    subscriptions: tuple[tuple[str, Literal["service", "system"]], ...] = (
        (plan.dashboard_subscription_id, "service"),
        (plan.probe_subscription_id, "system"),
    )
    for identifier, kind in subscriptions:
        application = repository.get_gateway_application(
            application_id_for(plan.gateway_profile_id, identifier)
        )
        if application is not None and (
            application["status"] != "active"
            or application["application_type"] != kind
            or str(application["gateway_profile_id"]) != str(plan.gateway_profile_id)
        ):
            raise PolicyCompilationError(
                "A staged system application changed outside initialization"
            )
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        default_token_limit=plan.initial_monthly_token_limit,
        default_tokens_per_minute=plan.initial_tokens_per_minute,
    )
    service.sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=plan.gateway_profile_id,
            discovered_at=datetime.now(UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id=identifier,
                    display_name=f"Turnstile {identifier}",
                    state="active",
                    scope_type="api",
                    scope_id=plan.api_id,
                    scope_exists=True,
                    application_type=kind,
                    system_managed=kind == "system",
                )
                for identifier, kind in subscriptions
            ],
        ),
        str(operation["created_by"]),
    )
    for identifier, kind in subscriptions:
        application_id = application_id_for(plan.gateway_profile_id, identifier)
        prepare_application_ledger(repository, ledger, plan.gateway_profile_id, application_id)
        if kind != "system":
            prepare_execution_scope(application_id, epoch)
    service_hash = hashlib.sha256(plan.service_name.lower().encode()).hexdigest()
    partition = f"budget-protocol|{service_hash}|{plan.api_id}"
    fence = {
        "protocol_version": 2,
        "epoch": str(epoch),
        "gateway_profile_id": str(plan.gateway_profile_id),
        "apim_service_id": plan.service_name.lower(),
        "api_id": plan.api_id,
        "mode": "active",
        "gateways": {"managed": ["1"]},
        "model_contracts": {},
    }
    ledger.insert_if_missing(partition, "F", {"Document": json.dumps(fence, sort_keys=True)})
    observed = ledger.read_entity(partition, "F")
    if observed is None:
        raise RetryablePublicationError("The new APIM budget fence is not yet readable")
    try:
        document = json.loads(str(observed.get("Document", "")))
    except ValueError as error:
        raise PolicyCompilationError("The new APIM budget fence has an invalid document") from error
    if document != fence:
        raise PolicyCompilationError("The APIM budget fence exists with another configuration")
