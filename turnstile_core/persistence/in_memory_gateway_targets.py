from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from ..domain.gateway_targets import (
    ApimOnboardingPlan,
    ApimTarget,
    ApimTargetRouteBinding,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayInstanceObservation,
)


class InMemoryGatewayTargetRepositoryMixin:
    gateway_release_operations: list[dict[str, Any]]
    gateway_release_operation_secrets: dict[UUID, bytes]
    gateway_release_operation_audit: list[dict[str, Any]]

    def create_apim_onboarding_operation(
        self, plan: ApimOnboardingPlan, credential_ciphertext: bytes, actor: str,
    ) -> dict[str, Any]:
        if not plan.ready or plan.blockers or not credential_ciphertext:
            raise ValueError("APIM initialization prerequisites are incomplete")
        existing = next((row for row in reversed(self.gateway_release_operations)
                         if row["gateway_profile_id"] == plan.gateway_profile_id
                         and row["operation_kind"] == "apim_onboarding"), None)
        if existing is not None:
            if existing["confirmation_sha256"] != plan.confirmation_sha256:
                raise ValueError("The APIM initialization plan has changed")
            return deepcopy(existing)
        resource_id = plan.request.resource_id.casefold()
        if any(str(row["arm_resource_id"]).casefold() == resource_id
               for row in self.apim_targets.values()):
            raise ValueError("This APIM instance is already registered")
        name = plan.request.name or f"{plan.service_name} ({plan.gateway_profile_id.hex[:8]})"
        occupied = any(row["id"] == plan.gateway_profile_id or row["name"] == name
                       for row in self.gateways)
        if occupied:
            raise ValueError("A Gateway profile already uses this identity or name")
        now = datetime.now(UTC)
        self.gateways.append({
            "id": plan.gateway_profile_id, "name": name, "implementation": "apim",
            "base_url": None, "auth_type": "api_key", "credential_ciphertext": None,
            "credential_hint": None, "enabled": False, "is_default": False,
            "created_at": now, "updated_at": now,
            "config": {"header_name": "Ocp-Apim-Subscription-Key",
                       "onboarding_resource_id": resource_id,
                       "onboarding_tenant_id": str(plan.request.tenant_id)},
        })
        operation: dict[str, Any] = {
            "id": uuid4(), "gateway_profile_id": plan.gateway_profile_id,
            "operation_kind": "apim_onboarding", "target_release_id": None,
            "prior_release_id": None, "status": "queued",
            "confirmation_sha256": plan.confirmation_sha256,
            "semantic_preview": plan.model_dump(mode="json"), "checkpoint": {},
            "error_code": None, "error_message": None, "lease_owner": None,
            "lease_expires_at": None, "attempt_count": 0, "created_by": actor,
            "created_at": now, "started_at": None, "completed_at": None, "updated_at": now,
        }
        self.gateway_release_operations.append(operation)
        self.gateway_release_operation_secrets[operation["id"]] = credential_ciphertext
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation["id"], "from_status": None,
            "to_status": "queued", "actor": actor, "detail": {}, "created_at": now,
        })
        return deepcopy(operation)

    def list_apim_onboarding_operations(self) -> Sequence[dict[str, Any]]:
        latest = {row["gateway_profile_id"]: row for row in self.gateway_release_operations
                  if row["operation_kind"] == "apim_onboarding"}
        return deepcopy(list(latest.values()))

    def retry_apim_onboarding_operation(self, operation_id: UUID, actor: str) -> dict[str, Any]:
        prior = next((row for row in self.gateway_release_operations
                      if row["id"] == operation_id), None)
        if prior is None or prior["operation_kind"] != "apim_onboarding":
            raise ValueError("APIM initialization operation was not found")
        current = next(row for row in reversed(self.gateway_release_operations)
                       if row["gateway_profile_id"] == prior["gateway_profile_id"])
        if current["id"] != operation_id or prior["status"] != "failed":
            raise ValueError("Only the latest failed APIM initialization can be retried")
        secret = self.gateway_release_operation_secrets.get(operation_id)
        if secret is None:
            raise ValueError("APIM initialization credentials are unavailable")
        now = datetime.now(UTC)
        operation = {
            **deepcopy(prior), "id": uuid4(), "status": "queued", "created_by": actor,
            "attempt_count": 0, "lease_owner": None, "lease_expires_at": None,
            "error_code": None, "error_message": None, "started_at": None,
            "completed_at": None, "created_at": now, "updated_at": now,
            "checkpoint": {"retry_of": str(operation_id)},
        }
        self.gateway_release_operations.append(operation)
        self.gateway_release_operation_secrets[operation["id"]] = secret
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation["id"], "from_status": None,
            "to_status": "queued", "actor": actor, "detail": {"retry_of": str(operation_id)},
            "created_at": now,
        })
        return deepcopy(operation)

    def retry_self_hosted_gateway_provision(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        prior = next((row for row in self.gateway_release_operations
                      if row["id"] == operation_id), None)
        if prior is None or prior["operation_kind"] != "self_hosted_gateway_provision":
            raise ValueError("Gateway creation operation was not found")
        if prior["status"] in {"queued", "validating_dependencies", "verifying_readback"}:
            return deepcopy(prior)
        if prior["status"] != "failed":
            raise ValueError("Only failed Gateway creation can be retried")
        if any(row["gateway_profile_id"] == prior["gateway_profile_id"]
               and row["id"] != operation_id
               and row["status"] not in {"succeeded", "failed", "restored"}
               for row in self.gateway_release_operations):
            raise ValueError("Another Gateway operation is already in progress")
        now = datetime.now(UTC)
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation_id, "from_status": "failed",
            "to_status": "queued", "actor": actor, "created_at": now,
            "detail": {"retry": True, "error_code": prior["error_code"],
                       "error_message": prior["error_message"],
                       "attempt_count": prior["attempt_count"]},
        })
        prior.update({
            "status": "queued", "attempt_count": 0, "lease_owner": None,
            "lease_expires_at": None, "error_code": None, "error_message": None,
            "started_at": None, "completed_at": None, "updated_at": now,
        })
        return deepcopy(prior)

    def finish_apim_onboarding_operation(
        self, operation_id: UUID, worker_id: str, credential_ciphertext: bytes,
        credential_hint: str, configuration_sha256: str,
    ) -> dict[str, Any] | None:
        operation = next((row for row in self.gateway_release_operations
                          if row["id"] == operation_id), None)
        now = datetime.now(UTC)
        if (
            operation is None or operation["operation_kind"] != "apim_onboarding"
            or operation["status"] != "preflight_probing" or operation["lease_owner"] != worker_id
            or operation["lease_expires_at"] is None or operation["lease_expires_at"] <= now
        ):
            return None
        plan = ApimOnboardingPlan.model_validate(operation["semantic_preview"])
        profile = next((row for row in self.gateways if row["id"] == plan.gateway_profile_id), None)
        target = self.apim_targets.get(plan.gateway_profile_id)
        verified = any(row["apim_target_id"] == plan.gateway_profile_id
                       and row["native_gateway_id"] == "managed"
                       and row["reachability_state"] == "reachable"
                       for row in self.gateway_deployments.values())
        resource_id = plan.request.resource_id.casefold()
        if (
            profile is None or profile["enabled"] or profile["is_default"] or target is None
            or str(target["arm_resource_id"]).casefold() != resource_id
            or profile["config"].get("onboarding_resource_id") != resource_id
            or not verified
        ):
            raise ValueError("Cannot activate an unverified or changed APIM profile")
        profile.update(
            enabled=True,
            base_url=str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path,
            credential_ciphertext=credential_ciphertext, credential_hint=credential_hint,
            updated_at=now,
        )
        profile["config"].update({
            "onboarding_ready": True, "onboarding_request": plan.request.model_dump(mode="json"),
            "onboarding_configuration_sha256": configuration_sha256,
        })
        checkpoint = {
            **operation["checkpoint"], "step": "activate_target",
            "completed_steps": plan.steps, "managed_gateway_verified": True,
        }
        operation.update(
            status="succeeded", checkpoint=checkpoint, error_code=None, error_message=None,
            lease_owner=None, lease_expires_at=None, completed_at=now, updated_at=now,
        )
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation_id, "from_status": "preflight_probing",
            "to_status": "succeeded", "actor": worker_id, "detail": checkpoint, "created_at": now,
        })
        for row in self.gateway_release_operations:
            same_target = row["gateway_profile_id"] == plan.gateway_profile_id
            if same_target and row["operation_kind"] == "apim_onboarding":
                self.gateway_release_operation_secrets.pop(row["id"], None)
        return deepcopy(operation)

    def enroll_apim_onboarding_budget_target(
        self, operation_id: UUID, worker_id: str,
    ) -> dict[str, Any]:
        raise RuntimeError("APIM budget enrollment requires a durable PostgreSQL repository")

    gateways: list[dict[str, Any]]
    apim_targets: dict[UUID, dict[str, Any]]
    gateway_deployments: dict[UUID, dict[str, Any]]
    apim_target_route_bindings: dict[UUID, dict[str, Any]]
    models: list[dict[str, Any]]
    runtimes: list[dict[str, Any]]
    gateway_publications: list[dict[str, Any]]
    gateway_deployment_inventories: dict[UUID, dict[str, Any]]
    gateway_instance_observations: dict[tuple[UUID, str], dict[str, Any]]

    def upsert_gateway_instance_observation(
        self,
        observation: GatewayInstanceObservation,
    ) -> dict[str, Any]:
        if observation.deployment_id not in self.gateway_deployments:
            raise ValueError("Unknown Gateway deployment")
        key = (observation.deployment_id, observation.native_instance_id)
        current = self.gateway_instance_observations.get(key)
        values = observation.model_dump()
        if current is not None:
            newer = observation.last_seen_at >= current["last_seen_at"]
            values = {
                **(values if newer else current),
                "first_seen_at": min(current["first_seen_at"], observation.first_seen_at),
                "last_seen_at": max(current["last_seen_at"], observation.last_seen_at),
                "last_successful_request_at": max(
                    (
                        value
                        for value in (
                            current["last_successful_request_at"],
                            observation.last_successful_request_at,
                        )
                        if value is not None
                    ),
                    default=None,
                ),
                "runtime_version": observation.runtime_version or current["runtime_version"]
                if newer
                else current["runtime_version"],
                "processor_count": observation.processor_count or current["processor_count"]
                if newer
                else current["processor_count"],
            }
        row = {**values, "updated_at": datetime.now(UTC)}
        self.gateway_instance_observations[key] = row
        return deepcopy(row)

    def list_gateway_instance_observations(
        self,
        deployment_id: UUID,
        *,
        limit: int = 100,
    ) -> Sequence[dict[str, Any]]:
        rows = [
            row
            for row in self.gateway_instance_observations.values()
            if row["deployment_id"] == deployment_id
        ]
        return deepcopy(
            sorted(
                rows,
                key=lambda row: (
                    -row["last_seen_at"].timestamp(),
                    row["native_instance_id"],
                ),
            )[: max(1, min(limit, 500))]
        )

    def upsert_gateway_deployment_inventory(
        self,
        inventory: GatewayDeploymentInventory,
    ) -> dict[str, Any]:
        if inventory.deployment_id not in self.gateway_deployments:
            raise ValueError("Unknown Gateway deployment")
        current = self.gateway_deployment_inventories.get(inventory.deployment_id)
        if current is not None and inventory.synced_at < current["synced_at"]:
            return deepcopy(current)
        row = {**inventory.model_dump(mode="python"), "updated_at": datetime.now(UTC)}
        self.gateway_deployment_inventories[inventory.deployment_id] = row
        return deepcopy(row)

    def get_gateway_deployment_inventory(self, deployment_id: UUID) -> dict[str, Any] | None:
        row = self.gateway_deployment_inventories.get(deployment_id)
        return deepcopy(row) if row is not None else None

    def list_gateway_deployment_inventories(
        self,
        deployment_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        return [
            deepcopy(self.gateway_deployment_inventories[identity])
            for identity in sorted(set(deployment_ids), key=str)
            if identity in self.gateway_deployment_inventories
        ]

    def _assert_gateway_retirement_allowed(self, deployment: GatewayDeployment) -> None:
        if any(
            row["gateway_profile_id"] == deployment.apim_target_id
            and row["status"] not in {"active", "failed", "superseded", "rolled_back"}
            for row in self.gateway_publications
        ):
            raise ValueError("Wait for the APIM publication to finish before changing this Gateway")
        for model in self.models:
            binding = next(
                (
                    row
                    for row in self.apim_target_route_bindings.values()
                    if row["logical_model_id"] == model["id"]
                    and row["apim_target_id"] == deployment.apim_target_id
                ),
                None,
            )
            runtime = next((row for row in self.runtimes if row["id"] == model["runtime_id"]), None)
            if binding is not None:
                scope = binding["deployment_scope"]
                used = binding["enabled"] and (
                    scope["mode"] == "all" or deployment.id in scope["deployment_ids"]
                )
            else:
                used = (
                    runtime is not None
                    and runtime.get("gateway_profile_id") == deployment.apim_target_id
                )
            if used:
                raise ValueError("Remove or move the Gateway's published models first")

    def upsert_gateway_deployment(
        self,
        deployment: GatewayDeployment,
        *,
        preserve_display_name: bool = False,
        retiring_deployment: GatewayDeployment | None = None,
    ) -> dict[str, Any]:
        if deployment.apim_target_id not in self.apim_targets:
            raise ValueError("Unknown APIM target")
        existing = self.gateway_deployments.get(deployment.id)
        if existing is not None and (
            existing["apim_target_id"] != deployment.apim_target_id
            or str(existing["native_gateway_id"]).casefold()
            != deployment.native_gateway_id.casefold()
            or existing["kind"] != deployment.kind
        ):
            raise ValueError("Gateway deployment identity cannot be changed in place")
        if any(
            row["id"] != deployment.id
            and row["apim_target_id"] == deployment.apim_target_id
            and str(row["native_gateway_id"]).casefold() == deployment.native_gateway_id.casefold()
            for row in self.gateway_deployments.values()
        ):
            raise ValueError("Gateway native identity already exists")
        retiring = retiring_deployment or (
            deployment
            if deployment.kind == "self_hosted"
            and deployment.retired_at is not None
            and preserve_display_name
            else None
        )
        if retiring is not None:
            current = self.gateway_deployments.get(retiring.id)
            if (
                current is None
                or current["apim_target_id"] != retiring.apim_target_id
                or (retiring_deployment is not None and current["retired_at"] is not None)
            ):
                raise ValueError("The Self-hosted Gateway changed before removal")
            if current["retired_at"] is None:
                self._assert_gateway_retirement_allowed(retiring)
            if (
                retiring_deployment is not None
                and existing is not None
                and existing["retired_at"] is None
                and existing["endpoint_url"] is not None
            ):
                raise ValueError("The destination Gateway is already associated")
        now = datetime.now(UTC)
        values = deployment.model_dump()
        values["endpoint_url"] = str(deployment.endpoint_url) if deployment.endpoint_url else None
        if existing is not None and existing.get("provisioning_operation_id"):
            values["provisioning_operation_id"] = existing["provisioning_operation_id"]
        if existing is not None and preserve_display_name:
            values["display_name"] = existing["display_name"]
            values["retired_at"] = existing["retired_at"] or values["retired_at"]
        row = {
            **values,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        self.gateway_deployments[deployment.id] = row
        if retiring_deployment is not None:
            previous = self.gateway_deployments[retiring_deployment.id]
            self.gateway_deployments[retiring_deployment.id] = {
                **previous,
                "retired_at": now,
                "updated_at": now,
            }
        return deepcopy(row)

    def rename_gateway_deployment(self, deployment_id: UUID, display_name: str) -> bool:
        row = self.gateway_deployments.get(deployment_id)
        if (
            row is None
            or row["kind"] != "self_hosted"
            or row["retired_at"] is not None
            or not any(
                profile["id"] == row["apim_target_id"]
                and profile["enabled"]
                and profile["implementation"] == "apim"
                for profile in self.gateways
            )
        ):
            return False
        self.gateway_deployments[deployment_id] = {
            **row,
            "display_name": display_name,
            "updated_at": datetime.now(UTC),
        }
        return True

    def get_gateway_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        row = self.gateway_deployments.get(deployment_id)
        return deepcopy(row) if row is not None else None

    def list_gateway_deployments(
        self,
        apim_target_id: UUID,
        *,
        include_retired: bool = False,
    ) -> Sequence[dict[str, Any]]:
        return self.list_gateway_deployments_for_targets(
            [apim_target_id], include_retired=include_retired
        )

    def list_gateway_deployments_for_targets(
        self,
        apim_target_ids: Sequence[UUID],
        *,
        include_retired: bool = False,
    ) -> Sequence[dict[str, Any]]:
        return [
            deepcopy(row)
            for row in sorted(
                self.gateway_deployments.values(),
                key=lambda row: (
                    str(row["apim_target_id"]),
                    row["kind"],
                    row["display_name"],
                    str(row["id"]),
                ),
            )
            if row["apim_target_id"] in apim_target_ids
            and (include_retired or row["retired_at"] is None)
        ]

    def upsert_apim_target_route_binding(self, binding: ApimTargetRouteBinding) -> dict[str, Any]:
        if binding.apim_target_id not in self.apim_targets:
            raise ValueError("Unknown APIM target")
        if not any(row["id"] == binding.logical_model_id for row in self.models):
            raise ValueError("Unknown logical model")
        if not any(
            row["id"] == binding.runtime_id
            and row.get("gateway_profile_id") == binding.apim_target_id
            for row in self.runtimes
        ):
            raise ValueError("Route Runtime does not belong to the APIM target")
        if binding.current_publication_id is not None and not any(
            row["id"] == binding.current_publication_id
            and row["gateway_profile_id"] == binding.apim_target_id
            for row in self.gateway_publications
        ):
            raise ValueError("Route publication does not belong to the APIM target")
        for deployment_id in binding.deployment_scope.deployment_ids:
            deployment = self.gateway_deployments.get(deployment_id)
            if (
                deployment is None
                or deployment["apim_target_id"] != binding.apim_target_id
                or deployment["retired_at"] is not None
            ):
                raise ValueError(
                    "Selected deployments must be active and belong to the APIM target"
                )
        for other in self.apim_target_route_bindings.values():
            if other["id"] == binding.id or other["logical_model_id"] != binding.logical_model_id:
                continue
            if other["apim_target_id"] == binding.apim_target_id:
                raise ValueError("APIM target route already exists for this logical model")
            if other["is_default_serving"] and binding.is_default_serving:
                raise ValueError("Logical model already has a default APIM target route")
        existing = self.apim_target_route_bindings.get(binding.id)
        if existing is not None and (
            existing["logical_model_id"] != binding.logical_model_id
            or existing["apim_target_id"] != binding.apim_target_id
        ):
            raise ValueError("APIM target route identity cannot be changed in place")
        now = datetime.now(UTC)
        row = {
            **binding.model_dump(),
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        self.apim_target_route_bindings[binding.id] = row
        return deepcopy(row)

    def get_apim_target_route_binding(self, binding_id: UUID) -> dict[str, Any] | None:
        row = self.apim_target_route_bindings.get(binding_id)
        return deepcopy(row) if row is not None else None

    def list_apim_target_route_bindings(
        self,
        *,
        logical_model_id: UUID | None = None,
        apim_target_id: UUID | None = None,
        apim_target_ids: Sequence[UUID] | None = None,
    ) -> Sequence[dict[str, Any]]:
        return [
            deepcopy(row)
            for row in sorted(
                self.apim_target_route_bindings.values(),
                key=lambda row: (
                    str(row["logical_model_id"]),
                    str(row["apim_target_id"]),
                    str(row["id"]),
                ),
            )
            if (logical_model_id is None or row["logical_model_id"] == logical_model_id)
            and (apim_target_id is None or row["apim_target_id"] == apim_target_id)
            and (apim_target_ids is None or row["apim_target_id"] in apim_target_ids)
        ]

    def upsert_apim_target(self, target: ApimTarget) -> dict[str, Any]:
        gateway = next(
            (row for row in self.gateways if row["id"] == target.gateway_profile_id), None
        )
        if gateway is None or gateway["implementation"] != "apim":
            raise ValueError("APIM target requires an existing APIM gateway profile")
        now = datetime.now(UTC)
        values = target.model_dump()
        values["probe_secret_ref"] = (
            str(target.probe_secret_ref) if target.probe_secret_ref else None
        )
        values["capabilities"] = target.capabilities.model_dump(mode="json")
        existing = self.apim_targets.get(target.gateway_profile_id)
        if existing is not None:
            fields = ("arm_resource_id", "resource_group", "service_name", "api_id")
            if any(
                str(existing[name]).casefold() != str(values[name]).casefold() for name in fields
            ) or any(
                existing[name] != values[name]
                for name in (
                    "tenant_id",
                    "subscription_id",
                    "api_path",
                )
            ):
                raise ValueError("APIM target identity cannot be changed in place")
        elif any(
            str(row["arm_resource_id"]).casefold() == target.arm_resource_id.casefold()
            or (
                row["tenant_id"] == target.tenant_id
                and row["subscription_id"] == target.subscription_id
                and str(row["resource_group"]).casefold() == target.resource_group.casefold()
                and str(row["service_name"]).casefold() == target.service_name.casefold()
            )
            for row in self.apim_targets.values()
        ):
            raise ValueError("APIM target service identity already exists")
        row = {
            **values,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
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
                key=lambda item: (
                    str(item["service_name"]),
                    str(item["gateway_profile_id"]),
                ),
            )
        ]
