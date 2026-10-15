from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx

from ..domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    application_id_for,
)
from ..domain.gateway_targets import (
    ApimOnboardingPlan,
    ApimTargetArmInventory,
    CapabilitySupport,
    GatewayDeclaredState,
    GatewayDeployment,
    GatewayDeploymentKind,
)
from ..integrations.apim_control_plane_contract import (
    ApimPublisherClient,
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_policy_components import parse_policy, validate_parent_policy
from ..integrations.ledger import TableStorageLedger
from ..persistence.repository import QueryRepository
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
    actor = str(operation["created_by"])
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
                "A staged system application was changed outside initialization"
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
        actor,
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
        raise PolicyCompilationError(
            "The APIM budget fence already exists with another configuration"
        )


def enroll_self_hosted_gateway_protocol(
    repository: QueryRepository,
    ledger: TableStorageLedger,
    inventory: ApimTargetArmInventory,
    expected_fence: Mapping[str, object],
    client: ApimPublisherClient,
    canonical_parent: str,
    *,
    before_write: Callable[[], None] | None = None,
    verify_fixed_program: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    target = repository.get_apim_target(inventory.gateway_profile_id)
    if target is None or any(
        str(target.get(name, "")).casefold() != str(getattr(inventory, name)).casefold()
        for name in (
            "gateway_profile_id", "arm_resource_id", "subscription_id", "resource_group",
            "service_name", "api_id", "principal_id",
        )
    ) or target.get("api_path") != inventory.api_path:
        raise PolicyCompilationError("Gateway protocol registration target identity changed")
    if (
        target.get("drift_state") != "in_sync"
        or target.get("last_inventory_sync_at") != inventory.discovered_at
    ):
        raise RetryablePublicationError(
            "Gateway protocol registration requires the latest inventory"
        )
    discovered = {gateway.native_gateway_id.casefold(): gateway for gateway in inventory.gateways}
    candidates: list[str] = []
    configured: dict[UUID, GatewayDeployment] = {}
    retiring: set[str] = set()
    expected_gateways = expected_fence.get("gateways")
    for row in repository.list_gateway_deployments(
        inventory.gateway_profile_id, include_retired=True,
    ):
        deployment = GatewayDeployment.model_validate({
            name: row[name] for name in GatewayDeployment.model_fields if name in row
        })
        if deployment.kind is not GatewayDeploymentKind.SELF_HOSTED:
            continue
        if deployment.retired_at is not None:
            if (
                isinstance(expected_gateways, dict)
                and deployment.native_gateway_id in expected_gateways
            ):
                retiring.add(deployment.native_gateway_id)
                configured[deployment.id] = deployment
            continue
        if deployment.endpoint_url is None:
            continue
        if inventory.capabilities.self_hosted_gateway is not CapabilitySupport.SUPPORTED:
            raise PolicyCompilationError("APIM does not support the configured Self-hosted Gateway")
        gateway = discovered.get(deployment.native_gateway_id.casefold())
        if gateway is None or deployment.declared_state is not GatewayDeclaredState.PRESENT:
            raise RetryablePublicationError(
                "The configured Gateway is missing from Azure inventory; "
                "verify Publisher Gateway read access and synchronize again"
            )
        if inventory.api_id.casefold() not in {
            name.casefold() for name in gateway.associated_api_ids
        }:
            raise PolicyCompilationError(
                "The configured Gateway is not associated with the APIM API"
            )
        expected_resource = f"{inventory.arm_resource_id}/gateways/{gateway.native_gateway_id}"
        if (
            deployment.apim_target_id != inventory.gateway_profile_id
            or gateway.arm_resource_id.casefold() != expected_resource.casefold()
        ):
            raise PolicyCompilationError("Gateway protocol registration returned a foreign Gateway")
        candidates.append(gateway.native_gateway_id)
        configured[deployment.id] = deployment
    candidates.sort(key=str.casefold)
    if not candidates and not retiring:
        return ()

    revision, policy = client.current_api_policy()
    if revision != inventory.api_revision:
        raise RetryablePublicationError(
            "The current API Revision changed during Gateway registration"
        )
    fixed = parse_policy(policy).find(".//set-variable[@name='routeProgram']")
    if fixed is not None:
        if verify_fixed_program is None:
            raise PolicyCompilationError(
                "Gateway registration requires complete fixed program evidence",
            )
        verify_fixed_program()
    else:
        validate_parent_policy(policy, canonical_parent)
    version = parse_policy(policy).find("./inbound/set-variable[@name='budgetExecutionVersion']")
    if version is None or version.get("value") != "@(2)":
        raise PolicyCompilationError("Gateway registration requires the budget execution policy")

    service_name = inventory.service_name.lower()
    partition = (
        f"budget-protocol|{hashlib.sha256(service_name.encode()).hexdigest()}|{inventory.api_id}"
    )
    try:
        epoch = UUID(str(expected_fence.get("epoch", "")))
    except ValueError as error:
        raise PolicyCompilationError("Gateway protocol registration has no valid epoch") from error
    if (
        type(expected_fence.get("protocol_version")) is not int
        or expected_fence.get("protocol_version") != 2 or expected_fence.get("mode") != "active"
        or expected_fence.get("gateway_profile_id") != str(inventory.gateway_profile_id)
        or expected_fence.get("apim_service_id") != service_name
        or expected_fence.get("api_id") != inventory.api_id
        or not isinstance(expected_fence.get("model_contracts"), dict)
    ):
        raise PolicyCompilationError(
            "Gateway protocol registration requires its active target fence"
        )
    protected = json.dumps({
        name: value for name, value in expected_fence.items() if name != "gateways"
    }, sort_keys=True)
    preserved_gateways = expected_fence.get("gateways")
    if not isinstance(preserved_gateways, dict):
        raise PolicyCompilationError("The Gateway budget protocol enrollment is invalid")
    preserved_gateways = dict(preserved_gateways)
    uncertain_write = False

    for attempt in range(4):
        current = ledger.read_versioned_entity(partition, "F")
        if current is None:
            raise RetryablePublicationError("The existing Gateway budget protocol is unavailable")
        entity, etag = current
        try:
            fence: Any = json.loads(entity.get("Document", ""))
        except (TypeError, ValueError) as error:
            raise PolicyCompilationError(
                "The Gateway budget protocol document is invalid"
            ) from error
        if (
            not isinstance(fence, dict)
            or fence.get("epoch") != str(epoch)
            or json.dumps({
                name: value for name, value in fence.items() if name != "gateways"
            }, sort_keys=True) != protected
        ):
            raise PolicyCompilationError(
                "The Gateway budget protocol changed; registration is blocked"
            )
        gateways = fence.get("gateways")
        if not isinstance(gateways, dict) or not gateways or any(
            not isinstance(name, str) or not name.strip()
            or not isinstance(revisions, list) or not revisions
            or any(not isinstance(value, str) or not value.strip() for value in revisions)
            for name, revisions in gateways.items()
        ) or len({name.casefold() for name in gateways}) != len(gateways):
            raise PolicyCompilationError("The Gateway budget protocol enrollment is invalid")
        if any(
            gateways.get(name) != revisions and not (name in retiring and name not in gateways)
            for name, revisions in preserved_gateways.items()
        ):
            raise PolicyCompilationError("Existing Gateway registration changed concurrently")
        preserved_gateways = dict(gateways)
        missing = [name for name in candidates if name not in gateways]
        revoked = retiring.intersection(gateways)
        if any(name.casefold() in {key.casefold() for key in gateways} for name in missing):
            raise PolicyCompilationError("The registered Gateway identity has different casing")
        if not missing and not revoked:
            return tuple(candidates)
        if uncertain_write:
            raise RetryablePublicationError(
                "Gateway registration outcome is uncertain; synchronize again"
            )
        if attempt == 3:
            break
        if client.current_revision() != revision:
            raise RetryablePublicationError("The API Revision changed before Gateway registration")
        current_target = repository.get_apim_target(inventory.gateway_profile_id)
        if current_target != target:
            raise RetryablePublicationError("The APIM target changed before Gateway registration")
        for deployment_id, original in configured.items():
            current_deployment = repository.get_gateway_deployment(deployment_id)
            if current_deployment is None or GatewayDeployment.model_validate({
                name: current_deployment[name]
                for name in GatewayDeployment.model_fields if name in current_deployment
            }) != original:
                raise RetryablePublicationError(
                    "The Gateway association changed before registration"
                )
        updated_gateways = {
            **{name: revisions for name, revisions in gateways.items() if name not in revoked},
            **{name: [revision] for name in missing},
        }
        if not updated_gateways:
            raise PolicyCompilationError("Gateway synchronization cannot remove every registration")
        updated = {**fence, "gateways": updated_gateways}
        document = json.dumps(updated, sort_keys=True)
        if len(document.encode("utf-16-le")) > 60_000:
            raise PolicyCompilationError("Gateway protocol registration exceeds its document limit")
        replacement = {
            name: value for name, value in entity.items()
            if name not in {"Timestamp", "Document"} and not name.startswith("odata.")
        }
        replacement["Document"] = document
        if before_write is not None:
            before_write()
        try:
            if ledger.replace_if_match(partition, "F", replacement, etag):
                preserved_gateways = dict(updated["gateways"])
        except httpx.TransportError:
            uncertain_write = True
            continue
    raise RetryablePublicationError(
        "Gateway protocol registration requires a fresh conditional retry"
    )
