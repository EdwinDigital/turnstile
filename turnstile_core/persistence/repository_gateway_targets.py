from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from ..domain.gateway_targets import (
    ApimTarget,
    ApimTargetRouteBinding,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayInstanceObservation,
)


class PostgreSqlGatewayTargetRepositoryMixin:
    def upsert_gateway_instance_observation(
        self,
        observation: GatewayInstanceObservation,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO gateway_instance_observation AS existing (
                   schema_version, deployment_id, native_instance_id, runtime_version,
                   processor_count, first_seen_at, last_seen_at, last_successful_request_at,
                   source, confidence
                   ) VALUES (%(schema_version)s, %(deployment_id)s, %(native_instance_id)s,
                   %(runtime_version)s, %(processor_count)s, %(first_seen_at)s, %(last_seen_at)s,
                   %(last_successful_request_at)s, %(source)s, %(confidence)s)
                   ON CONFLICT (deployment_id, native_instance_id) DO UPDATE SET
                   first_seen_at = LEAST(existing.first_seen_at, EXCLUDED.first_seen_at),
                   last_seen_at = GREATEST(existing.last_seen_at, EXCLUDED.last_seen_at),
                   last_successful_request_at = GREATEST(existing.last_successful_request_at,
                                                        EXCLUDED.last_successful_request_at),
                   runtime_version = CASE WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                     THEN COALESCE(EXCLUDED.runtime_version, existing.runtime_version)
                     ELSE existing.runtime_version END,
                   processor_count = CASE WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                     THEN COALESCE(EXCLUDED.processor_count, existing.processor_count)
                     ELSE existing.processor_count END,
                   source = CASE WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                     THEN EXCLUDED.source ELSE existing.source END,
                   confidence = CASE WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                     THEN EXCLUDED.confidence ELSE existing.confidence END,
                   updated_at = now() RETURNING *""",
                observation.model_dump(mode="python"),
            ).fetchone()
        return cast(dict[str, Any], row)

    def list_gateway_instance_observations(
        self,
        deployment_id: UUID,
        *,
        limit: int = 100,
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_instance_observation WHERE deployment_id = %s
                   ORDER BY last_seen_at DESC, native_instance_id LIMIT %s""",
                (deployment_id, max(1, min(limit, 500))),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def upsert_gateway_deployment_inventory(
        self,
        inventory: GatewayDeploymentInventory,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """INSERT INTO gateway_deployment_inventory AS existing
                   (deployment_id, source_api_version, synced_at)
                   VALUES (%s, %s, %s) ON CONFLICT (deployment_id) DO UPDATE SET
                   source_api_version = EXCLUDED.source_api_version,
                   synced_at = EXCLUDED.synced_at, updated_at = now()
                   WHERE EXCLUDED.synced_at >= existing.synced_at RETURNING *""",
                (inventory.deployment_id, inventory.source_api_version, inventory.synced_at),
            ).fetchone()
            if row is None:
                current = self._deployment_inventories(connection, [inventory.deployment_id])
                if not current:
                    raise RuntimeError("Gateway inventory upsert returned no row")
                return current[0]
            connection.execute(
                "DELETE FROM gateway_deployment_api_association WHERE deployment_id = %s",
                (inventory.deployment_id,),
            )
            connection.execute(
                "DELETE FROM gateway_deployment_hostname_inventory WHERE deployment_id = %s",
                (inventory.deployment_id,),
            )
            connection.execute(
                "DELETE FROM gateway_deployment_ca_inventory WHERE deployment_id = %s",
                (inventory.deployment_id,),
            )
            with connection.cursor() as cursor:
                if inventory.associated_api_ids:
                    cursor.executemany(
                        "INSERT INTO gateway_deployment_api_association VALUES (%s,%s)",
                        [(inventory.deployment_id, item) for item in inventory.associated_api_ids],
                    )
                if inventory.hostname_configurations:
                    cursor.executemany(
                        """INSERT INTO gateway_deployment_hostname_inventory
                           (deployment_id, configuration_id, name, hostname, certificate_id,
                            negotiate_client_certificate) VALUES (%s,%s,%s,%s,%s,%s)""",
                        [
                            (
                                inventory.deployment_id,
                                item.configuration_id,
                                item.name,
                                item.hostname,
                                item.certificate_id,
                                item.negotiate_client_certificate,
                            )
                            for item in inventory.hostname_configurations
                        ],
                    )
                if inventory.certificate_authorities:
                    cursor.executemany(
                        """INSERT INTO gateway_deployment_ca_inventory
                           (deployment_id, authority_id, name, is_trusted) VALUES (%s,%s,%s,%s)""",
                        [
                            (inventory.deployment_id, item.authority_id, item.name, item.is_trusted)
                            for item in inventory.certificate_authorities
                        ],
                    )
            return {**inventory.model_dump(mode="python"), **dict(row)}

    @staticmethod
    def _deployment_inventories(
        connection: Any, deployment_ids: Sequence[UUID]
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT * FROM gateway_deployment_inventory
               WHERE deployment_id = ANY(%s) ORDER BY deployment_id""",
            (list(deployment_ids),),
        ).fetchall()
        result = {
            row["deployment_id"]: {
                **dict(row),
                "associated_api_ids": [],
                "hostname_configurations": [],
                "certificate_authorities": [],
            }
            for row in rows
        }
        associated = connection.execute(
            """SELECT deployment_id, api_id FROM gateway_deployment_api_association
               WHERE deployment_id = ANY(%s) ORDER BY lower(api_id), api_id""",
            (list(deployment_ids),),
        ).fetchall()
        hostnames = connection.execute(
            """SELECT deployment_id, configuration_id, name, hostname, certificate_id,
               negotiate_client_certificate FROM gateway_deployment_hostname_inventory
               WHERE deployment_id = ANY(%s) ORDER BY lower(name), configuration_id""",
            (list(deployment_ids),),
        ).fetchall()
        authorities = connection.execute(
            """SELECT deployment_id, authority_id, name, is_trusted
               FROM gateway_deployment_ca_inventory WHERE deployment_id = ANY(%s)
               ORDER BY lower(name), authority_id""",
            (list(deployment_ids),),
        ).fetchall()
        for row in associated:
            result[row["deployment_id"]]["associated_api_ids"].append(row["api_id"])
        for name, children in (
            ("hostname_configurations", hostnames),
            ("certificate_authorities", authorities),
        ):
            for row in children:
                result[row["deployment_id"]][name].append(
                    {key: value for key, value in row.items() if key != "deployment_id"}
                )
        return list(result.values())

    def get_gateway_deployment_inventory(self, deployment_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            rows = self._deployment_inventories(connection, [deployment_id])
        return rows[0] if rows else None

    def list_gateway_deployment_inventories(
        self,
        deployment_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        if not deployment_ids:
            return []
        with self._connection() as connection:
            return self._deployment_inventories(connection, deployment_ids)

    @staticmethod
    def _assert_gateway_retirement_allowed(connection: Any, deployment: GatewayDeployment) -> None:
        pending = connection.execute(
            """SELECT id FROM gateway_publication WHERE gateway_profile_id = %s
               AND status NOT IN ('active', 'failed', 'superseded', 'rolled_back') LIMIT 1""",
            (deployment.apim_target_id,),
        ).fetchone()
        if pending is not None:
            raise ValueError("Wait for the APIM publication to finish before changing this Gateway")
        models = connection.execute(
            """SELECT model.id FROM managed_model model
               JOIN model_runtime runtime ON runtime.id = model.runtime_id
               LEFT JOIN apim_target_route_binding binding
                 ON binding.logical_model_id = model.id AND binding.apim_target_id = %s
               WHERE (binding.id IS NOT NULL AND binding.enabled AND (
                   binding.deployment_scope_mode = 'all' OR EXISTS (
                     SELECT 1 FROM apim_target_route_binding_deployment selected
                     WHERE selected.route_binding_id = binding.id
                       AND selected.gateway_deployment_id = %s
                   ))) OR (binding.id IS NULL AND runtime.gateway_profile_id = %s) LIMIT 1""",
            (deployment.apim_target_id, deployment.id, deployment.apim_target_id),
        ).fetchone()
        if models is not None:
            raise ValueError("Remove or move the Gateway's published models first")

    def upsert_gateway_deployment(
        self,
        deployment: GatewayDeployment,
        *,
        preserve_display_name: bool = False,
        retiring_deployment: GatewayDeployment | None = None,
    ) -> dict[str, Any]:
        values = deployment.model_dump(mode="json")
        values["preserve_display_name"] = preserve_display_name
        with self._connection() as connection, connection.transaction():
            retiring = retiring_deployment or (
                deployment
                if deployment.kind == "self_hosted"
                and deployment.retired_at is not None
                and preserve_display_name
                else None
            )
            if retiring is not None:
                for target_id in sorted(
                    {retiring.apim_target_id, deployment.apim_target_id}, key=str
                ):
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"gateway-publication:{target_id}",),
                    )
                current = connection.execute(
                    """SELECT id, retired_at FROM gateway_deployment WHERE id = %s
                       AND apim_target_id = %s AND kind = 'self_hosted' FOR UPDATE""",
                    (retiring.id, retiring.apim_target_id),
                ).fetchone()
                if current is None or (
                    retiring_deployment is not None and current["retired_at"] is not None
                ):
                    raise ValueError("The Self-hosted Gateway changed before removal")
                if current["retired_at"] is None:
                    self._assert_gateway_retirement_allowed(connection, retiring)
                if retiring_deployment is not None:
                    destination = connection.execute(
                        """SELECT id FROM gateway_deployment WHERE id = %s
                           AND retired_at IS NULL AND endpoint_url IS NOT NULL""",
                        (deployment.id,),
                    ).fetchone()
                    if destination is not None:
                        raise ValueError("The destination Gateway is already associated")
            row = connection.execute(
                """INSERT INTO gateway_deployment AS existing (
                   schema_version, id, apim_target_id, native_gateway_id, kind, display_name,
                   location, endpoint_url, hosting_mode, lifecycle_ownership, release_gate,
                   declared_state, reachability_state, observation_state, runtime_version_support,
                   rate_limit_synchronization, last_arm_sync_at, last_probe_at, last_observed_at,
                   retired_at
                   ) VALUES (
                   %(schema_version)s, %(id)s, %(apim_target_id)s, %(native_gateway_id)s,
                   %(kind)s, %(display_name)s, %(location)s, %(endpoint_url)s, %(hosting_mode)s,
                   %(lifecycle_ownership)s, %(release_gate)s, %(declared_state)s,
                   %(reachability_state)s, %(observation_state)s, %(runtime_version_support)s,
                   %(rate_limit_synchronization)s, %(last_arm_sync_at)s, %(last_probe_at)s,
                   %(last_observed_at)s, %(retired_at)s
                   ) ON CONFLICT (id) DO UPDATE SET
                   display_name = CASE WHEN %(preserve_display_name)s
                     THEN existing.display_name ELSE EXCLUDED.display_name END,
                   location = EXCLUDED.location, endpoint_url = EXCLUDED.endpoint_url,
                   hosting_mode = EXCLUDED.hosting_mode,
                   lifecycle_ownership = EXCLUDED.lifecycle_ownership,
                   release_gate = EXCLUDED.release_gate, declared_state = EXCLUDED.declared_state,
                   reachability_state = EXCLUDED.reachability_state,
                   observation_state = EXCLUDED.observation_state,
                   runtime_version_support = EXCLUDED.runtime_version_support,
                   rate_limit_synchronization = EXCLUDED.rate_limit_synchronization,
                   last_arm_sync_at = EXCLUDED.last_arm_sync_at,
                   last_probe_at = EXCLUDED.last_probe_at,
                   last_observed_at = EXCLUDED.last_observed_at,
                   retired_at = CASE WHEN %(preserve_display_name)s
                     THEN COALESCE(existing.retired_at, EXCLUDED.retired_at)
                     ELSE EXCLUDED.retired_at END, updated_at = now()
                   WHERE existing.apim_target_id = EXCLUDED.apim_target_id
                     AND lower(existing.native_gateway_id) = lower(EXCLUDED.native_gateway_id)
                     AND existing.kind = EXCLUDED.kind RETURNING *""",
                values,
            ).fetchone()
            if row is None:
                raise ValueError("Gateway deployment identity cannot be changed in place")
            if retiring_deployment is not None:
                connection.execute(
                    """UPDATE gateway_deployment SET retired_at = now(), updated_at = now()
                       WHERE id = %s AND apim_target_id = %s AND retired_at IS NULL""",
                    (retiring_deployment.id, retiring_deployment.apim_target_id),
                )
        return cast(dict[str, Any], row)

    def rename_gateway_deployment(self, deployment_id: UUID, display_name: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE gateway_deployment AS deployment SET display_name = %s, updated_at = now()
                   WHERE deployment.id = %s AND deployment.kind = 'self_hosted'
                     AND deployment.retired_at IS NULL AND EXISTS (
                       SELECT 1 FROM gateway_profile profile
                       WHERE profile.id = deployment.apim_target_id
                         AND profile.implementation = 'apim' AND profile.enabled)
                   RETURNING deployment.id""",
                (display_name, deployment_id),
            ).fetchone()
        return row is not None

    def get_gateway_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_deployment WHERE id = %s",
                (deployment_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

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
        if not apim_target_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_deployment WHERE apim_target_id = ANY(%s)
                   AND (%s OR retired_at IS NULL)
                   ORDER BY apim_target_id, kind, display_name, id""",
                (list(apim_target_ids), include_retired),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def upsert_apim_target_route_binding(self, binding: ApimTargetRouteBinding) -> dict[str, Any]:
        values = binding.model_dump(mode="json")
        values["deployment_scope_mode"] = values.pop("deployment_scope")["mode"]
        deployment_ids = list(binding.deployment_scope.deployment_ids)
        with self._connection() as connection, connection.transaction():
            runtime = connection.execute(
                "SELECT id FROM model_runtime WHERE id = %s AND gateway_profile_id = %s",
                (binding.runtime_id, binding.apim_target_id),
            ).fetchone()
            if runtime is None:
                raise ValueError("Route Runtime does not belong to the APIM target")
            if binding.current_publication_id is not None:
                publication = connection.execute(
                    "SELECT id FROM gateway_publication WHERE id = %s AND gateway_profile_id = %s",
                    (binding.current_publication_id, binding.apim_target_id),
                ).fetchone()
                if publication is None:
                    raise ValueError("Route publication does not belong to the APIM target")
            if deployment_ids:
                rows = connection.execute(
                    """SELECT id FROM gateway_deployment WHERE apim_target_id = %s
                       AND id = ANY(%s) AND retired_at IS NULL""",
                    (binding.apim_target_id, deployment_ids),
                ).fetchall()
                if {row["id"] for row in rows} != set(deployment_ids):
                    raise ValueError(
                        "Selected deployments must be active and belong to the APIM target"
                    )
            row = connection.execute(
                """INSERT INTO apim_target_route_binding AS existing (
                   schema_version, id, logical_model_id, apim_target_id, runtime_id, enabled,
                   is_default_serving, deployment_scope_mode,
                   current_publication_id, capability_state
                   ) VALUES (%(schema_version)s, %(id)s, %(logical_model_id)s, %(apim_target_id)s,
                   %(runtime_id)s, %(enabled)s, %(is_default_serving)s, %(deployment_scope_mode)s,
                   %(current_publication_id)s, %(capability_state)s)
                   ON CONFLICT (id) DO UPDATE SET runtime_id = EXCLUDED.runtime_id,
                   enabled = EXCLUDED.enabled, is_default_serving = EXCLUDED.is_default_serving,
                   deployment_scope_mode = EXCLUDED.deployment_scope_mode,
                   current_publication_id = EXCLUDED.current_publication_id,
                   capability_state = EXCLUDED.capability_state, updated_at = now()
                   WHERE existing.logical_model_id = EXCLUDED.logical_model_id
                     AND existing.apim_target_id = EXCLUDED.apim_target_id RETURNING *""",
                values,
            ).fetchone()
            if row is None:
                raise ValueError("APIM target route identity cannot be changed in place")
            connection.execute(
                "DELETE FROM apim_target_route_binding_deployment WHERE route_binding_id = %s",
                (binding.id,),
            )
            if deployment_ids:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO apim_target_route_binding_deployment
                           (route_binding_id, apim_target_id, gateway_deployment_id)
                           VALUES (%s,%s,%s)""",
                        [(binding.id, binding.apim_target_id, item) for item in deployment_ids],
                    )
            return self._route_binding_view(connection, row)

    @staticmethod
    def _route_binding_view(connection: Any, row: dict[str, Any]) -> dict[str, Any]:
        selected = connection.execute(
            """SELECT gateway_deployment_id FROM apim_target_route_binding_deployment
               WHERE route_binding_id = %s ORDER BY gateway_deployment_id""",
            (row["id"],),
        ).fetchall()
        item = dict(row)
        item["deployment_scope"] = {
            "mode": item.pop("deployment_scope_mode"),
            "deployment_ids": tuple(entry["gateway_deployment_id"] for entry in selected),
        }
        return item

    def get_apim_target_route_binding(self, binding_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM apim_target_route_binding WHERE id = %s",
                (binding_id,),
            ).fetchone()
            return self._route_binding_view(connection, row) if row is not None else None

    def list_apim_target_route_bindings(
        self,
        *,
        logical_model_id: UUID | None = None,
        apim_target_id: UUID | None = None,
        apim_target_ids: Sequence[UUID] | None = None,
    ) -> Sequence[dict[str, Any]]:
        if apim_target_ids is not None and not apim_target_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT binding.*, ARRAY(SELECT membership.gateway_deployment_id
                   FROM apim_target_route_binding_deployment membership
                   WHERE membership.route_binding_id = binding.id
                   ORDER BY membership.gateway_deployment_id
                   ) AS deployment_ids FROM apim_target_route_binding binding
                   WHERE (%s::uuid IS NULL OR binding.logical_model_id = %s)
                     AND (%s::uuid IS NULL OR binding.apim_target_id = %s)
                     AND (%s::uuid[] IS NULL OR binding.apim_target_id = ANY(%s))
                   ORDER BY binding.logical_model_id, binding.apim_target_id, binding.id""",
                (
                    logical_model_id,
                    logical_model_id,
                    apim_target_id,
                    apim_target_id,
                    list(apim_target_ids) if apim_target_ids is not None else None,
                    list(apim_target_ids) if apim_target_ids is not None else None,
                ),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["deployment_scope"] = {
                "mode": item.pop("deployment_scope_mode"),
                "deployment_ids": tuple(item.pop("deployment_ids")),
            }
            result.append(item)
        return result

    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def upsert_apim_target(self, target: ApimTarget) -> dict[str, Any]:
        values = target.model_dump(mode="json")
        values["capabilities"] = Jsonb(values["capabilities"])
        with self._connection() as connection, connection.transaction():
            gateway = connection.execute(
                "SELECT implementation FROM gateway_profile WHERE id = %s",
                (target.gateway_profile_id,),
            ).fetchone()
            if gateway is None or gateway["implementation"] != "apim":
                raise ValueError("APIM target requires an existing APIM gateway profile")
            row = connection.execute(
                """INSERT INTO apim_target_metadata AS existing (
                   schema_version, gateway_profile_id, arm_resource_id, tenant_id,
                   subscription_id, resource_group, service_name, api_id, api_path,
                   service_tier, tier_family, service_location, principal_id,
                   management_auth_profile_ref, probe_subscription_id, probe_secret_ref,
                   ownership_mode, capabilities, last_inventory_sync_at, last_validated_at,
                   drift_state
                   ) VALUES (
                   %(schema_version)s, %(gateway_profile_id)s, %(arm_resource_id)s, %(tenant_id)s,
                   %(subscription_id)s, %(resource_group)s, %(service_name)s, %(api_id)s,
                   %(api_path)s, %(service_tier)s, %(tier_family)s, %(service_location)s,
                   %(principal_id)s, %(management_auth_profile_ref)s, %(probe_subscription_id)s,
                   %(probe_secret_ref)s, %(ownership_mode)s, %(capabilities)s,
                   %(last_inventory_sync_at)s, %(last_validated_at)s, %(drift_state)s
                   ) ON CONFLICT (gateway_profile_id) DO UPDATE SET
                   service_tier = EXCLUDED.service_tier, tier_family = EXCLUDED.tier_family,
                   service_location = EXCLUDED.service_location,
                   principal_id = EXCLUDED.principal_id,
                   management_auth_profile_ref = EXCLUDED.management_auth_profile_ref,
                   probe_subscription_id = EXCLUDED.probe_subscription_id,
                   probe_secret_ref = EXCLUDED.probe_secret_ref,
                   ownership_mode = EXCLUDED.ownership_mode,
                   capabilities = EXCLUDED.capabilities,
                   last_inventory_sync_at = EXCLUDED.last_inventory_sync_at,
                   last_validated_at = EXCLUDED.last_validated_at,
                   drift_state = EXCLUDED.drift_state, updated_at = now()
                   WHERE lower(existing.arm_resource_id) = lower(EXCLUDED.arm_resource_id)
                     AND existing.tenant_id = EXCLUDED.tenant_id
                     AND existing.subscription_id = EXCLUDED.subscription_id
                     AND lower(existing.resource_group) = lower(EXCLUDED.resource_group)
                     AND lower(existing.service_name) = lower(EXCLUDED.service_name)
                     AND lower(existing.api_id) = lower(EXCLUDED.api_id)
                     AND existing.api_path = EXCLUDED.api_path RETURNING *""",
                values,
            ).fetchone()
        if row is None:
            raise ValueError("APIM target identity cannot be changed in place")
        return cast(dict[str, Any], row)

    def get_apim_target(self, gateway_profile_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM apim_target_metadata WHERE gateway_profile_id = %s",
                (gateway_profile_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_apim_targets(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM apim_target_metadata ORDER BY service_name, gateway_profile_id"
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)
