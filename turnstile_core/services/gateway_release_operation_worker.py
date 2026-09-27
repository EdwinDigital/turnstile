from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any, cast
from uuid import UUID, uuid4

import httpx

from ..domain.application_access import GatewayApplicationSubscriptionProvisionSpec
from ..domain.control_plane import (
    GatewayPublication,
    GatewayReleaseOperation,
    GatewayReleaseOperationKind,
    GatewayReleaseRetentionPolicy,
)
from ..domain.gateway_targets import (
    ApimTargetArmInventory,
    SelfHostedGatewayProvisionSpec,
)
from ..domain.model_availability import ModelAvailabilityPublication, ModelAvailabilityState
from ..integrations.apim_control_plane import (
    ApimPublisherClient,
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_control_plane_contract import (
    ApimPublisherClientResolver,
    ResolvedApimPublisherClient,
    coerce_apim_publisher_client_resolver,
)
from ..integrations.apim_model_availability import compose_availability_parent
from ..integrations.apim_policy_components import parse_policy, validate_parent_readback
from ..persistence.repository import QueryRepository
from ..security import CredentialCipher
from .application_access import ApplicationAccessService
from .control_plane import ControlPlaneConflictError, GatewayControlPlaneService
from .probe_journal import PersistentProbeJournal
from .worker_lease import WorkerLease


class GatewayReleaseOperationWorker:
    _TERMINAL = {"succeeded", "failed", "restored"}

    def __init__(
        self,
        repository: QueryRepository,
        client: ApimPublisherClient | ApimPublisherClientResolver,
        retention_policy: GatewayReleaseRetentionPolicy | None = None,
        application_default_token_limit: int = 100_000,
        application_default_tokens_per_minute: int = 100_000,
        cipher: CredentialCipher | None = None,
        application_projector: Callable[[UUID, UUID], None] | None = None,
        parent_policy: str | None = None,
        parent_policy_overrides: Mapping[UUID, str] | None = None,
        onboarding_handler: Callable[[Mapping[str, Any], str], None] | None = None,
        gateway_protocol_enrollment: Callable[
            [ApimTargetArmInventory, ApimPublisherClient, Callable[[], None]], tuple[str, ...]
        ] | None = None,
        self_hosted_gateway_provisioning_enabled: bool = False,
        availability_projector: Callable[[UUID], ModelAvailabilityState] | None = None,
    ) -> None:
        self._repository = repository
        self._client_resolver = coerce_apim_publisher_client_resolver(client)
        self._active_client: ContextVar[ApimPublisherClient | None] = ContextVar(
            "release-operation-client",
            default=(None if callable(getattr(client, "resolve", None))
                     else cast(ApimPublisherClient, client)),
        )
        self._cipher = cipher
        self._application_projector = application_projector
        self._availability_projector = availability_projector
        self._onboarding_handler = onboarding_handler
        self._gateway_protocol_enrollment = gateway_protocol_enrollment
        self._self_hosted_gateway_provisioning_enabled = self_hosted_gateway_provisioning_enabled
        self._parent_policy = parent_policy
        self._parent_policy_overrides = dict(parent_policy_overrides or {})
        self._service = GatewayControlPlaneService(
            repository,
            retention_policy=retention_policy,
        )
        self._application_service = ApplicationAccessService(
            repository,
            sync_available=True,
            default_token_limit=application_default_token_limit,
            default_tokens_per_minute=application_default_tokens_per_minute,
        )

    @property
    def _client(self) -> ApimPublisherClient:
        client = self._active_client.get()
        if client is None:
            raise RuntimeError("Release operation has no resolved APIM target")
        return client

    def _validate_operation_target_identity(
        self, row: Mapping[str, Any], resolved: ResolvedApimPublisherClient,
    ) -> None:
        gateway_id = UUID(str(row["gateway_profile_id"]))
        if resolved.gateway_profile_id != gateway_id:
            raise ControlPlaneConflictError("APIM client resolved another Release operation target")
        for field in ("target_release_id", "prior_release_id"):
            if row.get(field) is None:
                continue
            publication = self._service.publication(UUID(str(row[field])))
            if publication.gateway_profile_id != gateway_id:
                raise ControlPlaneConflictError("Release operation references another APIM target")
            snapshot = publication.resource_manifest.get("apim_target_identity_sha256")
            if snapshot is not None and snapshot != resolved.target_identity_sha256:
                raise ControlPlaneConflictError(
                    "APIM target identity changed after Release creation"
                )
            if snapshot is None and resolved.enforce_identity_snapshot:
                raise ControlPlaneConflictError("APIM target identity snapshot is missing")

    def run_once(
        self, worker_id: str, lease_seconds: int = 180, max_attempts: int = 30
    ) -> GatewayReleaseOperation | None:
        worker_id = f"{worker_id}:{uuid4().hex}"
        row = self._repository.claim_gateway_release_operation(
            worker_id, lease_seconds
        )
        if row is None:
            return None
        operation_id = UUID(str(row["id"]))
        if (
            int(row["attempt_count"]) > max_attempts
            and row["status"] != "restoring"
        ):
            self._handle_attempt_exhaustion(row, worker_id)
            return self._service.release_operation(operation_id)
        try:
            with WorkerLease(
                lambda: self._repository.renew_gateway_release_operation_lease(
                    operation_id, worker_id, lease_seconds
                ),
                lease_seconds,
            ):
                if row["operation_kind"] == GatewayReleaseOperationKind.APIM_ONBOARDING:
                    if self._onboarding_handler is None:
                        raise RuntimeError("APIM initialization is not enabled on this Worker")
                    self._onboarding_handler(row, worker_id)
                    return self._service.release_operation(operation_id)
                resolved = self._client_resolver.resolve(UUID(str(row["gateway_profile_id"])))
                self._validate_operation_target_identity(row, resolved)
                token = self._active_client.set(resolved.client)
                try:
                    self._advance(row, worker_id)
                finally:
                    self._active_client.reset(token)
        except (RetryablePublicationError, httpx.TransportError) as error:
            self._retry(row, error, worker_id)
        except Exception as error:
            self._fail_or_restore(row, error, worker_id)
        return self._service.release_operation(operation_id)

    def _advance(self, row: Mapping[str, Any], worker_id: str) -> None:
        kind = GatewayReleaseOperationKind(str(row["operation_kind"]))
        if kind is GatewayReleaseOperationKind.INTEGRITY_CHECK:
            self._advance_integrity(row, worker_id)
        elif kind is GatewayReleaseOperationKind.ROLLBACK:
            self._advance_rollback(row, worker_id)
        elif kind is GatewayReleaseOperationKind.GC_PLAN:
            self._advance_gc_plan(row, worker_id)
        elif kind is GatewayReleaseOperationKind.APPLICATION_SYNC:
            self._advance_application_sync(row, worker_id)
        elif kind is GatewayReleaseOperationKind.APPLICATION_PROVISION:
            self._advance_application_provision(row, worker_id)
        elif kind is GatewayReleaseOperationKind.TARGET_INVENTORY_SYNC:
            self._advance_target_inventory_sync(row, worker_id)
        elif kind is GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION:
            self._advance_self_hosted_gateway_provision(row, worker_id)

    def _advance_self_hosted_gateway_provision(
        self, row: Mapping[str, Any], worker_id: str,
    ) -> None:
        if not self._self_hosted_gateway_provisioning_enabled:
            raise PolicyCompilationError(
                "Self-hosted Gateway creation is not enabled on this Worker",
            )
        spec = SelfHostedGatewayProvisionSpec.model_validate(row["semantic_preview"])
        if spec.target.gateway_profile_id != UUID(str(row["gateway_profile_id"])):
            raise ControlPlaneConflictError("Gateway creation references another APIM target")
        target = self._service._apim_target(
            self._service._active_apim_target_row(spec.target.gateway_profile_id),
        )
        if (target.ownership_mode == "read_only"
                or target.arm_resource_id.casefold() != spec.target.arm_resource_id.casefold()
                or target.tenant_id != spec.target.tenant_id
                or target.api_id != spec.target.api_id or target.api_path != spec.target.api_path):
            raise ControlPlaneConflictError("Gateway creation target changed or is read-only")
        if row["status"] == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return

        def before_write() -> None:
            if not self._repository.renew_gateway_release_operation_lease(
                UUID(str(row["id"])), worker_id, 180,
            ):
                raise RetryablePublicationError("Worker lease is no longer owned")

        if row["status"] == "validating_dependencies":
            create = getattr(self._client, "ensure_self_hosted_gateway", None)
            if not callable(create):
                raise PolicyCompilationError("APIM client does not support Gateway creation")
            create(spec, before_write)
            self._transition(row, "verifying_readback", {"checkpoint": self._checkpoint(
                row, native_gateway_id=spec.native_gateway_id,
                deployment_id=str(spec.deployment_id), apim_gateway_created=True,
                api_associated=True,
            )}, worker_id)
            return
        if row["status"] != "verifying_readback":
            return
        discover = getattr(self._client, "discover_apim_target_inventory", None)
        if not callable(discover):
            raise PolicyCompilationError("APIM client does not support target inventory")
        inventory = ApimTargetArmInventory.model_validate(discover(spec.target.gateway_profile_id))
        gateway = next((item for item in inventory.gateways
                        if item.native_gateway_id == spec.native_gateway_id), None)
        if gateway is None or spec.target.api_id not in gateway.associated_api_ids:
            raise RetryablePublicationError("Created Gateway API association is not visible yet")
        expected_id = spec.target.arm_resource_id + "/gateways/" + spec.native_gateway_id
        if (gateway.arm_resource_id.casefold() != expected_id.casefold()
                or gateway.description
                != f"Turnstile gateway {spec.target.gateway_profile_id}/{spec.request_id}"):
            raise ControlPlaneConflictError("Created Gateway ownership changed before activation")
        before_write()
        self._service.apply_target_inventory(inventory)
        current = self._repository.get_gateway_deployment(spec.deployment_id)
        if current is None or current.get("retired_at") is not None:
            raise ControlPlaneConflictError("Created Gateway was removed before activation")
        operation_id = UUID(str(row["id"]))
        if current.get("provisioning_operation_id") not in {None, operation_id}:
            raise ControlPlaneConflictError("Gateway is already owned by another creation request")
        deployment = self._service._gateway_deployment(current).model_copy(update={
            "display_name": spec.configuration.display_name,
            "endpoint_url": spec.configuration.endpoint_url,
            "location": spec.configuration.location or spec.configuration.display_name,
            "hosting_mode": spec.configuration.hosting_mode,
            "release_gate": spec.configuration.release_gate,
            "provisioning_operation_id": operation_id,
        })
        before_write()
        self._repository.upsert_gateway_deployment(deployment)
        enrolled = self._gateway_protocol_enrollment(
            inventory, self._client, before_write,
        ) if self._gateway_protocol_enrollment is not None else None
        self._transition(row, "succeeded", {"checkpoint": self._checkpoint(
            row, deployment_id=str(spec.deployment_id), native_gateway_id=spec.native_gateway_id,
            configuration_ready=True, waiting_for_container=True, gateway_token_stored=False,
            **({"protocol_enrolled_gateways": list(enrolled)} if enrolled is not None else {}),
        )}, worker_id)

    def _advance_target_inventory_sync(self, row: Mapping[str, Any], worker_id: str) -> None:
        if row["status"] == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if row["status"] != "validating_dependencies":
            return
        discovery = getattr(self._client, "discover_apim_target_inventory", None)
        if not callable(discovery):
            raise PolicyCompilationError("APIM client does not support target inventory")
        inventory = ApimTargetArmInventory.model_validate(
            discovery(UUID(str(row["gateway_profile_id"])))
        )

        def before_write() -> None:
            if not self._repository.renew_gateway_release_operation_lease(
                UUID(str(row["id"])), worker_id, 180,
            ):
                raise RetryablePublicationError("Worker lease is no longer owned")

        before_write()
        topology = self._service.apply_target_inventory(inventory)
        enrolled = self._gateway_protocol_enrollment(
            inventory, self._client, before_write,
        ) if self._gateway_protocol_enrollment is not None else None
        self._transition(row, "succeeded", {"checkpoint": {
            "read_only_arm_discovery": True, "service_tier": inventory.service_tier,
            "gateway_count": len(topology.deployments),
            "self_hosted_gateway_count": len(inventory.gateways),
            "associated_api_count": sum(
                len(item.associated_api_ids) for item in inventory.gateways
            ),
            "hostname_configuration_count": sum(
                len(item.hostname_configurations) for item in inventory.gateways
            ),
            "certificate_authority_count": sum(
                len(item.certificate_authorities) for item in inventory.gateways
            ),
            **({"protocol_enrolled_gateways": list(enrolled)} if enrolled is not None else {}),
        }}, worker_id)

    def _advance_integrity(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        target = self._target(row)
        dependencies = self._client.inspect_revision_dependencies(target)
        self._repository.save_gateway_release_integrity_snapshot(
            target.id,
            UUID(str(row["id"])),
            dependencies.live_status,
            dependencies.model_dump(mode="json"),
            dependencies.issues,
            worker_id,
        )
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": {
                    "integrity_status": dependencies.live_status,
                    "issue_count": len(dependencies.issues),
                }
            },
            worker_id,
        )

    def _advance_application_sync(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        gateway_profile_id = UUID(str(row["gateway_profile_id"]))
        discovery = self._client.discover_gateway_applications(gateway_profile_id)
        synchronized = self._application_service.sync_discovery(discovery, worker_id)
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": {
                    "application_count": len(synchronized),
                    "system_application_count": sum(
                        item.system_managed for item in discovery.items
                    ),
                    "stale_subscription_count": sum(
                        not item.scope_exists for item in discovery.items
                    ),
                    "read_only_apim_discovery": True,
                }
            },
            worker_id,
        )

    def _advance_application_provision(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        operation_id = UUID(str(row["id"]))
        spec = GatewayApplicationSubscriptionProvisionSpec.model_validate(
            row["semantic_preview"]
        )
        if spec.provisioning_version == 2 and spec.gateway_profile_id != UUID(
            str(row["gateway_profile_id"])
        ):
            raise ControlPlaneConflictError("Application creation references another gateway")
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status == "validating_dependencies":
            if spec.provisioning_version == 2 and self._application_projector is None:
                raise RuntimeError("Application admission ledger projection is not configured")
            if self._cipher is None:
                raise RuntimeError("Credential encryption is unavailable")
            ciphertext = self._repository.gateway_release_operation_secret(
                operation_id
            )
            secret = self._cipher.decrypt(ciphertext)
            if secret is None:
                raise RuntimeError("Application provisioning credential is missing")
            values = json.loads(secret)
            primary_key = values.get("primary_key")
            secondary_key = values.get("secondary_key")
            if not isinstance(primary_key, str) or not isinstance(
                secondary_key, str
            ):
                raise RuntimeError("Application provisioning credential is invalid")
            self._client.ensure_application_subscription(
                spec, primary_key, secondary_key
            )
            self._transition(
                row,
                "promoting",
                {
                    "checkpoint": self._checkpoint(
                        row, apim_subscription_created=True
                    )
                },
                worker_id,
            )
            return
        if status == "verifying_readback" and spec.provisioning_version == 2:
            if self._application_projector is None:
                raise RuntimeError("Application admission ledger projection is not configured")
            try:
                self._application_projector(
                    UUID(str(row["gateway_profile_id"])), spec.application_id
                )
            except (ControlPlaneConflictError, PolicyCompilationError):
                raise
            except Exception as error:
                raise RetryablePublicationError(
                    "Application admission ledger is not yet ready"
                ) from error
            if self._cipher is None:
                raise RuntimeError("Credential encryption is unavailable")
            secret = self._cipher.decrypt(
                self._repository.gateway_release_operation_secret(operation_id)
            )
            if secret is None:
                raise RuntimeError("Application provisioning credential is missing")
            keys = json.loads(secret)
            self._client.activate_application_subscription(
                spec, keys["primary_key"], keys["secondary_key"]
            )
            self._transition(row, "succeeded", {"checkpoint": self._checkpoint(
                row, admission_ready=True, subscription_active=True,
                data_plane_authentication_ready=True, key_stored=False,
            )}, worker_id)
            return
        if status != "promoting":
            return
        try:
            application = self._application_service.provision(
                UUID(str(row["gateway_profile_id"])),
                spec,
                str(row["created_by"]),
            )
        except ValueError:
            raise
        except Exception as error:
            raise RetryablePublicationError(
                "Application materialization is temporarily unavailable"
            ) from error
        if spec.provisioning_version == 2:
            self._transition(row, "verifying_readback", {"checkpoint": self._checkpoint(
                row,
                application_id=str(application["id"]),
                apim_subscription_id=spec.apim_subscription_id,
                product_id=spec.scope_id,
                application_materialized=True,
            )}, worker_id)
            return
        self._transition(
            row,
            "succeeded",
            {
                "checkpoint": self._checkpoint(
                    row,
                    application_id=str(application["id"]),
                    apim_subscription_id=spec.apim_subscription_id,
                    product_id=spec.scope_id,
                    key_stored=False,
                )
            },
            worker_id,
        )

    def _advance_rollback(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        target = self._target(row)
        prior = self._prior(row)
        if status == "validating_dependencies":
            dependencies = self._client.inspect_revision_dependencies(target)
            self._repository.save_gateway_release_integrity_snapshot(
                target.id,
                UUID(str(row["id"])),
                dependencies.live_status,
                dependencies.model_dump(mode="json"),
                dependencies.issues,
                worker_id,
            )
            preview = self._service.rollback_preview(target.id)
            if preview.current_release_id != prior.id:
                raise ControlPlaneConflictError(
                    "Effective gateway release changed before rollback"
                )
            if row.get("confirmation_sha256") != preview.confirmation_sha256:
                raise ControlPlaneConflictError(
                    "Rollback confirmation no longer matches the live preview"
                )
            if not preview.rollback_eligible:
                raise ControlPlaneConflictError(
                    f"Rollback dependencies are not healthy: {', '.join(preview.rollback_blockers)}"
                )
            self._transition(
                row,
                "preflight_probing",
                {
                    "checkpoint": {
                        "dependency_validation": "healthy",
                        "target_revision": target.apim_revision,
                        "prior_revision": prior.apim_revision,
                    }
                },
                worker_id,
            )
            return
        if status == "preflight_probing":
            target_revision = self._revision(target)
            self._probe_release(row, target, worker_id)
            self._transition(
                row,
                "promoting",
                {"checkpoint": self._checkpoint(row, preflight_probe="passed")},
                worker_id,
            )
            return
        if status == "promoting":
            target_revision = self._revision(target)
            self._validate_rollback_parent(target)
            prior_revision = self._revision(prior)
            observed = self._client.current_revision()
            if observed != prior_revision:
                raise ControlPlaneConflictError(
                    "Current APIM revision changed before rollback promotion"
                )
            self._client.promote_revision(
                target_revision, f"rollback-{row['id']}"
            )
            self._transition(
                row,
                "verifying_readback",
                {"checkpoint": self._checkpoint(row, promotion="requested")},
                worker_id,
            )
            return
        if status == "verifying_readback":
            target_revision = self._revision(target)
            if self._client.current_revision() != target_revision:
                raise RetryablePublicationError(
                    "APIM did not report the rollback target as current"
                )
            self._transition(
                row,
                "post_promotion_probing",
                {"checkpoint": self._checkpoint(row, readback="passed")},
                worker_id,
            )
            return
        if status == "post_promotion_probing":
            target_revision = self._revision(target)
            self._probe_release(row, target, worker_id)
            self._repository.complete_gateway_release_rollback(
                UUID(str(row["id"])), target.id, prior.id, worker_id
            )
            return
        if status == "restoring":
            self._restore(row, prior, worker_id)

    def _validate_rollback_parent(self, publication: GatewayPublication) -> GatewayPublication:
        canonical_parent = self._parent_policy_overrides.get(
            publication.gateway_profile_id, self._parent_policy,
        )
        availability = publication.resource_manifest.get("model_availability")
        availability_required = (
            self._repository.model_availability_supported() or availability is not None
        )
        execution_enabled = canonical_parent is not None and parse_policy(canonical_parent).find(
            "./inbound/set-variable[@name='budgetExecutionVersion']"
        ) is not None
        profiles = [
            binding.model.image_profile
            for binding in publication.desired_spec.bindings
            if binding.model.image_profile is not None
        ]
        if (not profiles and not publication.resource_manifest.get("images_generations_operation")
            and not execution_enabled
            and not publication.resource_manifest.get("budget_contracts")
            and not availability_required):
            return publication
        if canonical_parent is None:
            raise PolicyCompilationError(
            "Governed rollback requires the canonical parent policy contract"
            )
        observed = self._client.revision_api_policy(self._revision(publication))
        if availability_required:
            if availability is None or parse_policy(observed).find(
                "inbound/set-variable[@name='availabilityProtocolVersion']",
            ) is None:
                raise PolicyCompilationError(
                    "Rollback cannot remove model availability enforcement"
                )
            snapshot = ModelAvailabilityPublication.model_validate(availability)
            snapshot.validate_publication(publication)
            if self._availability_projector is None:
                raise PolicyCompilationError("Rollback availability projection is unavailable")
            probe_states = {}
            for alias, model_id in snapshot.models.items():
                projected = self._availability_projector(model_id)
                if projected.model_id != model_id:
                    raise PolicyCompilationError("Rollback model availability identity changed")
                probe_states[alias] = projected.enabled
            publication = publication.model_copy(update={"resource_manifest": {
                **publication.resource_manifest, "availability_probe_states": probe_states,
            }})
            canonical_parent = compose_availability_parent(canonical_parent)
        validate_parent_readback(
            observed,
            canonical_parent,
            profiles,
            expected_contract=publication.resource_manifest.get("parent_policy_contract_sha256"),
            expected_raw=publication.resource_manifest.get("parent_policy_sha256"),
        )
        return publication

    def _probe_release(
        self,
        row: Mapping[str, Any],
        publication: GatewayPublication,
        worker_id: str,
    ) -> None:
        publication = self._validate_rollback_parent(publication)
        if not any(binding.model.image_profile for binding in publication.desired_spec.bindings):
            self._client.probe_revision(self._revision(publication), publication)
            return
        operation_id = UUID(str(row["id"]))

        def heartbeat() -> None:
            if not self._repository.renew_gateway_release_operation_lease(
                operation_id, worker_id, 180
            ):
                raise RetryablePublicationError("Worker lease is no longer owned")

        journal = PersistentProbeJournal(
            self._repository,
            f"release-operation:{operation_id}:{row['status']}",
            operation_id,
            heartbeat=heartbeat,
        )
        self._client.probe_revision(self._revision(publication), publication, journal=journal)

    def _advance_gc_plan(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        if status == "queued":
            self._transition(row, "validating_dependencies", {}, worker_id)
            return
        if status != "validating_dependencies":
            return
        operation_id = UUID(str(row["id"]))
        existing = self._repository.get_gateway_release_gc_plan(operation_id)
        if existing is None:
            gateway_id = UUID(str(row["gateway_profile_id"]))
            releases = [
                GatewayPublication.model_validate(value)
                for value in self._repository.all_gateway_publications(gateway_id)
            ]
            retained_ids = self._service.retained_release_ids(gateway_id)
            evidence = self._client.plan_release_garbage_collection(
                releases, {str(value) for value in retained_ids}
            )
            self._repository.save_gateway_release_gc_plan(
                operation_id,
                gateway_id,
                sorted(retained_ids, key=str),
                evidence.current_non_release_references,
                evidence.candidates,
                evidence.reference_graph_sha256,
                worker_id,
            )
        self._transition(
            row,
            "succeeded",
            {"checkpoint": {"dry_run_only": True}},
            worker_id,
        )

    def _restore(
        self,
        row: Mapping[str, Any],
        prior: GatewayPublication,
        worker_id: str,
    ) -> None:
        prior_revision = self._revision(prior)
        self._validate_rollback_parent(prior)
        if self._client.current_revision() != prior_revision:
            self._client.promote_revision(
                prior_revision, f"restore-{row['id']}"
            )
        if self._client.current_revision() != prior_revision:
            raise RetryablePublicationError(
                "APIM did not report the restored revision as current"
            )
        self._probe_release(row, prior, worker_id)
        self._transition(
            row,
            "restored",
            {
                "checkpoint": self._checkpoint(
                    row, restoration="passed", effective_pointer="unchanged"
                )
            },
            worker_id,
        )

    def _fail_or_restore(
        self, row: Mapping[str, Any], error: Exception, worker_id: str
    ) -> None:
        status = str(row["status"])
        updates = self._error_updates(row, error)
        if row["operation_kind"] in {
            GatewayReleaseOperationKind.APPLICATION_PROVISION,
            GatewayReleaseOperationKind.APIM_ONBOARDING,
            GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION,
        }:
            self._transition(row, "failed", updates, worker_id)
            return
        if status in {
            "promoting",
            "verifying_readback",
            "post_promotion_probing",
        } or status == "restoring":
            self._transition(row, "restoring", updates, worker_id)
        else:
            self._transition(row, "failed", updates, worker_id)

    def _retry(
        self, row: Mapping[str, Any], error: Exception, worker_id: str
    ) -> None:
        self._transition(row, str(row["status"]), self._error_updates(row, error), worker_id)

    def _handle_attempt_exhaustion(
        self, row: Mapping[str, Any], worker_id: str
    ) -> None:
        status = str(row["status"])
        if row["operation_kind"] in {
            GatewayReleaseOperationKind.APPLICATION_PROVISION,
            GatewayReleaseOperationKind.APIM_ONBOARDING,
            GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION,
        }:
            next_status = "failed"
        elif status in {
            "promoting",
            "verifying_readback",
            "post_promotion_probing",
        }:
            next_status = "restoring"
        else:
            next_status = "failed"
        self._transition(
            row,
            next_status,
            {
                "error_code": "max_attempts_exceeded",
                "error_message": "Gateway release operation exceeded its retry limit",
                "checkpoint": self._checkpoint(row, max_attempts_exceeded=True),
            },
            worker_id,
        )

    def _transition(
        self,
        row: Mapping[str, Any],
        status: str,
        updates: Mapping[str, Any],
        worker_id: str,
    ) -> None:
        if (
            row["operation_kind"] in {
                GatewayReleaseOperationKind.APPLICATION_PROVISION,
                GatewayReleaseOperationKind.SELF_HOSTED_GATEWAY_PROVISION,
            }
            and status != str(row["status"])
        ):
            updates = {"error_code": None, "error_message": None, **updates}
        transitioned = self._repository.transition_gateway_release_operation(
            UUID(str(row["id"])),
            str(row["status"]),
            status,
            updates,
            worker_id,
        )
        if transitioned is None:
            raise RuntimeError("Gateway release operation changed while leased")

    def _target(self, row: Mapping[str, Any]) -> GatewayPublication:
        if row.get("target_release_id") is None:
            raise ControlPlaneConflictError("Release operation has no target release")
        publication = self._publication(UUID(str(row["target_release_id"])))
        self._require_operation_gateway(row, publication)
        return publication

    def _prior(self, row: Mapping[str, Any]) -> GatewayPublication:
        if row.get("prior_release_id") is None:
            raise ControlPlaneConflictError("Rollback operation has no prior release")
        publication = self._publication(UUID(str(row["prior_release_id"])))
        self._require_operation_gateway(row, publication)
        return publication

    def _publication(self, publication_id: UUID) -> GatewayPublication:
        row = self._repository.get_gateway_publication(publication_id)
        if row is None:
            raise ControlPlaneConflictError("Gateway release no longer exists")
        return GatewayPublication.model_validate(row)

    @staticmethod
    def _require_operation_gateway(
        row: Mapping[str, Any], publication: GatewayPublication
    ) -> None:
        if publication.gateway_profile_id != UUID(str(row["gateway_profile_id"])):
            raise ControlPlaneConflictError(
                "Gateway release operation references another gateway"
            )

    @staticmethod
    def _revision(publication: GatewayPublication) -> str:
        if not publication.apim_revision:
            raise ControlPlaneConflictError("Gateway release has no APIM revision")
        return publication.apim_revision

    @staticmethod
    def _checkpoint(row: Mapping[str, Any], **updates: object) -> dict[str, object]:
        current = row.get("checkpoint")
        return {**(dict(current) if isinstance(current, Mapping) else {}), **updates}

    def _error_updates(
        self, row: Mapping[str, Any], error: Exception
    ) -> dict[str, object]:
        return {
            "error_code": type(error).__name__,
            "error_message": str(error).replace("\n", " ")[:1000],
            "checkpoint": self._checkpoint(row, last_failed_status=row["status"]),
        }