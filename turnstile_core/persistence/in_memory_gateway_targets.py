from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from ..domain.control_plane import GatewayPublication
from ..domain.gateway_targets import (
    ApimOnboardingPlan,
    ApimTarget,
    ApimTargetRouteBinding,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayDeploymentScopeMode,
    GatewayInstanceObservation,
    SelfHostedGatewayProvisionSpec,
    UsageGatewayAttribution,
    gateway_provision_digest,
    validate_gateway_provision_target,
)
from ..domain.models import TokenUsageRecord


class InMemoryGatewayTargetRepositoryMixin:
    gateways: list[dict[str, Any]]
    runtimes: list[dict[str, Any]]
    models: list[dict[str, Any]]
    gateway_publications: list[dict[str, Any]]
    gateway_publication_audit: list[dict[str, Any]]
    effective_gateway_releases: dict[UUID, UUID]
    usage_records: list[TokenUsageRecord]
    apim_targets: dict[UUID, dict[str, Any]]
    gateway_deployments: dict[UUID, dict[str, Any]]
    gateway_instance_observations: dict[tuple[UUID, str], dict[str, Any]]
    gateway_deployment_inventories: dict[UUID, dict[str, Any]]
    apim_target_route_bindings: dict[UUID, dict[str, Any]]
    usage_gateway_attributions: dict[str, dict[str, Any]]
    gateway_release_operations: list[dict[str, Any]]
    gateway_release_operation_secrets: dict[UUID, bytes]
    gateway_release_operation_audit: list[dict[str, Any]]

    def list_self_hosted_gateway_provisions(self) -> Sequence[dict[str, Any]]:
        enabled = {
            row["id"] for row in self.gateways if row["enabled"] and row["implementation"] == "apim"
        }
        return sorted(
            (
                row
                for row in self.gateway_release_operations
                if row["operation_kind"] == "self_hosted_gateway_provision"
                and row["gateway_profile_id"] in enabled
            ),
            key=lambda row: row["created_at"],
            reverse=True,
        )[:200]

    def retry_self_hosted_gateway_provision(
        self,
        operation_id: UUID,
        actor: str,
    ) -> dict[str, Any]:
        row = next(
            (row for row in self.gateway_release_operations if row["id"] == operation_id), None
        )
        if row is None or row["operation_kind"] != "self_hosted_gateway_provision":
            raise ValueError("Gateway creation operation was not found")
        if row["status"] in {"queued", "validating_dependencies", "verifying_readback"}:
            return row
        if row["status"] != "failed":
            raise ValueError("Only failed Gateway creation can be retried")
        if any(
            other["gateway_profile_id"] == row["gateway_profile_id"]
            and other["id"] != operation_id
            and other["status"] not in {"succeeded", "failed", "restored"}
            for other in self.gateway_release_operations
        ):
            raise ValueError("Another Gateway operation is already in progress")
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "from_status": "failed",
                "to_status": "queued",
                "actor": actor,
                "created_at": datetime.now(UTC),
                "detail": {
                    "retry": True,
                    "error_code": row["error_code"],
                    "error_message": row["error_message"],
                    "attempt_count": row["attempt_count"],
                    "checkpoint": deepcopy(row["checkpoint"]),
                },
            }
        )
        row.update(
            status="queued",
            lease_owner=None,
            lease_expires_at=None,
            error_code=None,
            error_message=None,
            completed_at=None,
            updated_at=datetime.now(UTC),
            checkpoint={**row["checkpoint"], "retry_attempt_base": row["attempt_count"]},
        )
        return row

    def finish_self_hosted_gateway_provision(
        self,
        operation_id: UUID,
        worker_id: str,
        spec: SelfHostedGatewayProvisionSpec,
        deployment: GatewayDeployment,
        inventory: GatewayDeploymentInventory,
        checkpoint: Mapping[str, Any],
        *,
        enqueue_inventory_sync: bool = False,
    ) -> None:
        row = next(
            (row for row in self.gateway_release_operations if row["id"] == operation_id), None
        )
        if (
            row is None
            or row["operation_kind"] != "self_hosted_gateway_provision"
            or row["status"] != "verifying_readback"
            or row["lease_owner"] != worker_id
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= datetime.now(UTC)
            or row["confirmation_sha256"] != gateway_provision_digest(spec)
            or SelfHostedGatewayProvisionSpec.model_validate(row["semantic_preview"]) != spec
        ):
            raise ValueError("Gateway creation lease or intent changed")
        validate_gateway_provision_target(spec, self.apim_targets[spec.target.gateway_profile_id])
        if not any(
            profile["id"] == spec.target.gateway_profile_id
            and profile["enabled"]
            and profile["implementation"] == "apim"
            for profile in self.gateways
        ):
            raise ValueError("Gateway creation target is unavailable")
        if (
            deployment.id != spec.deployment_id
            or inventory.deployment_id != spec.deployment_id
            or deployment.apim_target_id != spec.target.gateway_profile_id
            or deployment.native_gateway_id != spec.native_gateway_id
            or deployment.endpoint_url != spec.configuration.endpoint_url
            or deployment.last_probe_at is not None
            or deployment.reachability_state != "unknown"
        ):
            raise ValueError("Gateway creation materialization is invalid")
        existing = self.gateway_deployments.get(spec.deployment_id)
        if existing and (
            existing["retired_at"] is not None
            or existing["endpoint_url"] is not None
            or existing["apim_target_id"] != spec.target.gateway_profile_id
            or existing["native_gateway_id"] != spec.native_gateway_id
        ):
            raise ValueError("Existing Gateway cannot be overwritten or resurrected")
        prior_deployment = deepcopy(existing)
        prior_inventory = deepcopy(self.gateway_deployment_inventories.get(spec.deployment_id))
        try:
            if prior_inventory and prior_inventory["synced_at"] > inventory.synced_at:
                raise ValueError("Gateway inventory changed during creation")
            self.upsert_gateway_deployment(
                deployment.model_copy(
                    update={"provisioning_operation_id": operation_id},
                )
            )
            self.upsert_gateway_deployment_inventory(inventory)
            if row["lease_owner"] != worker_id or row["lease_expires_at"] <= datetime.now(UTC):
                raise ValueError("Gateway creation lease expired before completion")
        except Exception:
            if prior_deployment is None:
                self.gateway_deployments.pop(spec.deployment_id, None)
            else:
                self.gateway_deployments[spec.deployment_id] = prior_deployment
            if prior_inventory is None:
                self.gateway_deployment_inventories.pop(spec.deployment_id, None)
            else:
                self.gateway_deployment_inventories[spec.deployment_id] = prior_inventory
            raise
        row.update(
            status="succeeded",
            checkpoint=dict(checkpoint),
            error_code=None,
            error_message=None,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "from_status": "verifying_readback",
                "to_status": "succeeded",
                "actor": worker_id,
                "detail": {"checkpoint": dict(checkpoint)},
                "created_at": datetime.now(UTC),
            }
        )
        if enqueue_inventory_sync:
            now = datetime.now(UTC)
            synchronization = {
                **deepcopy(row),
                "id": uuid4(),
                "operation_kind": "target_inventory_sync",
                "status": "queued",
                "confirmation_sha256": None,
                "semantic_preview": {"source_provision_operation_id": str(operation_id)},
                "checkpoint": {},
                "created_by": worker_id,
                "attempt_count": 0,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
            }
            self.gateway_release_operations.append(synchronization)
            self.gateway_release_operation_audit.append(
                {
                    "id": uuid4(),
                    "operation_id": synchronization["id"],
                    "from_status": None,
                    "to_status": "queued",
                    "actor": worker_id,
                    "created_at": now,
                    "detail": {"source_provision_operation_id": str(operation_id)},
                }
            )

    def create_apim_onboarding_operation(
        self,
        plan: ApimOnboardingPlan,
        credential_ciphertext: bytes,
        actor: str,
    ) -> dict[str, Any]:
        if not plan.ready or plan.blockers or not credential_ciphertext:
            raise ValueError("APIM initialization prerequisites are incomplete")
        profile_id = plan.gateway_profile_id
        existing = next(
            (
                row
                for row in reversed(self.gateway_release_operations)
                if row["gateway_profile_id"] == profile_id
                and row["operation_kind"] == "apim_onboarding"
            ),
            None,
        )
        if existing is not None:
            if existing["confirmation_sha256"] != plan.confirmation_sha256:
                raise ValueError("The APIM initialization plan has changed")
            return deepcopy(existing)
        resource_id = plan.request.resource_id.casefold()
        if any(
            str(row["arm_resource_id"]).casefold() == resource_id
            for row in self.apim_targets.values()
        ):
            raise ValueError("This APIM instance is already registered")
        name = plan.request.name or f"{plan.service_name} ({profile_id.hex[:8]})"
        if any(row["id"] == profile_id or row["name"] == name for row in self.gateways):
            raise ValueError("A Gateway profile already uses this identity or name")
        now = datetime.now(UTC)
        operation_id = uuid4()
        profile = {
            "id": profile_id,
            "name": name,
            "implementation": "apim",
            "base_url": None,
            "auth_type": "api_key",
            "credential_ciphertext": None,
            "credential_hint": None,
            "enabled": False,
            "is_default": False,
            "created_at": now,
            "updated_at": now,
            "config": {
                "header_name": "Ocp-Apim-Subscription-Key",
                "onboarding_resource_id": resource_id,
                "onboarding_tenant_id": str(plan.request.tenant_id),
            },
        }
        operation = {
            "id": operation_id,
            "gateway_profile_id": profile_id,
            "operation_kind": "apim_onboarding",
            "target_release_id": None,
            "prior_release_id": None,
            "status": "queued",
            "confirmation_sha256": plan.confirmation_sha256,
            "semantic_preview": plan.model_dump(mode="json"),
            "checkpoint": {},
            "error_code": None,
            "error_message": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "attempt_count": 0,
            "created_by": actor,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        self.gateways.append(profile)
        self.gateway_release_operations.append(operation)
        self.gateway_release_operation_secrets[operation_id] = credential_ciphertext
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "from_status": None,
                "to_status": "queued",
                "actor": actor,
                "detail": {},
                "created_at": now,
            }
        )
        return deepcopy(operation)

    def list_apim_onboarding_operations(self) -> Sequence[dict[str, Any]]:
        latest: dict[UUID, dict[str, Any]] = {}
        for row in self.gateway_release_operations:
            if row["operation_kind"] == "apim_onboarding":
                latest[row["gateway_profile_id"]] = row
        return deepcopy(list(latest.values()))

    def retry_apim_onboarding_operation(self, operation_id: UUID, actor: str) -> dict[str, Any]:
        prior = next(
            (row for row in self.gateway_release_operations if row["id"] == operation_id), None
        )
        if prior is None or prior["operation_kind"] != "apim_onboarding":
            raise ValueError("APIM initialization operation was not found")
        current = next(
            row
            for row in reversed(self.gateway_release_operations)
            if row["gateway_profile_id"] == prior["gateway_profile_id"]
        )
        if current["id"] != operation_id or prior["status"] != "failed":
            raise ValueError("Only the latest failed APIM initialization can be retried")
        secret = self.gateway_release_operation_secrets.get(operation_id)
        if secret is None:
            raise ValueError("APIM initialization credentials are unavailable")
        now = datetime.now(UTC)
        operation = {
            **deepcopy(prior),
            "id": uuid4(),
            "status": "queued",
            "created_by": actor,
            "attempt_count": 0,
            "lease_owner": None,
            "lease_expires_at": None,
            "error_code": None,
            "error_message": None,
            "started_at": None,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
            "checkpoint": {
                "retry_of": str(operation_id),
                **{key: deepcopy(prior["checkpoint"][key]) for key in (
                    "fixed_program_requests", "fixed_route_initialization",
                ) if key in prior["checkpoint"]},
            },
        }
        self.gateway_release_operations.append(operation)
        self.gateway_release_operation_secrets[operation["id"]] = secret
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation["id"],
                "from_status": None,
                "to_status": "queued",
                "actor": actor,
                "detail": {"retry_of": str(operation_id)},
                "created_at": now,
            }
        )
        return deepcopy(operation)

    def finish_apim_onboarding_operation(
        self,
        operation_id: UUID,
        worker_id: str,
        credential_ciphertext: bytes,
        credential_hint: str,
        configuration_sha256: str,
        fixed_publication: GatewayPublication | None = None,
    ) -> dict[str, Any] | None:
        operation = next(
            (row for row in self.gateway_release_operations if row["id"] == operation_id), None
        )
        now = datetime.now(UTC)
        if (
            operation is None
            or operation["operation_kind"] != "apim_onboarding"
            or operation["status"] != "preflight_probing"
            or operation["lease_owner"] != worker_id
            or operation["lease_expires_at"] is None
            or operation["lease_expires_at"] <= now
        ):
            return None
        plan = ApimOnboardingPlan.model_validate(operation["semantic_preview"])
        if fixed_publication is None:
            raise ValueError("APIM initialization fixed program is missing")
        fixed_publication.require_onboarding_installation(
            plan.gateway_profile_id, operation_id, plan.fixed_program_sha256,
        )
        if any(row["gateway_profile_id"] == plan.gateway_profile_id
               for row in self.gateway_publications):
            raise ValueError("Fixed installation cannot replace an existing publication")
        profile = next((row for row in self.gateways if row["id"] == plan.gateway_profile_id), None)
        target = self.apim_targets.get(plan.gateway_profile_id)
        verified = any(
            row["apim_target_id"] == plan.gateway_profile_id
            and row["native_gateway_id"] == "managed"
            and row["reachability_state"] == "reachable"
            for row in self.gateway_deployments.values()
        )
        if (
            profile is None
            or profile["enabled"]
            or profile["is_default"]
            or target is None
            or str(target["arm_resource_id"]).casefold() != plan.request.resource_id.casefold()
            or profile["config"].get("onboarding_resource_id")
            != plan.request.resource_id.casefold()
            or not verified
        ):
            raise ValueError("Cannot activate an unverified or changed APIM profile")
        profile.update(
            enabled=True,
            base_url=str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path,
            credential_ciphertext=credential_ciphertext,
            credential_hint=credential_hint,
            updated_at=now,
        )
        profile["config"].update(
            {
                "onboarding_ready": True,
                "onboarding_request": plan.request.model_dump(mode="json"),
                "onboarding_configuration_sha256": configuration_sha256,
            }
        )
        self.gateway_publications.append({
            **fixed_publication.model_dump(mode="python"),
            "desired_spec": fixed_publication.desired_spec.model_dump(mode="json"),
        })
        self.effective_gateway_releases[plan.gateway_profile_id] = fixed_publication.id
        self.gateway_publication_audit.append({
            "id": uuid4(), "publication_id": fixed_publication.id,
            "from_status": None, "to_status": "active", "actor": fixed_publication.created_by,
            "detail": {"kind": "fixed_program_installation",
                       "program_sha256": fixed_publication.policy_sha256},
            "created_at": now,
        })
        checkpoint = {
            **operation["checkpoint"],
            "step": "activate_target",
            "completed_steps": plan.steps,
            "managed_gateway_verified": True,
            "fixed_program_publication_id": str(fixed_publication.id),
            "fixed_program_sha256": fixed_publication.policy_sha256,
        }
        operation.update(
            status="succeeded",
            checkpoint=checkpoint,
            error_code=None,
            error_message=None,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "from_status": "preflight_probing",
                "to_status": "succeeded",
                "actor": worker_id,
                "detail": checkpoint,
                "created_at": now,
            }
        )
        for row in self.gateway_release_operations:
            if (
                row["gateway_profile_id"] == plan.gateway_profile_id
                and row["operation_kind"] == "apim_onboarding"
            ):
                self.gateway_release_operation_secrets.pop(row["id"], None)
        return deepcopy(operation)

    def upsert_apim_target(self, target: ApimTarget) -> dict[str, Any]:
        gateway = next(
            (
                row
                for row in self.gateways
                if row["id"] == target.gateway_profile_id
            ),
            None,
        )
        if gateway is None or gateway["implementation"] != "apim":
            raise ValueError("APIM target requires an existing APIM gateway profile")
        now = datetime.now(UTC)
        values = target.model_dump()
        values["probe_secret_ref"] = (
            str(target.probe_secret_ref) if target.probe_secret_ref is not None else None
        )
        values["capabilities"] = target.capabilities.model_dump(mode="json")
        existing = self.apim_targets.get(target.gateway_profile_id)
        if existing is not None:
            existing_identity = (
                str(existing["arm_resource_id"]).casefold(),
                existing["tenant_id"],
                existing["subscription_id"],
                str(existing["resource_group"]).casefold(),
                str(existing["service_name"]).casefold(),
                str(existing["api_id"]).casefold(),
                existing["api_path"],
            )
            incoming_identity = (
                target.arm_resource_id.casefold(),
                target.tenant_id,
                target.subscription_id,
                target.resource_group.casefold(),
                target.service_name.casefold(),
                target.api_id.casefold(),
                target.api_path,
            )
            if existing_identity != incoming_identity:
                raise ValueError("APIM target identity cannot be changed in place")
            created_at = existing["created_at"]
        else:
            duplicate = next(
                (
                    row
                    for row in self.apim_targets.values()
                    if str(row["arm_resource_id"]).casefold()
                    == target.arm_resource_id.casefold()
                    or (
                        row["tenant_id"] == target.tenant_id
                        and row["subscription_id"] == target.subscription_id
                        and str(row["resource_group"]).casefold()
                        == target.resource_group.casefold()
                        and str(row["service_name"]).casefold()
                        == target.service_name.casefold()
                    )
                ),
                None,
            )
            if duplicate is not None:
                raise ValueError("APIM target service identity already exists")
            created_at = now
        row = {**values, "created_at": created_at, "updated_at": now}
        self.apim_targets[target.gateway_profile_id] = row
        return deepcopy(row)

    def get_apim_target(self, gateway_profile_id: UUID) -> dict[str, Any] | None:
        row = self.apim_targets.get(gateway_profile_id)
        return deepcopy(row) if row is not None else None

    def list_apim_targets(self) -> Sequence[dict[str, Any]]:
        return [
            deepcopy(row)
            for row in sorted(
                self.apim_targets.values(),
                key=lambda item: (str(item["service_name"]), str(item["gateway_profile_id"])),
            )
        ]

    def upsert_gateway_deployment(
        self, deployment: GatewayDeployment, *, preserve_display_name: bool = False,
        retiring_deployment: GatewayDeployment | None = None,
    ) -> dict[str, Any]:
        if deployment.apim_target_id not in self.apim_targets:
            raise ValueError("Unknown APIM target")
        retiring = retiring_deployment or (
            deployment if deployment.kind == "self_hosted"
            and deployment.retired_at is not None and preserve_display_name else None
        )
        if retiring is not None:
            original = self.gateway_deployments.get(retiring.id)
            if original is None or (
                retiring_deployment is not None and original["retired_at"] is not None
            ):
                raise ValueError("The Self-hosted Gateway changed before removal")
            if original["retired_at"] is None:
                if any(
                    row["gateway_profile_id"] == retiring.apim_target_id
                    and row["status"] not in {"active", "failed", "superseded", "rolled_back"}
                    for row in self.gateway_publications
                ):
                    raise ValueError(
                        "Wait for the APIM publication to finish before changing this Gateway"
                    )
                bindings = {
                    row["logical_model_id"]: row for row in self.apim_target_route_bindings.values()
                    if row["apim_target_id"] == retiring.apim_target_id
                }
                runtime_ids = {row["id"] for row in self.runtimes
                               if row.get("gateway_profile_id") == retiring.apim_target_id}
                for model in self.models:
                    binding = bindings.get(model["id"])
                    if binding is None:
                        referenced = model["runtime_id"] in runtime_ids
                    else:
                        scope = binding["deployment_scope"]
                        referenced = binding["enabled"] and (
                            scope["mode"] == "all" or retiring.id in {
                                UUID(str(value)) for value in scope["deployment_ids"]
                            }
                        )
                    if referenced:
                        raise ValueError("Remove or move the Gateway's published models first")
            if retiring_deployment is not None:
                destination = self.gateway_deployments.get(deployment.id)
                if (
                    destination and destination["retired_at"] is None
                    and destination["endpoint_url"]
                ):
                    raise ValueError("The destination Gateway is already associated")
        duplicate = next(
            (
                row
                for row in self.gateway_deployments.values()
                if row["id"] != deployment.id
                and row["apim_target_id"] == deployment.apim_target_id
                and str(row["native_gateway_id"]).casefold()
                == deployment.native_gateway_id.casefold()
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError("Gateway deployment native identity already exists")
        now = datetime.now(UTC)
        values = deployment.model_dump()
        values["endpoint_url"] = (
            str(deployment.endpoint_url) if deployment.endpoint_url is not None else None
        )
        existing = self.gateway_deployments.get(deployment.id)
        if existing is not None:
            values["provisioning_operation_id"] = (
                existing.get("provisioning_operation_id") or values.get("provisioning_operation_id")
            )
            identity = (
                existing["apim_target_id"],
                str(existing["native_gateway_id"]).casefold(),
                existing["kind"],
            )
            incoming = (
                deployment.apim_target_id,
                deployment.native_gateway_id.casefold(),
                deployment.kind,
            )
            if identity != incoming:
                raise ValueError("Gateway deployment identity cannot be changed in place")
            if preserve_display_name:
                values["display_name"] = existing["display_name"]
                values["retired_at"] = existing["retired_at"] or values["retired_at"]
            created_at = existing["created_at"]
        else:
            created_at = now
        row = {**values, "created_at": created_at, "updated_at": now}
        self.gateway_deployments[deployment.id] = row
        if retiring_deployment is not None:
            self.gateway_deployments[retiring_deployment.id].update(retired_at=now, updated_at=now)
        return deepcopy(row)

    def rename_gateway_deployment(self, deployment_id: UUID, display_name: str) -> bool:
        row = self.gateway_deployments.get(deployment_id)
        if row is None or row["kind"] != "self_hosted" or row["retired_at"] is not None:
            return False
        if not any(
            profile["id"] == row["apim_target_id"]
            and profile["implementation"] == "apim" and profile["enabled"]
            for profile in self.gateways
        ):
            return False
        row.update(display_name=display_name, updated_at=datetime.now(UTC))
        return True

    def get_gateway_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        row = self.gateway_deployments.get(deployment_id)
        return deepcopy(row) if row is not None else None

    def list_gateway_deployments(
        self, apim_target_id: UUID, *, include_retired: bool = False
    ) -> Sequence[dict[str, Any]]:
        return self.list_gateway_deployments_for_targets(
            [apim_target_id], include_retired=include_retired
        )

    def list_gateway_deployments_for_targets(
        self, apim_target_ids: Sequence[UUID], *, include_retired: bool = False
    ) -> Sequence[dict[str, Any]]:
        selected_ids = set(apim_target_ids)
        rows = (
            row
            for row in self.gateway_deployments.values()
            if row["apim_target_id"] in selected_ids
            and (include_retired or row["retired_at"] is None)
        )
        return [
            deepcopy(row)
            for row in sorted(
                rows,
                key=lambda item: (
                    str(item["apim_target_id"]),
                    str(item["kind"]),
                    str(item["display_name"]),
                    str(item["id"]),
                ),
            )
        ]

    def upsert_gateway_instance_observation(
        self, observation: GatewayInstanceObservation
    ) -> dict[str, Any]:
        if observation.deployment_id not in self.gateway_deployments:
            raise ValueError("Unknown Gateway deployment")
        key = (observation.deployment_id, observation.native_instance_id)
        incoming = observation.model_dump()
        existing = self.gateway_instance_observations.get(key)
        if existing is None:
            row = {**incoming, "updated_at": datetime.now(UTC)}
        else:
            incoming_is_latest = observation.last_seen_at >= existing["last_seen_at"]
            successful_at = existing["last_successful_request_at"]
            if observation.last_successful_request_at is not None and (
                successful_at is None
                or observation.last_successful_request_at > successful_at
            ):
                successful_at = observation.last_successful_request_at
            row = {
                **existing,
                "runtime_version": (
                    observation.runtime_version or existing["runtime_version"]
                    if incoming_is_latest
                    else existing["runtime_version"]
                ),
                "processor_count": (
                    observation.processor_count or existing["processor_count"]
                    if incoming_is_latest
                    else existing["processor_count"]
                ),
                "first_seen_at": min(
                    observation.first_seen_at, existing["first_seen_at"]
                ),
                "last_seen_at": max(observation.last_seen_at, existing["last_seen_at"]),
                "last_successful_request_at": successful_at,
                "source": observation.source if incoming_is_latest else existing["source"],
                "confidence": (
                    observation.confidence if incoming_is_latest else existing["confidence"]
                ),
                "updated_at": datetime.now(UTC),
            }
        self.gateway_instance_observations[key] = row
        return deepcopy(row)

    def list_gateway_instance_observations(
        self, deployment_id: UUID
    ) -> Sequence[dict[str, Any]]:
        rows = (
            row
            for (candidate_id, _), row in self.gateway_instance_observations.items()
            if candidate_id == deployment_id
        )
        return [
            deepcopy(row)
            for row in sorted(
                rows,
                key=lambda item: (
                    item["last_seen_at"],
                    str(item["native_instance_id"]),
                ),
                reverse=True,
            )
        ]

    def upsert_gateway_deployment_inventory(
        self, inventory: GatewayDeploymentInventory
    ) -> dict[str, Any]:
        if inventory.deployment_id not in self.gateway_deployments:
            raise ValueError("Unknown Gateway deployment")
        existing = self.gateway_deployment_inventories.get(
            inventory.deployment_id
        )
        if existing is not None and existing["synced_at"] > inventory.synced_at:
            return deepcopy(existing)
        row = {**inventory.model_dump(), "updated_at": datetime.now(UTC)}
        self.gateway_deployment_inventories[inventory.deployment_id] = row
        return deepcopy(row)

    def get_gateway_deployment_inventory(
        self, deployment_id: UUID
    ) -> dict[str, Any] | None:
        row = self.gateway_deployment_inventories.get(deployment_id)
        return deepcopy(row) if row is not None else None

    def list_gateway_deployment_inventories(
        self, deployment_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        return [
            deepcopy(self.gateway_deployment_inventories[deployment_id])
            for deployment_id in sorted(set(deployment_ids), key=str)
            if deployment_id in self.gateway_deployment_inventories
        ]

    def upsert_apim_target_route_binding(
        self, binding: ApimTargetRouteBinding
    ) -> dict[str, Any]:
        if binding.apim_target_id not in self.apim_targets:
            raise ValueError("Unknown APIM target")
        if not any(row["id"] == binding.logical_model_id for row in self.models):
            raise ValueError("Unknown logical model")
        runtime = next(
            (row for row in self.runtimes if row["id"] == binding.runtime_id),
            None,
        )
        if runtime is None or runtime.get("gateway_profile_id") != binding.apim_target_id:
            raise ValueError("Route Runtime does not belong to the APIM target")
        if binding.current_publication_id is not None and not any(
            row["id"] == binding.current_publication_id
            and row["gateway_profile_id"] == binding.apim_target_id
            for row in self.gateway_publications
        ):
            raise ValueError("Route publication does not belong to the APIM target")
        deployment_ids = binding.deployment_scope.deployment_ids
        if binding.deployment_scope.mode is GatewayDeploymentScopeMode.SELECTED:
            for deployment_id in deployment_ids:
                deployment = self.gateway_deployments.get(deployment_id)
                if (
                    deployment is None
                    or deployment["apim_target_id"] != binding.apim_target_id
                    or deployment["retired_at"] is not None
                ):
                    raise ValueError(
                        "Selected Gateway deployments must be active and belong to the APIM target"
                    )
        duplicate = next(
            (
                row
                for row in self.apim_target_route_bindings.values()
                if row["id"] != binding.id
                and row["logical_model_id"] == binding.logical_model_id
                and row["apim_target_id"] == binding.apim_target_id
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError("APIM target route already exists for this logical model")
        if binding.is_default_serving and any(
            row["id"] != binding.id
            and row["logical_model_id"] == binding.logical_model_id
            and row["is_default_serving"]
            for row in self.apim_target_route_bindings.values()
        ):
            raise ValueError("Logical model already has a default APIM target route")
        now = datetime.now(UTC)
        values = binding.model_dump()
        values["deployment_scope"] = {
            "mode": binding.deployment_scope.mode,
            "deployment_ids": tuple(deployment_ids),
        }
        existing = self.apim_target_route_bindings.get(binding.id)
        if existing is not None and (
            existing["logical_model_id"] != binding.logical_model_id
            or existing["apim_target_id"] != binding.apim_target_id
        ):
            raise ValueError("APIM target route identity cannot be changed in place")
        row = {
            **values,
            "created_at": existing["created_at"] if existing is not None else now,
            "updated_at": now,
        }
        self.apim_target_route_bindings[binding.id] = row
        return deepcopy(row)

    def get_apim_target_route_binding(
        self, binding_id: UUID
    ) -> dict[str, Any] | None:
        row = self.apim_target_route_bindings.get(binding_id)
        return deepcopy(row) if row is not None else None

    def list_apim_target_route_bindings(
        self,
        *,
        logical_model_id: UUID | None = None,
        apim_target_id: UUID | None = None,
        apim_target_ids: Sequence[UUID] | None = None,
    ) -> Sequence[dict[str, Any]]:
        selected_ids = set(apim_target_ids) if apim_target_ids is not None else None
        rows = (
            row
            for row in self.apim_target_route_bindings.values()
            if (logical_model_id is None or row["logical_model_id"] == logical_model_id)
            and (apim_target_id is None or row["apim_target_id"] == apim_target_id)
            and (selected_ids is None or row["apim_target_id"] in selected_ids)
        )
        return [
            deepcopy(row)
            for row in sorted(
                rows,
                key=lambda item: (
                    str(item["logical_model_id"]),
                    str(item["apim_target_id"]),
                    str(item["id"]),
                ),
            )
        ]

    def upsert_usage_gateway_attribution(
        self, attribution: UsageGatewayAttribution
    ) -> dict[str, Any]:
        if attribution.apim_target_id not in self.apim_targets:
            raise ValueError("Unknown APIM target")
        if not any(record.id == attribution.usage_id for record in self.usage_records):
            raise ValueError("Unknown canonical usage ID")
        if attribution.gateway_deployment_id is not None:
            deployment = self.gateway_deployments.get(
                attribution.gateway_deployment_id
            )
            if (
                deployment is None
                or deployment["apim_target_id"] != attribution.apim_target_id
            ):
                raise ValueError(
                    "Usage Gateway deployment does not belong to the APIM target"
                )
            if (
                str(deployment["native_gateway_id"]).casefold()
                != attribution.native_gateway_id.casefold()
                or (deployment["kind"] == "managed") != attribution.is_managed
            ):
                raise ValueError(
                    "Usage Gateway attribution conflicts with the deployment identity"
                )
        now = datetime.now(UTC)
        incoming = attribution.model_dump()
        existing = self.usage_gateway_attributions.get(attribution.usage_id)
        if existing is None:
            row = {**incoming, "created_at": now, "updated_at": now}
        else:
            identity_matches = (
                existing["apim_target_id"] == attribution.apim_target_id
                and str(existing["native_gateway_id"]).casefold()
                == attribution.native_gateway_id.casefold()
                and existing["is_managed"] == attribution.is_managed
                and str(existing["apim_service_id"]).casefold()
                == attribution.apim_service_id.casefold()
                and str(existing["apim_region"]).casefold()
                == attribution.apim_region.casefold()
                and existing["api_revision"] == attribution.api_revision
                and (
                    existing["gateway_deployment_id"] is None
                    or attribution.gateway_deployment_id is None
                    or existing["gateway_deployment_id"]
                    == attribution.gateway_deployment_id
                )
                and (
                    existing["native_instance_id"] is None
                    or attribution.native_instance_id is None
                    or str(existing["native_instance_id"]).casefold()
                    == attribution.native_instance_id.casefold()
                )
            )
            if not identity_matches:
                raise ValueError("Conflicting Gateway attribution for canonical usage ID")
            source_rank = {
                "backfill": 1,
                "reconciliation": 2,
                "eventhub": 3,
                "observer": 4,
            }
            incoming_source = attribution.attribution_source.value
            existing_source = str(existing["attribution_source"])
            row = {
                **existing,
                "gateway_deployment_id": (
                    existing["gateway_deployment_id"]
                    or attribution.gateway_deployment_id
                ),
                "native_instance_id": (
                    existing["native_instance_id"] or attribution.native_instance_id
                ),
                "event_schema_version": max(
                    existing["event_schema_version"],
                    attribution.event_schema_version,
                ),
                "attribution_source": (
                    incoming_source
                    if source_rank[incoming_source] >= source_rank[existing_source]
                    else existing_source
                ),
                "observed_at": max(existing["observed_at"], attribution.observed_at),
                "updated_at": now,
            }
        self.usage_gateway_attributions[attribution.usage_id] = row
        return deepcopy(row)

    def get_usage_gateway_attribution(self, usage_id: str) -> dict[str, Any] | None:
        row = self.usage_gateway_attributions.get(usage_id)
        return deepcopy(row) if row is not None else None
