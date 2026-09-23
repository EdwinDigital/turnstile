from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ..domain.gateway_targets import (
    ApimTarget,
    ApimTargetRouteBinding,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayInstanceObservation,
)


class InMemoryGatewayTargetRepositoryMixin:
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
