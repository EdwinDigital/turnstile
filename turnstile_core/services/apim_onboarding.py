from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import HttpUrl, ValidationError

from ..config import Settings
from ..domain.control_plane import ApimOnboardingOperations, GatewayReleaseOperationAccepted
from ..domain.gateway_targets import (
    ApimOnboardingConfiguration,
    ApimOnboardingPlan,
    ApimOnboardingRequest,
    ApimOnboardingStart,
    ApimTarget,
    ApimTargetCapabilitySnapshot,
    ApimTargetDriftState,
    ApimTargetOwnershipMode,
    ApimTierFamily,
    CapabilitySupport,
    GatewayDeclaredState,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayDeploymentKind,
    GatewayHostingMode,
    GatewayLifecycleOwnership,
    GatewayObservationState,
    GatewayReachabilityState,
    gateway_deployment_id_for,
)
from ..domain.ledger import LedgerScope
from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_onboarding_client import (
    AzureApimOnboardingClient,
    AzureApimPlatformConfigurationClient,
    onboarding_plan_digest,
)
from ..integrations.apim_onboarding_policy import (
    onboarding_policy_configuration_digest,
    render_onboarding_parent,
)
from ..integrations.atomic_ledger import AtomicTableLedger
from ..persistence.repository_contract import QueryRepository
from ..security import CredentialCipher, credential_hint
from .apim_onboarding_ledger import project_onboarding_ledger
from .budget_settlement import BudgetScopeRepository, BudgetScopeService
from .control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
    GatewayControlPlaneService,
)


@contextmanager
def onboarding_client(request: ApimOnboardingRequest) -> Iterator[AzureApimOnboardingClient]:
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        yield AzureApimOnboardingClient(request, client=client)


class ApimOnboardingService:
    def __init__(
        self,
        repository: QueryRepository,
        *,
        enabled: bool,
        configuration: ApimOnboardingConfiguration | None,
        canonical_parent: str | None,
        cipher: CredentialCipher | None,
        budget_ready: Callable[[], bool],
        client_factory: Callable[
            [ApimOnboardingRequest], AbstractContextManager[AzureApimOnboardingClient]
        ] = onboarding_client,
        projector: Callable[[ApimOnboardingPlan, UUID, str], None] | None = None,
        monthly_token_limit: int = 100_000,
        tokens_per_minute: int = 100_000,
        configuration_provider: Callable[[], ApimOnboardingConfiguration] | None = None,
    ) -> None:
        self._repository = repository
        self._configuration = configuration
        self._configuration_provider = configuration_provider
        self._canonical_parent = canonical_parent
        self._cipher = cipher
        self._client_factory = client_factory
        self._projector = projector
        self._budget_ready = budget_ready
        self._monthly_token_limit = monthly_token_limit
        self._tokens_per_minute = tokens_per_minute
        self._available = (
            enabled
            and (configuration is not None or configuration_provider is not None)
            and canonical_parent is not None
            and cipher is not None
        )
        self._operations = GatewayControlPlaneService(repository, release_worker_enabled=enabled)

    def operations(self) -> ApimOnboardingOperations:
        return ApimOnboardingOperations(
            available=self._available,
            unavailable_reason=None if self._available else "APIM initialization is not configured",
            items=[
                self._operations.release_operation(UUID(str(row["id"])))
                for row in self._repository.list_apim_onboarding_operations()
            ],
        )

    def _configuration_digest(self) -> str:
        if not self._available or self._canonical_parent is None:
            raise ControlPlaneUnavailableError("APIM initialization is unavailable")
        if self._configuration is None and self._configuration_provider is not None:
            self._configuration = self._configuration_provider()
        if self._configuration is None:
            raise ControlPlaneUnavailableError("Platform governance configuration is unavailable")
        return onboarding_policy_configuration_digest(self._configuration, self._canonical_parent)

    def preview(self, request: ApimOnboardingRequest) -> ApimOnboardingPlan:
        digest = self._configuration_digest()
        assert self._configuration is not None
        blockers = () if self._budget_ready() else ("budget_execution_not_ready",)
        with self._client_factory(request) as client:
            plan = client.inspect(
                management_tenant_id=self._configuration.management_tenant_id,
                configuration_digest=digest,
                configuration_blockers=blockers,
            )
        duplicate = any(
            str(row["arm_resource_id"]).casefold() == request.resource_id.casefold()
            and str(row["gateway_profile_id"]) != str(request.gateway_profile_id)
            for row in self._repository.list_apim_targets()
        )
        if duplicate:
            plan = plan.model_copy(
                update={
                    "ready": False,
                    "blockers": [*plan.blockers, "apim_already_registered"],
                }
            )
        plan = plan.model_copy(
            update={
                "initial_monthly_token_limit": self._monthly_token_limit,
                "initial_tokens_per_minute": self._tokens_per_minute,
            }
        )
        return plan.model_copy(update={"confirmation_sha256": onboarding_plan_digest(plan, digest)})

    def _accepted(self, operation_id: UUID) -> GatewayReleaseOperationAccepted:
        return GatewayReleaseOperationAccepted(
            operation=self._operations.release_operation(operation_id),
            status_url=f"/api/v1/model-management/release-operations/{operation_id}",
        )

    def start(self, write: ApimOnboardingStart, actor: str) -> GatewayReleaseOperationAccepted:
        self._configuration_digest()
        assert self._cipher is not None
        existing = next(
            (
                row
                for row in self._repository.list_apim_onboarding_operations()
                if str(row["gateway_profile_id"]) == str(write.request.gateway_profile_id)
            ),
            None,
        )
        if existing is not None:
            if existing["confirmation_sha256"] != write.confirmation_sha256:
                raise ControlPlaneConflictError("This APIM has a different initialization plan")
            return self._accepted(UUID(str(existing["id"])))
        plan = self.preview(write.request)
        if not plan.ready or plan.confirmation_sha256 != write.confirmation_sha256:
            raise ControlPlaneConflictError("APIM prerequisites changed; review the plan again")
        keys = {
            purpose: {kind: secrets.token_hex(32) for kind in ("primary", "secondary")}
            for purpose in ("dashboard", "probe")
        }
        row = self._repository.create_apim_onboarding_operation(
            plan,
            self._cipher.encrypt(json.dumps(keys)),
            actor,
        )
        return self._accepted(UUID(str(row["id"])))

    def retry(self, operation_id: UUID, actor: str) -> GatewayReleaseOperationAccepted:
        self._configuration_digest()
        prior = self._repository.get_gateway_release_operation(operation_id)
        if prior is None or prior["operation_kind"] != "apim_onboarding":
            raise ControlPlaneConflictError("APIM initialization operation was not found")
        plan = ApimOnboardingPlan.model_validate(prior["semantic_preview"])
        latest = self.preview(plan.request)
        if not latest.ready or latest.confirmation_sha256 != plan.confirmation_sha256:
            raise ControlPlaneConflictError(
                "Initialization plan changed or prerequisites are missing"
            )
        row = self._repository.retry_apim_onboarding_operation(operation_id, actor)
        return self._accepted(UUID(str(row["id"])))

    def _keys(self, operation_id: UUID) -> dict[str, dict[str, str]]:
        assert self._cipher is not None
        plaintext = self._cipher.decrypt(
            self._repository.gateway_release_operation_secret(operation_id)
        )
        if plaintext is None:
            raise PolicyCompilationError("APIM initialization credentials are unavailable")
        try:
            values = json.loads(plaintext)
            valid = set(values) == {"dashboard", "probe"} and all(
                set(values[purpose]) == {"primary", "secondary"}
                and all(
                    isinstance(value, str) and len(value) >= 32
                    for value in values[purpose].values()
                )
                and values[purpose]["primary"] != values[purpose]["secondary"]
                for purpose in ("dashboard", "probe")
            )
            if not valid:
                raise ValueError("Invalid key structure")
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise PolicyCompilationError("APIM initialization credentials are invalid") from error
        return cast(dict[str, dict[str, str]], values)

    def _transition(
        self,
        row: Mapping[str, Any],
        worker_id: str,
        status: str,
        completed: list[str],
        **details: object,
    ) -> None:
        changed = self._repository.transition_gateway_release_operation(
            UUID(str(row["id"])),
            str(row["status"]),
            status,
            {
                "checkpoint": {
                    **dict(row.get("checkpoint") or {}),
                    **details,
                    "step": completed[-1],
                    "completed_steps": completed,
                },
                "error_code": None,
                "error_message": None,
            },
            worker_id,
        )
        if changed is None:
            raise ControlPlaneConflictError("The initialization lease changed")

    def advance(self, row: Mapping[str, Any], worker_id: str) -> None:
        digest = self._configuration_digest()
        if self._projector is None:
            raise ControlPlaneUnavailableError(
                "APIM initialization ledger projection is unavailable"
            )
        try:
            plan = ApimOnboardingPlan.model_validate(row["semantic_preview"])
        except (ValidationError, KeyError) as error:
            raise PolicyCompilationError("Stored APIM initialization plan is invalid") from error
        if (
            row["operation_kind"] != "apim_onboarding"
            or str(row["gateway_profile_id"]) != str(plan.request.gateway_profile_id)
            or plan.confirmation_sha256 != row["confirmation_sha256"]
            or onboarding_plan_digest(plan, digest) != plan.confirmation_sha256
        ):
            raise PolicyCompilationError(
                "APIM initialization plan identity or configuration changed"
            )
        if not self._budget_ready():
            raise ControlPlaneUnavailableError("Budget execution prerequisites are not ready")
        assert self._configuration is not None and self._canonical_parent is not None
        assert self._cipher is not None
        operation_id = UUID(str(row["id"]))
        status = str(row["status"])
        with self._client_factory(plan.request) as client:
            if status == "queued":
                current = self.preview(plan.request)
                if not current.ready or current.confirmation_sha256 != plan.confirmation_sha256:
                    raise PolicyCompilationError("APIM initialization preflight no longer matches")
                client.verify_initialization_permissions(self._configuration)
                self._transition(row, worker_id, "validating_dependencies", plan.steps[:1])
            elif status == "validating_dependencies":
                client.ensure_api()
                client.ensure_catalog()
                self._transition(row, worker_id, "promoting", plan.steps[:2])
            elif status == "promoting":
                principal = client.ensure_governance_dependencies(self._configuration)
                client.ensure_policy(
                    client.api_resource_path,
                    render_onboarding_parent(
                        plan.request,
                        self._configuration,
                        self._canonical_parent,
                    ),
                )
                self._transition(
                    row,
                    worker_id,
                    "verifying_readback",
                    plan.steps[:3],
                    apim_principal_id=str(principal),
                )
            elif status == "verifying_readback":
                client.ensure_subscriptions(self._keys(operation_id))
                self._materialize(plan, UUID(str(row["checkpoint"]["apim_principal_id"])), False)
                self._projector(plan, operation_id, worker_id)
                self._transition(row, worker_id, "preflight_probing", plan.steps[:5])
            elif status == "preflight_probing":
                keys = self._keys(operation_id)
                client.ensure_api()
                client.ensure_catalog()
                client.ensure_policy(
                    client.api_resource_path,
                    render_onboarding_parent(
                        plan.request,
                        self._configuration,
                        self._canonical_parent,
                    ),
                )
                client.ensure_subscriptions(keys, activate=True)
                client.verify_managed_gateway(plan, keys)
                self._materialize(plan, UUID(str(row["checkpoint"]["apim_principal_id"])), True)
                result = self._repository.finish_apim_onboarding_operation(
                    operation_id,
                    worker_id,
                    self._cipher.encrypt(keys["dashboard"]["primary"]),
                    credential_hint(keys["dashboard"]["primary"]),
                    digest,
                )
                if result is None:
                    raise ControlPlaneConflictError(
                        "Initialization lease changed before activation"
                    )
            else:
                raise PolicyCompilationError("APIM initialization has an unsupported state")

    def _materialize(self, plan: ApimOnboardingPlan, principal_id: UUID, verified: bool) -> None:
        now = datetime.now(UTC)
        family = (
            ApimTierFamily.V2
            if plan.service_tier.casefold().endswith("v2")
            else ApimTierFamily.CLASSIC
        )
        capabilities = ApimTargetCapabilitySnapshot(
            self_hosted_gateway=CapabilitySupport.SUPPORTED
            if plan.service_tier.casefold() in {"developer", "premium"}
            else CapabilitySupport.UNSUPPORTED,
            backend_pools=CapabilitySupport.SUPPORTED,
            managed_identity_policy=CapabilitySupport.SUPPORTED,
            event_hub_logger=CapabilitySupport.SUPPORTED,
            table_ledger=CapabilitySupport.SUPPORTED,
            responses=CapabilitySupport.SUPPORTED,
            anthropic_messages=CapabilitySupport.SUPPORTED,
            response_observer=CapabilitySupport.UNSUPPORTED,
            source_api_version="2024-05-01",
            observed_at=now,
        )
        self._repository.upsert_apim_target(
            ApimTarget(
                gateway_profile_id=plan.gateway_profile_id,
                arm_resource_id=plan.request.resource_id,
                tenant_id=plan.request.tenant_id,
                subscription_id=plan.request.subscription_id,
                resource_group=plan.request.resource_group,
                service_name=plan.service_name,
                api_id=plan.api_id,
                api_path=plan.api_path,
                service_tier=plan.service_tier,
                tier_family=family,
                service_location=plan.location,
                principal_id=principal_id,
                management_auth_profile_ref="system_managed_identity",
                probe_subscription_id=plan.probe_subscription_id,
                ownership_mode=ApimTargetOwnershipMode.TURNSTILE_MANAGED,
                capabilities=capabilities,
                last_inventory_sync_at=now,
                last_validated_at=now if verified else None,
                drift_state=ApimTargetDriftState.IN_SYNC
                if verified
                else ApimTargetDriftState.UNKNOWN,
            )
        )
        deployment = GatewayDeployment(
            id=gateway_deployment_id_for(plan.gateway_profile_id, "managed"),
            apim_target_id=plan.gateway_profile_id,
            native_gateway_id="managed",
            kind=GatewayDeploymentKind.MANAGED,
            display_name=f"{plan.service_name} Managed",
            location=plan.location,
            endpoint_url=HttpUrl(
                str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path
            ),
            hosting_mode=GatewayHostingMode.AZURE_MANAGED,
            lifecycle_ownership=GatewayLifecycleOwnership.EXTERNAL,
            declared_state=GatewayDeclaredState.PRESENT,
            reachability_state=GatewayReachabilityState.REACHABLE
            if verified
            else GatewayReachabilityState.UNKNOWN,
            observation_state=GatewayObservationState.OBSERVED
            if verified
            else GatewayObservationState.UNKNOWN,
            last_arm_sync_at=now,
            last_probe_at=now if verified else None,
            last_observed_at=now if verified else None,
        )
        self._repository.upsert_gateway_deployment(deployment)
        self._repository.upsert_gateway_deployment_inventory(
            GatewayDeploymentInventory(
                deployment_id=deployment.id,
                associated_api_ids=(plan.api_id,),
                source_api_version="2024-05-01",
                synced_at=now,
            )
        )


def onboarding_template_path(settings: Settings) -> Path:
    path = settings.control_plane_parent_policy_path
    if path.is_absolute() or path.is_file():
        return path
    root = Path(__file__).resolve().parents[2]
    packaged = root / path
    if packaged.is_file() or path != Path("policies/foundry-finops-policy.xml"):
        return packaged
    return root / "infra" / path


def initialized_apim_targets(repository: QueryRepository) -> dict[UUID, ApimOnboardingRequest]:
    requests: dict[UUID, ApimOnboardingRequest] = {}
    targets = {str(row["gateway_profile_id"]): row for row in repository.list_apim_targets()}
    for profile in repository.registry()["gateways"]:
        config = profile.get("config") or {}
        if (
            profile.get("implementation") != "apim"
            or not profile.get("enabled")
            or config.get("onboarding_ready") is not True
        ):
            continue
        try:
            request = ApimOnboardingRequest.model_validate(config.get("onboarding_request"))
        except ValidationError as error:
            raise PolicyCompilationError(
                "An initialized APIM has invalid persisted identity"
            ) from error
        target = targets.get(str(profile["id"]))
        if (
            str(request.gateway_profile_id) != str(profile["id"])
            or target is None
            or target["ownership_mode"] != "turnstile_managed"
            or str(target["arm_resource_id"]).casefold() != request.resource_id.casefold()
            or str(target["tenant_id"]) != str(request.tenant_id)
            or target["api_id"] != request.api_id
            or target["api_path"] != request.api_path
            or target["probe_subscription_id"] != request.probe_subscription_id
        ):
            raise PolicyCompilationError(
                "An initialized APIM target no longer matches its identity"
            )
        requests[request.gateway_profile_id] = request
    return requests


def apim_onboarding_available(settings: Settings) -> bool:
    return bool(
        settings.apim_target_registry_enabled
        and settings.gateway_release_worker_enabled
        and settings.budget_execution_enabled
        and settings.ledger_table_endpoint
        and settings.credential_encryption_key
        and settings.legacy_apim_identity
        and settings.data_backend == "postgresql"
        and onboarding_template_path(settings).is_file()
    )


def build_apim_onboarding_service(
    repository: QueryRepository,
    settings: Settings,
) -> ApimOnboardingService:
    enabled = apim_onboarding_available(settings)
    source = onboarding_template_path(settings).read_text(encoding="utf-8") if enabled else None
    resolved_configuration: ApimOnboardingConfiguration | None = None

    def configuration() -> ApimOnboardingConfiguration:
        nonlocal resolved_configuration
        if resolved_configuration is None:
            management_settings = settings.model_copy(
                update={
                    "apim_gateway_url": settings.apim_gateway_url
                    or f"https://{settings.apim_service_name}.azure-api.net",
                }
            )
            with httpx.Client(timeout=30.0, follow_redirects=False) as http:
                current = AzureApimPlatformConfigurationClient(
                    management_settings,
                    client=http,
                ).configuration()
            ledger = current.ledger_table_resource_id.split("/")
            if (
                (settings.ledger_table_endpoint or "").rstrip("/")
                != f"https://{ledger[8]}.table.core.windows.net"
                or settings.ledger_table_name != ledger[-1]
            ):
                raise PolicyCompilationError(
                    "The platform governance configuration does not match its budget ledger"
                )
            resolved_configuration = current
        return resolved_configuration

    def budget_ready() -> bool:
        if not isinstance(repository, BudgetScopeRepository):
            return False
        rollout = repository.active_budget_execution_rollout()
        return rollout is not None and rollout["prepared_period"] == datetime.now(
            UTC
        ).date().replace(day=1)

    def project(plan: ApimOnboardingPlan, operation_id: UUID, worker_id: str) -> None:
        current = configuration()
        parts = current.ledger_table_resource_id.split("/")
        if (
            not settings.ledger_table_endpoint
            or settings.ledger_table_endpoint.rstrip("/")
            != f"https://{parts[8]}.table.core.windows.net"
            or settings.ledger_table_name != parts[-1]
        ):
            raise PolicyCompilationError(
                "APIM initialization must use the platform's configured ledger"
            )
        if not isinstance(repository, BudgetScopeRepository):
            raise PolicyCompilationError("APIM initialization requires durable budget scopes")
        with AtomicTableLedger(
            settings.ledger_table_endpoint, settings.ledger_table_name
        ) as ledger:

            def prepare_scope(application_id: UUID, epoch: UUID) -> None:
                if not budget_ready():
                    raise RetryablePublicationError("Budget period preparation is pending")
                prepared = BudgetScopeService(repository, ledger).prepare(
                    application_id=application_id,
                    gateway_profile_id=plan.gateway_profile_id,
                )
                scope = LedgerScope(
                    scope_type="application",
                    scope_id=str(application_id),
                    period_start=datetime.now(UTC).date().replace(day=1),
                )
                summary = ledger.read_summary(scope)
                if prepared != 1 or summary.epoch != epoch or not summary.enabled:
                    raise RetryablePublicationError("The new Application budget is not yet active")

            project_onboarding_ledger(
                repository, ledger, plan, operation_id, worker_id, prepare_scope
            )

    return ApimOnboardingService(
        repository,
        enabled=enabled,
        configuration=None,
        configuration_provider=configuration if enabled else None,
        canonical_parent=source,
        cipher=CredentialCipher.from_settings(settings) if enabled else None,
        budget_ready=budget_ready,
        projector=project if enabled else None,
        monthly_token_limit=settings.gateway_application_default_monthly_token_limit,
        tokens_per_minute=settings.gateway_application_default_tokens_per_minute,
    )
