from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

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
from .repository_publications import PostgreSqlPublicationRepositoryMixin


class PostgreSqlGatewayTargetRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def list_self_hosted_gateway_provisions(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT operation.* FROM gateway_release_operation operation
                   JOIN gateway_profile profile ON profile.id = operation.gateway_profile_id
                   WHERE operation.operation_kind = 'self_hosted_gateway_provision'
                     AND profile.enabled AND profile.implementation = 'apim'
                   ORDER BY operation.created_at DESC, operation.id DESC LIMIT 200""",
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def retry_self_hosted_gateway_provision(
        self,
        operation_id: UUID,
        actor: str,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            identity = connection.execute(
                "SELECT gateway_profile_id FROM gateway_release_operation WHERE id = %s",
                (operation_id,),
            ).fetchone()
            if identity is None:
                raise ValueError("Gateway creation operation was not found")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"gateway-release-operation:{identity['gateway_profile_id']}",),
            )
            prior = connection.execute(
                "SELECT * FROM gateway_release_operation WHERE id = %s FOR UPDATE",
                (operation_id,),
            ).fetchone()
            if prior is None or prior["operation_kind"] != "self_hosted_gateway_provision":
                raise ValueError("Gateway creation operation was not found")
            if prior["status"] in {"queued", "validating_dependencies", "verifying_readback"}:
                return cast(dict[str, Any], prior)
            if prior["status"] != "failed":
                raise ValueError("Only failed Gateway creation can be retried")
            open_operation = connection.execute(
                """SELECT id FROM gateway_release_operation WHERE gateway_profile_id = %s
                   AND id <> %s AND status NOT IN ('succeeded', 'failed', 'restored') LIMIT 1""",
                (prior["gateway_profile_id"], operation_id),
            ).fetchone()
            if open_operation is not None:
                raise ValueError("Another Gateway operation is already in progress")
            checkpoint = {**prior["checkpoint"], "retry_attempt_base": prior["attempt_count"]}
            row = connection.execute(
                """UPDATE gateway_release_operation SET status = 'queued',
                   checkpoint = %s, lease_owner = NULL, lease_expires_at = NULL,
                   error_code = NULL, error_message = NULL, completed_at = NULL, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (Jsonb(checkpoint), operation_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_operation_audit
                   (operation_id, from_status, to_status, actor, detail)
                   VALUES (%s, 'failed', 'queued', %s, %s)""",
                (
                    operation_id,
                    actor,
                    Jsonb(
                        {
                            "retry": True,
                            "error_code": prior["error_code"],
                            "error_message": prior["error_message"],
                            "attempt_count": prior["attempt_count"],
                            "checkpoint": prior["checkpoint"],
                        }
                    ),
                ),
            )
        return cast(dict[str, Any], row)

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
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"gateway-release-operation:{spec.target.gateway_profile_id}",),
            )
            operation = connection.execute(
                """SELECT * FROM gateway_release_operation
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > clock_timestamp()
                     AND status = 'verifying_readback'
                     AND operation_kind = 'self_hosted_gateway_provision'
                   FOR UPDATE""",
                (operation_id, worker_id),
            ).fetchone()
            if operation is None or operation["confirmation_sha256"] != gateway_provision_digest(
                spec
            ):
                raise ValueError("Gateway creation lease or intent changed")
            if SelfHostedGatewayProvisionSpec.model_validate(operation["semantic_preview"]) != spec:
                raise ValueError("Gateway creation snapshot changed")
            current_target = connection.execute(
                """SELECT target.* FROM apim_target_metadata target
                   JOIN gateway_profile profile ON profile.id = target.gateway_profile_id
                   WHERE target.gateway_profile_id = %s AND profile.enabled
                   FOR UPDATE OF target, profile""",
                (spec.target.gateway_profile_id,),
            ).fetchone()
            if current_target is None:
                raise ValueError("Gateway creation target is unavailable")
            validate_gateway_provision_target(spec, current_target)
            values = deployment.model_dump(mode="json", exclude={"provisioning_operation_id"})
            columns = ", ".join(values)
            row = connection.execute(
                f"""INSERT INTO gateway_deployment AS existing ({columns})
                    SELECT {columns} FROM jsonb_populate_record(NULL::gateway_deployment, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        display_name = EXCLUDED.display_name, endpoint_url = EXCLUDED.endpoint_url,
                        location = EXCLUDED.location, hosting_mode = EXCLUDED.hosting_mode,
                        release_gate = EXCLUDED.release_gate,
                        declared_state = EXCLUDED.declared_state,
                        reachability_state = 'unknown', last_probe_at = NULL,
                        observation_state = 'unknown', last_observed_at = NULL,
                        last_arm_sync_at = EXCLUDED.last_arm_sync_at, updated_at = now()
                    WHERE existing.apim_target_id = EXCLUDED.apim_target_id
                      AND existing.native_gateway_id = EXCLUDED.native_gateway_id
                      AND existing.kind = 'self_hosted' AND existing.retired_at IS NULL
                      AND existing.endpoint_url IS NULL
                    RETURNING id""",
                (Jsonb(values),),
            ).fetchone()
            if row is None:
                raise ValueError("Existing Gateway cannot be overwritten or resurrected")
            connection.execute(
                """INSERT INTO gateway_deployment_provisioning
                   (deployment_id, apim_target_id, operation_id) VALUES (%s, %s, %s)""",
                (spec.deployment_id, spec.target.gateway_profile_id, operation_id),
            )
            inventory_row = connection.execute(
                """INSERT INTO gateway_deployment_inventory AS existing
                   (deployment_id, source_api_version, synced_at) VALUES (%s, %s, %s)
                   ON CONFLICT (deployment_id) DO UPDATE SET
                       source_api_version = EXCLUDED.source_api_version,
                       synced_at = EXCLUDED.synced_at, updated_at = now()
                   WHERE EXCLUDED.synced_at >= existing.synced_at RETURNING deployment_id""",
                (inventory.deployment_id, inventory.source_api_version, inventory.synced_at),
            ).fetchone()
            if inventory_row is None:
                raise ValueError("Gateway inventory changed during creation")
            self._write_gateway_inventory_collections(connection, inventory)
            completed = connection.execute(
                """UPDATE gateway_release_operation SET status = 'succeeded', checkpoint = %s,
                   error_code = NULL, error_message = NULL,
                   completed_at = now(), updated_at = now(),
                   lease_owner = NULL, lease_expires_at = NULL
                   WHERE id = %s AND lease_owner = %s AND lease_expires_at > clock_timestamp()
                   RETURNING id""",
                (Jsonb(dict(checkpoint)), operation_id, worker_id),
            ).fetchone()
            if completed is None:
                raise ValueError("Gateway creation lease expired before completion")
            if enqueue_inventory_sync:
                synchronization = connection.execute(
                    """INSERT INTO gateway_release_operation
                       (gateway_profile_id, operation_kind, semantic_preview, created_by)
                       VALUES (%s, 'target_inventory_sync', %s, %s) RETURNING id""",
                    (
                        spec.target.gateway_profile_id,
                        Jsonb({"source_provision_operation_id": str(operation_id)}),
                        worker_id,
                    ),
                ).fetchone()
                connection.execute(
                    """INSERT INTO gateway_release_operation_audit
                       (operation_id, from_status, to_status, actor, detail)
                       VALUES (%s, NULL, 'queued', %s, %s)""",
                    (
                        synchronization["id"],
                        worker_id,
                        Jsonb({"source_provision_operation_id": str(operation_id)}),
                    ),
                )
            connection.execute(
                """INSERT INTO gateway_release_operation_audit
                   (operation_id, from_status, to_status, actor, detail)
                   VALUES (%s, 'verifying_readback', 'succeeded', %s, %s)""",
                (operation_id, worker_id, Jsonb({"checkpoint": dict(checkpoint)})),
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
        resource_id = plan.request.resource_id.casefold()
        preview = plan.model_dump(mode="json")
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"apim-onboarding:{resource_id}",),
            )
            existing = connection.execute(
                """SELECT * FROM gateway_release_operation
                   WHERE gateway_profile_id = %s AND operation_kind = 'apim_onboarding'
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (profile_id,),
            ).fetchone()
            if existing is not None:
                if existing["confirmation_sha256"] != plan.confirmation_sha256:
                    raise ValueError("The APIM initialization plan has changed")
                return cast(dict[str, Any], existing)
            target = connection.execute(
                """SELECT gateway_profile_id FROM apim_target_metadata
                   WHERE lower(arm_resource_id) = %s""",
                (resource_id,),
            ).fetchone()
            if target is not None:
                raise ValueError("This APIM instance is already registered")
            profile = connection.execute(
                """INSERT INTO gateway_profile (
                       id, name, implementation, auth_type, enabled, is_default, config
                     ) VALUES (%s, %s, 'apim', 'api_key', FALSE, FALSE, %s)
                     ON CONFLICT DO NOTHING RETURNING id""",
                (
                    profile_id,
                    plan.request.name or f"{plan.service_name} ({profile_id.hex[:8]})",
                    Jsonb(
                        {
                            "header_name": "Ocp-Apim-Subscription-Key",
                            "onboarding_resource_id": resource_id,
                            "onboarding_tenant_id": str(plan.request.tenant_id),
                        }
                    ),
                ),
            ).fetchone()
            if profile is None:
                raise ValueError("A Gateway profile already uses this identity or name")
            operation = connection.execute(
                """INSERT INTO gateway_release_operation (
                       gateway_profile_id, operation_kind, confirmation_sha256,
                       semantic_preview, created_by
                   ) VALUES (%s, 'apim_onboarding', %s, %s, %s) RETURNING *""",
                (profile_id, plan.confirmation_sha256, Jsonb(preview), actor),
            ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_operation_secret (
                      operation_id, credential_ciphertext)
                   VALUES (%s, %s)""",
                (operation["id"], credential_ciphertext),
            )
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor
                   ) VALUES (%s, NULL, 'queued', %s)""",
                (operation["id"], actor),
            )
        return cast(dict[str, Any], operation)

    def list_apim_onboarding_operations(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT ON (gateway_profile_id) * FROM gateway_release_operation
                   WHERE operation_kind = 'apim_onboarding'
                   ORDER BY gateway_profile_id, created_at DESC, id DESC""",
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def retry_apim_onboarding_operation(self, operation_id: UUID, actor: str) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            prior = connection.execute(
                "SELECT * FROM gateway_release_operation WHERE id = %s FOR UPDATE",
                (operation_id,),
            ).fetchone()
            if prior is None or prior["operation_kind"] != "apim_onboarding":
                raise ValueError("APIM initialization operation was not found")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"gateway-release-operation:{prior['gateway_profile_id']}",),
            )
            current = connection.execute(
                """SELECT * FROM gateway_release_operation WHERE gateway_profile_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (prior["gateway_profile_id"],),
            ).fetchone()
            if current["id"] != operation_id or prior["status"] != "failed":
                raise ValueError("Only the latest failed APIM initialization can be retried")
            secret = connection.execute(
                """SELECT credential_ciphertext FROM gateway_release_operation_secret
                   WHERE operation_id = %s""",
                (operation_id,),
            ).fetchone()
            if secret is None:
                raise ValueError("APIM initialization credentials are unavailable")
            operation = connection.execute(
                """INSERT INTO gateway_release_operation (
                       gateway_profile_id, operation_kind, confirmation_sha256,
                       semantic_preview, checkpoint, created_by
                   ) VALUES (%s, 'apim_onboarding', %s, %s, %s, %s) RETURNING *""",
                (
                    prior["gateway_profile_id"],
                    prior["confirmation_sha256"],
                    Jsonb(prior["semantic_preview"]),
                    Jsonb({
                        "retry_of": str(operation_id),
                        **{key: prior["checkpoint"][key] for key in (
                            "fixed_program_requests", "fixed_route_initialization",
                        ) if key in prior["checkpoint"]},
                    }),
                    actor,
                ),
            ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_operation_secret (
                      operation_id, credential_ciphertext)
                   VALUES (%s, %s)""",
                (operation["id"], secret["credential_ciphertext"]),
            )
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor, detail
                   ) VALUES (%s, NULL, 'queued', %s, %s)""",
                (operation["id"], actor, Jsonb({"retry_of": str(operation_id)})),
            )
        return cast(dict[str, Any], operation)

    def finish_apim_onboarding_operation(
        self,
        operation_id: UUID,
        worker_id: str,
        credential_ciphertext: bytes,
        credential_hint: str,
        configuration_sha256: str,
        fixed_publication: GatewayPublication | None = None,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.transaction():
            identity = connection.execute(
                "SELECT gateway_profile_id FROM gateway_release_operation WHERE id = %s",
                (operation_id,),
            ).fetchone()
            if identity is None:
                return None
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gateway-publication:{identity['gateway_profile_id']}",),
            )
            operation = connection.execute(
                """SELECT * FROM gateway_release_operation WHERE id = %s
                   AND operation_kind = 'apim_onboarding' AND status = 'preflight_probing'
                   AND lease_owner = %s AND lease_expires_at > now() FOR UPDATE""",
                (operation_id, worker_id),
            ).fetchone()
            if operation is None:
                return None
            plan = ApimOnboardingPlan.model_validate(operation["semantic_preview"])
            if fixed_publication is None:
                raise ValueError("APIM initialization fixed program is missing")
            fixed_publication.require_onboarding_installation(
                plan.gateway_profile_id, operation_id, plan.fixed_program_sha256,
            )
            profile = connection.execute(
                """UPDATE gateway_profile SET enabled = TRUE, base_url = %s,
                       credential_ciphertext = %s, credential_hint = %s,
                       config = config || %s, updated_at = now()
                   WHERE id = %s AND NOT enabled AND NOT is_default
                     AND config->>'onboarding_resource_id' = %s
                     AND EXISTS (SELECT 1 FROM apim_target_metadata target
                                                 JOIN gateway_deployment AS deployment
                                                     ON deployment.apim_target_id =
                                                            target.gateway_profile_id
                         WHERE target.gateway_profile_id = gateway_profile.id
                           AND lower(target.arm_resource_id) = %s
                           AND deployment.native_gateway_id = 'managed'
                           AND deployment.reachability_state = 'reachable')
                   RETURNING id""",
                (
                    str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path,
                    credential_ciphertext,
                    credential_hint,
                    Jsonb(
                        {
                            "onboarding_ready": True,
                            "onboarding_request": plan.request.model_dump(mode="json"),
                            "onboarding_configuration_sha256": configuration_sha256,
                        }
                    ),
                    plan.gateway_profile_id,
                    plan.request.resource_id.casefold(),
                    plan.request.resource_id.casefold(),
                ),
            ).fetchone()
            if profile is None:
                raise ValueError("Cannot activate an unverified or changed APIM profile")
            PostgreSqlPublicationRepositoryMixin._record_fixed_route_installation(
                connection, fixed_publication,
            )
            checkpoint = {
                **operation["checkpoint"],
                "step": "activate_target",
                "completed_steps": plan.steps,
                "managed_gateway_verified": True,
                "fixed_program_publication_id": str(fixed_publication.id),
                "fixed_program_sha256": fixed_publication.policy_sha256,
            }
            result = connection.execute(
                """UPDATE gateway_release_operation SET status = 'succeeded', checkpoint = %s,
                       error_code = NULL, error_message = NULL, lease_owner = NULL,
                       lease_expires_at = NULL, completed_at = now(), updated_at = now()
                   WHERE id = %s RETURNING *""",
                (Jsonb(checkpoint), operation_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO gateway_release_operation_audit (
                       operation_id, from_status, to_status, actor, detail
                   ) VALUES (%s, 'preflight_probing', 'succeeded', %s, %s)""",
                (operation_id, worker_id, Jsonb(checkpoint)),
            )
            connection.execute(
                """DELETE FROM gateway_release_operation_secret secret
                         USING gateway_release_operation operation
                         WHERE secret.operation_id = operation.id
                            AND operation.operation_kind = 'apim_onboarding'
                     AND operation.gateway_profile_id = %s""",
                (plan.gateway_profile_id,),
            )
        return cast(dict[str, Any], result)

    def enroll_apim_onboarding_budget_target(
        self,
        operation_id: UUID,
        worker_id: str,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            operation = connection.execute(
                """SELECT operation.gateway_profile_id FROM gateway_release_operation operation
                   JOIN gateway_profile profile ON profile.id = operation.gateway_profile_id
                   WHERE operation.id = %s AND operation.operation_kind = 'apim_onboarding'
                     AND operation.status = 'verifying_readback'
                     AND operation.lease_owner = %s AND operation.lease_expires_at > now()
                     AND NOT profile.enabled
                     AND profile.config->>'onboarding_resource_id' IS NOT NULL
                   FOR UPDATE OF operation""",
                (operation_id, worker_id),
            ).fetchone()
            if operation is None:
                raise ValueError("A valid initialization lease is required for APIM enrollment")
            rollout = connection.execute(
                """SELECT * FROM budget_execution_rollout WHERE state = 'active'
                     AND scope_sync_enabled AND prepared_period =
                         date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date
                   FOR UPDATE""",
            ).fetchone()
            if rollout is None:
                raise ValueError("The current budget execution period is not ready")
            if operation["gateway_profile_id"] not in rollout["gateway_profile_ids"]:
                rollout = connection.execute(
                    """UPDATE budget_execution_rollout SET
                           gateway_profile_ids = array_append(gateway_profile_ids, %s::uuid)
                       WHERE epoch = %s RETURNING *""",
                    (operation["gateway_profile_id"], rollout["epoch"]),
                ).fetchone()
        return cast(dict[str, Any], rollout)

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
                           schema_version, gateway_profile_id, arm_resource_id,
                           tenant_id, subscription_id, resource_group, service_name,
                           api_id, api_path, service_tier, tier_family, service_location,
                           principal_id, management_auth_profile_ref,
                           probe_subscription_id, probe_secret_ref, ownership_mode,
                           capabilities, last_inventory_sync_at, last_validated_at,
                           drift_state
                       ) VALUES (
                           %(schema_version)s, %(gateway_profile_id)s, %(arm_resource_id)s,
                           %(tenant_id)s, %(subscription_id)s, %(resource_group)s,
                           %(service_name)s, %(api_id)s, %(api_path)s, %(service_tier)s,
                           %(tier_family)s, %(service_location)s, %(principal_id)s,
                           %(management_auth_profile_ref)s, %(probe_subscription_id)s,
                           %(probe_secret_ref)s, %(ownership_mode)s, %(capabilities)s,
                           %(last_inventory_sync_at)s, %(last_validated_at)s,
                           %(drift_state)s
                       ) ON CONFLICT (gateway_profile_id) DO UPDATE SET
                           service_tier = EXCLUDED.service_tier,
                           tier_family = EXCLUDED.tier_family,
                           service_location = EXCLUDED.service_location,
                           principal_id = EXCLUDED.principal_id,
                           management_auth_profile_ref = EXCLUDED.management_auth_profile_ref,
                           probe_subscription_id = EXCLUDED.probe_subscription_id,
                           probe_secret_ref = EXCLUDED.probe_secret_ref,
                           ownership_mode = EXCLUDED.ownership_mode,
                           capabilities = EXCLUDED.capabilities,
                           last_inventory_sync_at = EXCLUDED.last_inventory_sync_at,
                           last_validated_at = EXCLUDED.last_validated_at,
                           drift_state = EXCLUDED.drift_state,
                           updated_at = now()
                       WHERE lower(existing.arm_resource_id) = lower(EXCLUDED.arm_resource_id)
                         AND existing.tenant_id = EXCLUDED.tenant_id
                         AND existing.subscription_id = EXCLUDED.subscription_id
                         AND lower(existing.resource_group) = lower(EXCLUDED.resource_group)
                         AND lower(existing.service_name) = lower(EXCLUDED.service_name)
                         AND lower(existing.api_id) = lower(EXCLUDED.api_id)
                         AND existing.api_path = EXCLUDED.api_path
                       RETURNING *""",
                values,
            ).fetchone()
        if row is None:
            raise ValueError("APIM target identity cannot be changed in place")
        return cast(dict[str, Any], row)

    def get_apim_target(self, gateway_profile_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM apim_target_metadata
                   WHERE gateway_profile_id = %s""",
                (gateway_profile_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_apim_targets(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM apim_target_metadata
                   ORDER BY service_name, gateway_profile_id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

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
                   ))) OR (binding.id IS NULL AND runtime.gateway_profile_id = %s)
               LIMIT 1""",
            (deployment.apim_target_id, deployment.id, deployment.apim_target_id),
        ).fetchone()
        if models is not None:
            raise ValueError("Remove or move the Gateway's published models first")

    def upsert_gateway_deployment(
        self, deployment: GatewayDeployment, *, preserve_display_name: bool = False,
        retiring_deployment: GatewayDeployment | None = None,
    ) -> dict[str, Any]:
        values = deployment.model_dump(mode="json")
        values["preserve_display_name"] = preserve_display_name
        with self._connection() as connection, connection.transaction():
            retiring = retiring_deployment or (
                deployment if deployment.kind == "self_hosted"
                and deployment.retired_at is not None and preserve_display_name else None
            )
            if retiring is not None:
                target_ids = {retiring.apim_target_id, deployment.apim_target_id}
                for target_id in sorted(target_ids, key=str):
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"gateway-publication:{target_id}",),
                    )
                current = connection.execute(
                    """SELECT id, retired_at FROM gateway_deployment
                       WHERE id = %s AND apim_target_id = %s AND kind = 'self_hosted'
                       FOR UPDATE""",
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
                           schema_version, id, apim_target_id, native_gateway_id,
                           kind, display_name, location, endpoint_url, hosting_mode,
                           lifecycle_ownership, release_gate, declared_state,
                           reachability_state, observation_state,
                           runtime_version_support, rate_limit_synchronization,
                           last_arm_sync_at, last_probe_at, last_observed_at, retired_at
                       ) VALUES (
                           %(schema_version)s, %(id)s, %(apim_target_id)s,
                           %(native_gateway_id)s, %(kind)s, %(display_name)s,
                           %(location)s, %(endpoint_url)s, %(hosting_mode)s,
                           %(lifecycle_ownership)s, %(release_gate)s,
                           %(declared_state)s, %(reachability_state)s,
                           %(observation_state)s, %(runtime_version_support)s,
                           %(rate_limit_synchronization)s, %(last_arm_sync_at)s,
                           %(last_probe_at)s, %(last_observed_at)s, %(retired_at)s
                       ) ON CONFLICT (id) DO UPDATE SET
                           display_name = CASE WHEN %(preserve_display_name)s
                               THEN existing.display_name ELSE EXCLUDED.display_name END,
                           location = EXCLUDED.location,
                           endpoint_url = EXCLUDED.endpoint_url,
                           hosting_mode = EXCLUDED.hosting_mode,
                           lifecycle_ownership = EXCLUDED.lifecycle_ownership,
                           release_gate = EXCLUDED.release_gate,
                           declared_state = EXCLUDED.declared_state,
                           reachability_state = EXCLUDED.reachability_state,
                           observation_state = EXCLUDED.observation_state,
                           runtime_version_support = EXCLUDED.runtime_version_support,
                           rate_limit_synchronization = EXCLUDED.rate_limit_synchronization,
                           last_arm_sync_at = EXCLUDED.last_arm_sync_at,
                           last_probe_at = EXCLUDED.last_probe_at,
                           last_observed_at = EXCLUDED.last_observed_at,
                           retired_at = CASE WHEN %(preserve_display_name)s
                               THEN COALESCE(existing.retired_at, EXCLUDED.retired_at)
                               ELSE EXCLUDED.retired_at END,
                           updated_at = now()
                       WHERE existing.apim_target_id = EXCLUDED.apim_target_id
                         AND lower(existing.native_gateway_id)
                             = lower(EXCLUDED.native_gateway_id)
                         AND existing.kind = EXCLUDED.kind
                       RETURNING *""",
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
        if row is None:
            raise ValueError("Gateway deployment identity cannot be changed in place")
        return cast(dict[str, Any], row)

    def rename_gateway_deployment(self, deployment_id: UUID, display_name: str) -> bool:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE gateway_deployment AS deployment
                   SET display_name = %s, updated_at = now()
                   WHERE deployment.id = %s
                     AND deployment.kind = 'self_hosted'
                     AND deployment.retired_at IS NULL
                     AND EXISTS (
                         SELECT 1 FROM gateway_profile AS profile
                         WHERE profile.id = deployment.apim_target_id
                           AND profile.implementation = 'apim' AND profile.enabled
                     )
                   RETURNING deployment.id""",
                (display_name, deployment_id),
            ).fetchone()
        return row is not None

    def get_gateway_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT deployment.*, provision.operation_id AS provisioning_operation_id
                   FROM gateway_deployment deployment
                   LEFT JOIN gateway_deployment_provisioning provision
                       ON provision.deployment_id = deployment.id
                   WHERE deployment.id = %s""",
                (deployment_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def list_gateway_deployments(
        self, apim_target_id: UUID, *, include_retired: bool = False
    ) -> Sequence[dict[str, Any]]:
        return self.list_gateway_deployments_for_targets(
            [apim_target_id], include_retired=include_retired
        )

    def list_gateway_deployments_for_targets(
        self, apim_target_ids: Sequence[UUID], *, include_retired: bool = False
    ) -> Sequence[dict[str, Any]]:
        if not apim_target_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT deployment.*, provision.operation_id AS provisioning_operation_id
                   FROM gateway_deployment deployment
                   LEFT JOIN gateway_deployment_provisioning provision
                       ON provision.deployment_id = deployment.id
                   WHERE deployment.apim_target_id = ANY(%s)
                       AND (%s OR deployment.retired_at IS NULL)
                   ORDER BY deployment.apim_target_id, deployment.kind, deployment.display_name,
                            deployment.id""",
                (list(apim_target_ids), include_retired),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def upsert_gateway_instance_observation(
        self, observation: GatewayInstanceObservation
    ) -> dict[str, Any]:
        values = observation.model_dump(mode="json")
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """INSERT INTO gateway_instance_observation AS existing (
                           schema_version, deployment_id, native_instance_id,
                           runtime_version, processor_count, first_seen_at,
                           last_seen_at, last_successful_request_at, source, confidence
                       ) VALUES (
                           %(schema_version)s, %(deployment_id)s,
                           %(native_instance_id)s, %(runtime_version)s,
                           %(processor_count)s, %(first_seen_at)s, %(last_seen_at)s,
                           %(last_successful_request_at)s, %(source)s, %(confidence)s
                       ) ON CONFLICT (deployment_id, native_instance_id) DO UPDATE SET
                           runtime_version = CASE
                               WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                               THEN COALESCE(EXCLUDED.runtime_version, existing.runtime_version)
                               ELSE existing.runtime_version
                           END,
                           processor_count = CASE
                               WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                               THEN COALESCE(EXCLUDED.processor_count, existing.processor_count)
                               ELSE existing.processor_count
                           END,
                           first_seen_at = LEAST(
                               existing.first_seen_at, EXCLUDED.first_seen_at
                           ),
                           last_seen_at = GREATEST(
                               existing.last_seen_at, EXCLUDED.last_seen_at
                           ),
                           last_successful_request_at = CASE
                               WHEN existing.last_successful_request_at IS NULL
                                   THEN EXCLUDED.last_successful_request_at
                               WHEN EXCLUDED.last_successful_request_at IS NULL
                                   THEN existing.last_successful_request_at
                               ELSE GREATEST(
                                   existing.last_successful_request_at,
                                   EXCLUDED.last_successful_request_at
                               )
                           END,
                           source = CASE
                               WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                               THEN EXCLUDED.source ELSE existing.source
                           END,
                           confidence = CASE
                               WHEN EXCLUDED.last_seen_at >= existing.last_seen_at
                               THEN EXCLUDED.confidence ELSE existing.confidence
                           END,
                           updated_at = now()
                       RETURNING *""",
                values,
            ).fetchone()
        return cast(dict[str, Any], row)

    def list_gateway_instance_observations(
        self, deployment_id: UUID
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_instance_observation
                   WHERE deployment_id = %s
                   ORDER BY last_seen_at DESC, native_instance_id""",
                (deployment_id,),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def upsert_gateway_deployment_inventory(
        self, inventory: GatewayDeploymentInventory
    ) -> dict[str, Any]:
        values = inventory.model_dump(mode="python")
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """INSERT INTO gateway_deployment_inventory AS existing (
                           deployment_id, source_api_version, synced_at
                       ) VALUES (
                           %(deployment_id)s, %(source_api_version)s, %(synced_at)s
                       ) ON CONFLICT (deployment_id) DO UPDATE SET
                           source_api_version = EXCLUDED.source_api_version,
                           synced_at = EXCLUDED.synced_at,
                           updated_at = now()
                       WHERE EXCLUDED.synced_at >= existing.synced_at
                       RETURNING *""",
                values,
            ).fetchone()
            if row is None:
                current = connection.execute(
                    """SELECT * FROM gateway_deployment_inventory
                       WHERE deployment_id = %s""",
                    (inventory.deployment_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError(
                        "Gateway deployment inventory upsert returned no row"
                    )
                return self._deployment_inventory_view(connection, current)
            self._write_gateway_inventory_collections(connection, inventory)
            return self._deployment_inventory_view(connection, row)

    @staticmethod
    def _write_gateway_inventory_collections(
        connection: Any,
        inventory: GatewayDeploymentInventory,
    ) -> None:
        for table in (
            "gateway_deployment_api_association",
            "gateway_deployment_hostname_inventory",
            "gateway_deployment_ca_inventory",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE deployment_id = %s",
                (inventory.deployment_id,),
            )
        with connection.cursor() as cursor:
            if inventory.associated_api_ids:
                cursor.executemany(
                    """INSERT INTO gateway_deployment_api_association
                       (deployment_id, api_id) VALUES (%s, %s)""",
                    [(inventory.deployment_id, api_id) for api_id in inventory.associated_api_ids],
                )
            if inventory.hostname_configurations:
                cursor.executemany(
                    """INSERT INTO gateway_deployment_hostname_inventory (
                       deployment_id, configuration_id, name, hostname,
                       certificate_id, negotiate_client_certificate
                       ) VALUES (%s, %s, %s, %s, %s, %s)""",
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
                       (deployment_id, authority_id, name, is_trusted) VALUES (%s, %s, %s, %s)""",
                    [
                        (inventory.deployment_id, item.authority_id, item.name, item.is_trusted)
                        for item in inventory.certificate_authorities
                    ],
                )

    def get_gateway_deployment_inventory(
        self, deployment_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM gateway_deployment_inventory
                   WHERE deployment_id = %s""",
                (deployment_id,),
            ).fetchone()
            return (
                self._deployment_inventory_view(connection, row)
                if row is not None
                else None
            )

    def list_gateway_deployment_inventories(
        self, deployment_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        if not deployment_ids:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT inventory.*,
                       COALESCE((SELECT jsonb_agg(association.api_id
                           ORDER BY lower(association.api_id), association.api_id)
                           FROM gateway_deployment_api_association association
                           WHERE association.deployment_id = inventory.deployment_id),
                           '[]'::jsonb) AS associated_api_ids,
                       COALESCE((SELECT jsonb_agg(jsonb_build_object(
                           'configuration_id', hostname.configuration_id,
                           'name', hostname.name, 'hostname', hostname.hostname,
                           'certificate_id', hostname.certificate_id,
                           'negotiate_client_certificate', hostname.negotiate_client_certificate)
                           ORDER BY lower(hostname.name), hostname.configuration_id)
                           FROM gateway_deployment_hostname_inventory hostname
                           WHERE hostname.deployment_id = inventory.deployment_id),
                           '[]'::jsonb) AS hostname_configurations,
                       COALESCE((SELECT jsonb_agg(jsonb_build_object(
                           'authority_id', authority.authority_id, 'name', authority.name,
                           'is_trusted', authority.is_trusted)
                           ORDER BY lower(authority.name), authority.authority_id)
                           FROM gateway_deployment_ca_inventory authority
                           WHERE authority.deployment_id = inventory.deployment_id),
                           '[]'::jsonb) AS certificate_authorities
                   FROM gateway_deployment_inventory inventory
                   WHERE inventory.deployment_id = ANY(%s)
                   ORDER BY inventory.deployment_id""",
                (list(deployment_ids),),
            ).fetchall()
        return [
            {
                **row,
                "associated_api_ids": tuple(row["associated_api_ids"]),
                "hostname_configurations": tuple(
                    dict(item) for item in row["hostname_configurations"]
                ),
                "certificate_authorities": tuple(
                    dict(item) for item in row["certificate_authorities"]
                ),
            }
            for row in rows
        ]

    @staticmethod
    def _deployment_inventory_view(
        connection: Any, row: dict[str, Any]
    ) -> dict[str, Any]:
        deployment_id = row["deployment_id"]
        associated = connection.execute(
            """SELECT api_id FROM gateway_deployment_api_association
               WHERE deployment_id = %s ORDER BY lower(api_id), api_id""",
            (deployment_id,),
        ).fetchall()
        hostnames = connection.execute(
            """SELECT configuration_id, name, hostname, certificate_id,
                      negotiate_client_certificate
               FROM gateway_deployment_hostname_inventory
               WHERE deployment_id = %s ORDER BY lower(name), configuration_id""",
            (deployment_id,),
        ).fetchall()
        authorities = connection.execute(
            """SELECT authority_id, name, is_trusted
               FROM gateway_deployment_ca_inventory
               WHERE deployment_id = %s ORDER BY lower(name), authority_id""",
            (deployment_id,),
        ).fetchall()
        return {
            **row,
            "associated_api_ids": tuple(item["api_id"] for item in associated),
            "hostname_configurations": tuple(dict(item) for item in hostnames),
            "certificate_authorities": tuple(dict(item) for item in authorities),
        }

    def upsert_apim_target_route_binding(
        self, binding: ApimTargetRouteBinding
    ) -> dict[str, Any]:
        values = binding.model_dump(mode="json")
        scope = values.pop("deployment_scope")
        values["deployment_scope_mode"] = scope["mode"]
        deployment_ids = list(binding.deployment_scope.deployment_ids)
        with self._connection() as connection, connection.transaction():
            runtime = connection.execute(
                """SELECT id FROM model_runtime
                   WHERE id = %s AND gateway_profile_id = %s""",
                (binding.runtime_id, binding.apim_target_id),
            ).fetchone()
            if runtime is None:
                raise ValueError("Route Runtime does not belong to the APIM target")
            if binding.current_publication_id is not None:
                publication = connection.execute(
                    """SELECT id FROM gateway_publication
                       WHERE id = %s AND gateway_profile_id = %s""",
                    (binding.current_publication_id, binding.apim_target_id),
                ).fetchone()
                if publication is None:
                    raise ValueError("Route publication does not belong to the APIM target")
            if binding.deployment_scope.mode is GatewayDeploymentScopeMode.SELECTED:
                deployments = connection.execute(
                    """SELECT id FROM gateway_deployment
                       WHERE apim_target_id = %s
                         AND id = ANY(%s)
                         AND retired_at IS NULL""",
                    (binding.apim_target_id, deployment_ids),
                ).fetchall()
                if {row["id"] for row in deployments} != set(deployment_ids):
                    raise ValueError(
                        "Selected Gateway deployments must be active and belong to the APIM target"
                    )
            row = connection.execute(
                """INSERT INTO apim_target_route_binding AS existing (
                           schema_version, id, logical_model_id, apim_target_id,
                           runtime_id, enabled, is_default_serving,
                           deployment_scope_mode, current_publication_id,
                           capability_state
                       ) VALUES (
                           %(schema_version)s, %(id)s, %(logical_model_id)s,
                           %(apim_target_id)s, %(runtime_id)s, %(enabled)s,
                           %(is_default_serving)s, %(deployment_scope_mode)s,
                           %(current_publication_id)s, %(capability_state)s
                       ) ON CONFLICT (id) DO UPDATE SET
                           runtime_id = EXCLUDED.runtime_id,
                           enabled = EXCLUDED.enabled,
                           is_default_serving = EXCLUDED.is_default_serving,
                           deployment_scope_mode = EXCLUDED.deployment_scope_mode,
                           current_publication_id = EXCLUDED.current_publication_id,
                           capability_state = EXCLUDED.capability_state,
                           updated_at = now()
                       WHERE existing.logical_model_id = EXCLUDED.logical_model_id
                         AND existing.apim_target_id = EXCLUDED.apim_target_id
                       RETURNING *""",
                values,
            ).fetchone()
            if row is None:
                raise ValueError("APIM target route identity cannot be changed in place")
            connection.execute(
                """DELETE FROM apim_target_route_binding_deployment
                   WHERE route_binding_id = %s""",
                (binding.id,),
            )
            if deployment_ids:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO apim_target_route_binding_deployment (
                                   route_binding_id, apim_target_id, gateway_deployment_id
                               ) VALUES (%s, %s, %s)""",
                        [
                            (binding.id, binding.apim_target_id, deployment_id)
                            for deployment_id in deployment_ids
                        ],
                    )
            result = self._route_binding_view(connection, row)
        return result

    def get_apim_target_route_binding(
        self, binding_id: UUID
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM apim_target_route_binding WHERE id = %s",
                (binding_id,),
            ).fetchone()
            if row is None:
                return None
            return self._route_binding_view(connection, row)

    def list_apim_target_route_bindings(
        self,
        *,
        logical_model_id: UUID | None = None,
        apim_target_id: UUID | None = None,
        apim_target_ids: Sequence[UUID] | None = None,
    ) -> Sequence[dict[str, Any]]:
        if apim_target_ids is not None and not apim_target_ids:
            return []
        clauses: list[str] = []
        parameters: list[UUID | list[UUID]] = []
        if logical_model_id is not None:
            clauses.append("binding.logical_model_id = %s")
            parameters.append(logical_model_id)
        if apim_target_id is not None:
            clauses.append("binding.apim_target_id = %s")
            parameters.append(apim_target_id)
        if apim_target_ids is not None:
            clauses.append("binding.apim_target_id = ANY(%s)")
            parameters.append(list(apim_target_ids))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT binding.*, ARRAY(
                        SELECT membership.gateway_deployment_id
                        FROM apim_target_route_binding_deployment membership
                        WHERE membership.route_binding_id = binding.id
                        ORDER BY membership.gateway_deployment_id
                    ) AS deployment_ids
                    FROM apim_target_route_binding binding
                    {where}
                    ORDER BY binding.logical_model_id, binding.apim_target_id, binding.id""",  # noqa: S608
                parameters,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            mode = item.pop("deployment_scope_mode")
            deployment_ids = item.pop("deployment_ids")
            item["deployment_scope"] = {"mode": mode, "deployment_ids": tuple(deployment_ids)}
            result.append(item)
        return result

    @staticmethod
    def _route_binding_view(connection: Any, row: dict[str, Any]) -> dict[str, Any]:
        deployment_rows = connection.execute(
            """SELECT gateway_deployment_id
               FROM apim_target_route_binding_deployment
               WHERE route_binding_id = %s
               ORDER BY gateway_deployment_id""",
            (row["id"],),
        ).fetchall()
        result = dict(row)
        mode = result.pop("deployment_scope_mode")
        result["deployment_scope"] = {
            "mode": mode,
            "deployment_ids": tuple(item["gateway_deployment_id"] for item in deployment_rows),
        }
        return result

    def upsert_usage_gateway_attribution(
        self, attribution: UsageGatewayAttribution
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.transaction():
            self._write_usage_gateway_attribution(connection, attribution)
            row = connection.execute(
                """SELECT * FROM token_usage_gateway_attribution
                   WHERE usage_id = %s""",
                (attribution.usage_id,),
            ).fetchone()
        return cast(dict[str, Any], row)

    def get_usage_gateway_attribution(self, usage_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM token_usage_gateway_attribution
                   WHERE usage_id = %s""",
                (usage_id,),
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def _write_usage_gateway_attribution(
        self,
        connection: Any,
        attribution: UsageGatewayAttribution,
    ) -> None:
        if attribution.gateway_deployment_id is not None:
            deployment = connection.execute(
                """SELECT native_gateway_id, kind
                   FROM gateway_deployment
                   WHERE id = %s AND apim_target_id = %s""",
                (
                    attribution.gateway_deployment_id,
                    attribution.apim_target_id,
                ),
            ).fetchone()
            if deployment is None:
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
        values = attribution.model_dump(mode="json")
        row = connection.execute(
            """INSERT INTO token_usage_gateway_attribution AS existing (
                       usage_id, apim_target_id, gateway_deployment_id,
                       native_gateway_id, native_instance_id, is_managed,
                       apim_service_id, apim_region, api_revision,
                       event_schema_version, attribution_source, observed_at
                   ) VALUES (
                       %(usage_id)s, %(apim_target_id)s,
                       %(gateway_deployment_id)s, %(native_gateway_id)s,
                       %(native_instance_id)s, %(is_managed)s,
                       %(apim_service_id)s, %(apim_region)s, %(api_revision)s,
                       %(event_schema_version)s, %(attribution_source)s,
                       %(observed_at)s
                   ) ON CONFLICT (usage_id) DO UPDATE SET
                       gateway_deployment_id = COALESCE(
                           existing.gateway_deployment_id,
                           EXCLUDED.gateway_deployment_id
                       ),
                       native_instance_id = COALESCE(
                           existing.native_instance_id,
                           EXCLUDED.native_instance_id
                       ),
                       event_schema_version = GREATEST(
                           existing.event_schema_version,
                           EXCLUDED.event_schema_version
                       ),
                       attribution_source = CASE
                           WHEN CASE EXCLUDED.attribution_source
                               WHEN 'observer' THEN 4
                               WHEN 'eventhub' THEN 3
                               WHEN 'reconciliation' THEN 2
                               ELSE 1
                           END >= CASE existing.attribution_source
                               WHEN 'observer' THEN 4
                               WHEN 'eventhub' THEN 3
                               WHEN 'reconciliation' THEN 2
                               ELSE 1
                           END THEN EXCLUDED.attribution_source
                           ELSE existing.attribution_source
                       END,
                       observed_at = GREATEST(
                           existing.observed_at, EXCLUDED.observed_at
                       ),
                       updated_at = now()
                   WHERE existing.apim_target_id = EXCLUDED.apim_target_id
                     AND lower(existing.native_gateway_id)
                         = lower(EXCLUDED.native_gateway_id)
                     AND existing.is_managed = EXCLUDED.is_managed
                     AND lower(existing.apim_service_id)
                         = lower(EXCLUDED.apim_service_id)
                     AND lower(existing.apim_region) = lower(EXCLUDED.apim_region)
                     AND existing.api_revision = EXCLUDED.api_revision
                     AND (
                         existing.gateway_deployment_id IS NULL
                         OR EXCLUDED.gateway_deployment_id IS NULL
                         OR existing.gateway_deployment_id
                             = EXCLUDED.gateway_deployment_id
                     )
                     AND (
                         existing.native_instance_id IS NULL
                         OR EXCLUDED.native_instance_id IS NULL
                         OR lower(existing.native_instance_id)
                             = lower(EXCLUDED.native_instance_id)
                     )
                   RETURNING usage_id""",
            values,
        ).fetchone()
        if row is None:
            raise ValueError("Conflicting Gateway attribution for canonical usage ID")
