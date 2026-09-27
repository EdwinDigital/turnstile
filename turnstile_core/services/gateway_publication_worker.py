from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import httpx

from ..domain.budget_counting import BudgetPublicationContracts
from ..domain.control_plane import (
    ApiFormat,
    GatewayPublication,
    ImageProbeAuthorization,
    PublicationKind,
    PublicationStatus,
    publication_credential_evidence,
    publication_credential_states,
    publication_model_id,
)
from ..domain.image_profiles import validate_image_profile
from ..domain.model_availability import ModelAvailabilityPublication, ModelAvailabilityState
from ..integrations.apim_control_plane import (
    ApimPolicyCompiler,
    ApimPublisherClient,
    AuthorizationRequiredError,
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_control_plane_contract import (
    ApimPublisherClientResolver,
    CredentialSourceMissingError,
    PublisherPermissionError,
    ResolvedApimPublisherClient,
    coerce_apim_publisher_client_resolver,
)
from ..integrations.apim_model_availability import compose_availability_parent
from ..integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    policy_document_digest,
    validate_parent_policy,
    validate_parent_readback,
)
from ..persistence.repository import QueryRepository
from ..security import CredentialCipher
from .control_plane import GatewayControlPlaneService
from .model_availability import ModelAvailabilityRepository
from .probe_journal import PersistentProbeJournal
from .worker_lease import WorkerLease


class GatewayPublicationWorker:
    _NEXT_STATUS = {
        "queued": "validating",
        "validating": "provisioning",
        "provisioning": "building_revision",
        "building_revision": "verifying",
        "verifying": "promoting",
    }

    def __init__(
        self,
        repository: QueryRepository,
        client: ApimPublisherClient | ApimPublisherClientResolver,
        parent_policy: str,
        compiler: ApimPolicyCompiler | None = None,
        cipher: CredentialCipher | None = None,
        chat_completions_operation_id: str = "chat-completions",
        responses_operation_id: str = "responses",
        responses_compact_operation_id: str = "responses-compact",
        messages_operation_id: str = "anthropic-messages",
        count_tokens_operation_id: str = "anthropic-count-tokens",
        models_operation_id: str = "anthropic-models",
        parent_policy_overrides: Mapping[UUID, str] | None = None,
        budget_configuration: Callable[[UUID], Mapping[str, object]] | None = None,
        availability_projector: Callable[[UUID], ModelAvailabilityState] | None = None,
    ) -> None:
        self._repository = repository
        self._client_resolver = coerce_apim_publisher_client_resolver(client)
        self._parent_policy = parent_policy
        self._parent_policy_overrides = dict(parent_policy_overrides or {})
        self._budget_configuration = budget_configuration
        self._availability_projector = availability_projector
        self._compiler = compiler or ApimPolicyCompiler()
        self._cipher = cipher
        self._chat_completions_operation_id = chat_completions_operation_id
        self._responses_operation_id = responses_operation_id
        self._responses_compact_operation_id = responses_compact_operation_id
        self._messages_operation_id = messages_operation_id
        self._count_tokens_operation_id = count_tokens_operation_id
        self._models_operation_id = models_operation_id

    def run_once(
        self, worker_id: str, lease_seconds: int = 180, max_attempts: int = 30
    ) -> GatewayPublication | None:
        worker_id = f"{worker_id}:{uuid4().hex}"
        claimed = self._repository.claim_gateway_publication(worker_id, lease_seconds)
        if claimed is None:
            return None
        publication = GatewayPublication.model_validate(claimed)
        client: ApimPublisherClient | None = None
        observed_credentials: dict[str, Literal["present", "missing", "unverified"]] = {}

        def failure_updates(code: str, message: str) -> dict[str, Any]:
            updates: dict[str, Any] = {"error_code": code, "error_message": message}
            if observed_credentials:
                states = {**(publication_credential_states(publication) or {}),
                          **observed_credentials}
                updates["resource_manifest"] = {
                    **publication.resource_manifest,
                    "credential_evidence": publication_credential_evidence(publication, states),
                }
            return updates

        try:
            with WorkerLease(
                lambda: self._repository.renew_gateway_publication_lease(
                    publication.id, worker_id, lease_seconds
                ),
                lease_seconds,
            ) as lease:
                if publication.attempt_count > max_attempts and not (
                    publication.status is PublicationStatus.PROMOTING
                    and publication.apim_revision is not None
                ):
                    return self._fail_attempt_limit(publication, worker_id)
                resolved = self._client_resolver.resolve(
                    publication.gateway_profile_id,
                    include_probe_credentials=publication.status is not PublicationStatus.PROMOTING,
                )
                self._validate_target_identity(publication, resolved)
                client = resolved.client
                return self._advance(
                    publication, worker_id, resolved, lease.heartbeat,
                    max_attempts=max_attempts, observed_credentials=observed_credentials,
                )
        except AuthorizationRequiredError:
            waiting = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                "awaiting_authorization",
                {
                    "error_code": "provider_authorization_required",
                    "error_message": "Provider authorization is required",
                },
                worker_id,
            )
            return GatewayPublication.model_validate(waiting) if waiting else publication
        except (RetryablePublicationError, httpx.TransportError) as error:
            code = type(error).__name__
            message = str(error).replace("\n", " ")[:1000]
            queued = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                publication.status.value,
                failure_updates(code, message),
                worker_id,
            )
            return GatewayPublication.model_validate(queued) if queued else publication
        except Exception as error:
            code = type(error).__name__
            message = str(error).replace("\n", " ")[
                :8192 if isinstance(error, PublisherPermissionError) else 1000
            ]
            if publication.status is PublicationStatus.PROMOTING and publication.apim_revision:
                try:
                    current_revision = client.current_revision() if client is not None else None
                except Exception:
                    current_revision = None
                if current_revision is None or current_revision == publication.apim_revision:
                    queued = self._repository.transition_gateway_publication(
                        publication.id, publication.status.value, publication.status.value,
                        failure_updates(code, message), worker_id,
                    )
                    return GatewayPublication.model_validate(queued) if queued else publication
            failed = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                "failed",
                failure_updates(code, message),
                worker_id,
            )
            return GatewayPublication.model_validate(failed) if failed else publication

    def _fail_attempt_limit(
        self, publication: GatewayPublication, worker_id: str,
    ) -> GatewayPublication:
        failed = self._repository.transition_gateway_publication(
            publication.id, publication.status.value, "failed",
            {"error_code": "max_attempts_exceeded",
             "error_message": "Gateway publication exceeded its retry limit"}, worker_id,
        )
        return GatewayPublication.model_validate(failed) if failed else publication

    @staticmethod
    def _validate_target_identity(
        publication: GatewayPublication, resolved: ResolvedApimPublisherClient,
    ) -> None:
        if resolved.gateway_profile_id != publication.gateway_profile_id:
            raise PolicyCompilationError("APIM client resolved another publication target")
        snapshot = publication.resource_manifest.get("apim_target_identity_sha256")
        if snapshot is not None and snapshot != resolved.target_identity_sha256:
            raise PolicyCompilationError(
                "APIM target identity changed after publication was queued"
            )
        if (snapshot is None and resolved.enforce_identity_snapshot
                and publication.status.value != "queued"):
            raise PolicyCompilationError("APIM target identity snapshot is missing in flight")

    def _compiler_for(
        self, publication: GatewayPublication, resolved: ResolvedApimPublisherClient,
    ) -> ApimPolicyCompiler:
        compiler = self._compiler
        row = self._repository.get_apim_target(publication.gateway_profile_id)
        if row is not None and row.get("probe_subscription_id"):
            compiler = compiler.with_probe_subscription(str(row["probe_subscription_id"]))
        if resolved.usage_observer is not None:
            return compiler.with_usage_observer(*resolved.usage_observer)
        if (row is not None
            and (row.get("capabilities") or {}).get("response_observer") == "unsupported"):
            return compiler.without_usage_observer()
        return compiler

    def _advance(
        self,
        publication: GatewayPublication,
        worker_id: str,
        resolved: ResolvedApimPublisherClient,
        heartbeat: Callable[[], None] | None = None,
        *,
        max_attempts: int = 30,
        observed_credentials: dict[str, Literal["present", "missing", "unverified"]] | None = None,
    ) -> GatewayPublication:
        if observed_credentials is None:
            observed_credentials = {}
        client = resolved.client
        status = publication.status.value
        already_promoted = False
        if status == "promoting" and publication.apim_revision is not None:
            current_revision = client.current_revision()
            if current_revision is None:
                raise RetryablePublicationError("APIM current revision could not be verified")
            already_promoted = current_revision == publication.apim_revision
        if publication.attempt_count > max_attempts and not already_promoted:
            return self._fail_attempt_limit(publication, worker_id)
        registry = self._repository.registry()
        canonical_parent = self._parent_policy_overrides.get(
            publication.gateway_profile_id, self._parent_policy,
        )
        routed_publication = publication.model_copy(
            update={
                "desired_spec": GatewayControlPlaneService._reconciled_release_spec(
                    publication.desired_spec,
                    registry,
                )
            }
        )
        observed_current: tuple[str, str] | None = None
        availability_snapshot = publication.resource_manifest.get("model_availability")
        if self._repository.model_availability_supported() and availability_snapshot is None:
            if status != "queued":
                raise PolicyCompilationError("In-flight publication requires availability identity")
            observed_current = client.current_api_policy()
            if parse_policy(observed_current[1]).find(
                "inbound/set-variable[@name='availabilityProtocolVersion']",
            ) is None:
                raise PolicyCompilationError("Publication requires installed model availability")
            target = self._repository.get_apim_target(publication.gateway_profile_id)
            if target is None:
                raise PolicyCompilationError("Availability requires a registered APIM identity")
            availability_snapshot = ModelAvailabilityPublication.capture(
                routed_publication, registry, str(target["api_id"]),
            ).model_dump(mode="json")
        snapshot_availability = None
        if availability_snapshot is not None:
            snapshot_availability = ModelAvailabilityPublication.model_validate(
                availability_snapshot,
            )
            snapshot_availability.validate_publication(routed_publication)
            availability_manifest = {
                **publication.resource_manifest,
                "model_availability": snapshot_availability.model_dump(mode="json"),
            }
            publication = publication.model_copy(
                update={"resource_manifest": availability_manifest},
            )
            routed_publication = routed_publication.model_copy(
                update={"resource_manifest": availability_manifest},
            )
            canonical_parent = compose_availability_parent(canonical_parent)
            if self._availability_projector is None and not already_promoted:
                raise PolicyCompilationError("Availability publication projection is unavailable")
        execution_enabled = parse_policy(canonical_parent).find(
            "./inbound/set-variable[@name='budgetExecutionVersion']"
        ) is not None
        if execution_enabled:
            if self._budget_configuration is None:
                raise PolicyCompilationError("Budget publication configuration is unavailable")
            fence = self._budget_configuration(publication.gateway_profile_id)
            if (fence.get("protocol_version") != 2 or fence.get("mode") != "active"
                    or fence.get("gateway_profile_id") != str(publication.gateway_profile_id)):
                raise PolicyCompilationError("Budget publication requires its active Gateway fence")
            raw_snapshot = publication.resource_manifest.get("budget_contracts")
            if raw_snapshot is None:
                if status != "queued":
                    raise PolicyCompilationError(
                        "In-flight publication has no budget contract snapshot"
                    )
                previous_row = (
                    self._repository.get_gateway_publication(publication.base_release_id)
                    if publication.base_release_id is not None else None
                )
                snapshot = BudgetPublicationContracts.capture(
                    routed_publication, registry, fence,
                    GatewayPublication.model_validate(previous_row) if previous_row else None,
                    new_model_key=(publication.desired_spec.bindings[-1].model.model_key
                                   if publication.desired_spec.bindings else None),
                )
            else:
                snapshot = BudgetPublicationContracts.model_validate(raw_snapshot)
            snapshot.validate_publication(routed_publication)
            if str(snapshot.epoch) != fence.get("epoch"):
                raise PolicyCompilationError("Budget publication epoch changed after queueing")
            manifest = {**publication.resource_manifest,
                        "budget_contracts": snapshot.model_dump(mode="json")}
            publication = publication.model_copy(update={"resource_manifest": manifest})
            routed_publication = routed_publication.model_copy(
                update={"resource_manifest": manifest}
            )
        elif publication.resource_manifest.get("budget_contracts") is not None:
            raise PolicyCompilationError("Budget contract publication requires an execution parent")
        if (snapshot_availability is not None and status in {"verifying", "promoting"}
                and not already_promoted):
            if self._availability_projector is None:
                raise PolicyCompilationError("Availability publication projection is unavailable")
            availability_repository = cast(ModelAvailabilityRepository, self._repository)
            probe_states = {}
            for alias, model_id in snapshot_availability.models.items():
                availability_repository.stage_model_availability(model_id, publication.id)
                projected = self._availability_projector(model_id)
                if projected.model_id != model_id or projected.lifecycle == "deleted":
                    raise PolicyCompilationError("Publication availability identity is not current")
                probe_states[alias] = projected.enabled
            routed_publication = routed_publication.model_copy(update={"resource_manifest": {
                **routed_publication.resource_manifest, "availability_probe_states": probe_states,
            }})
        compiler = self._compiler_for(publication, resolved)
        compiled = compiler.compile(routed_publication)
        client.verify_publication_permissions(compiled, recovery_only=already_promoted)
        if ((execution_enabled or snapshot_availability is not None)
            and status in {"verifying", "promoting"}
                and publication.policy_sha256 != compiled.policy_sha256):
            raise PolicyCompilationError("Candidate policy no longer matches the active compiler")
        profiles = [
            validate_image_profile(binding.model.image_profile)
            for binding in routed_publication.desired_spec.bindings
            if binding.api_format is ApiFormat.OPENAI_IMAGES
        ]
        image_release = compiled.images_generations_policy is not None
        updates: dict[str, Any] = {}
        if status == "queued":
            base_revision, live_policy = observed_current or client.current_api_policy()
            if (execution_enabled or image_release or snapshot_availability is not None
                    or "imageGenerationPolicyVersion" in live_policy):
                validate_parent_policy(live_policy, canonical_parent, profiles)
            else:
                compiler.patch_parent_policy(canonical_parent)
                compiler.patch_parent_policy(live_policy)
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "apim_target_identity_sha256": resolved.target_identity_sha256,
                "base_apim_revision": base_revision,
                "base_policy_sha256": hashlib.sha256(live_policy.encode("utf-8")).hexdigest(),
                "base_policy_document_sha256": policy_document_digest(live_policy),
            }
        elif status == "validating":
            encrypted = self._repository.gateway_publication_credential(publication.id)
            credential = None
            if encrypted is not None:
                if self._cipher is None:
                    raise PolicyCompilationError("Credential decryption is unavailable")
                credential = self._cipher.decrypt(encrypted)
            latest_named_value = (
                publication.desired_spec.bindings[-1].named_value_name
                if publication.publication_kind is not PublicationKind.MODEL_REMOVE
                else None
            )
            latest_oauth = (
                publication.desired_spec.bindings[-1].oauth
                if publication.desired_spec.bindings else None
            )
            for oauth in compiled.oauth_credentials:
                materialized_oauth = (
                    replace(oauth, client_secret=credential)
                    if credential is not None and latest_oauth == oauth.config
                    else oauth
                )
                client.ensure_oauth_credential(materialized_oauth)
            for named_value in compiled.named_values:
                materialized = (
                    replace(named_value, value=credential)
                    if credential is not None and named_value.id == latest_named_value
                    else named_value
                )
                observed_credentials[named_value.id] = "unverified"
                try:
                    client.ensure_named_value(materialized)
                except CredentialSourceMissingError as error:
                    if error.named_value_id == named_value.id:
                        observed_credentials[named_value.id] = "missing"
                    raise
                observed_credentials[named_value.id] = "present"
            for backend in compiled.backends:
                client.ensure_backend(backend)
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "backends": [backend.id for backend in compiled.backends],
                "named_values": [value.id for value in compiled.named_values],
                "credential_evidence": publication_credential_evidence(
                    publication, observed_credentials,
                ),
                **({"oauth_credentials": [
                    item.config.provider_id for item in compiled.oauth_credentials
                ]} if compiled.oauth_credentials else {}),
            }
        elif status == "provisioning":
            revision = f"turnstile-{publication.generation}-{publication.id.hex[:8]}"
            client.ensure_revision(revision, f"Turnstile publication {publication.id}")
            updates["apim_revision"] = revision
        elif status == "building_revision":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            base_revision, live_policy = client.current_api_policy()
            manifest = publication.resource_manifest
            live_hash = hashlib.sha256(live_policy.encode("utf-8")).hexdigest()
            expected_document = manifest.get("base_policy_document_sha256")
            matching_document = (
                isinstance(expected_document, str) and len(expected_document) == 64
                and policy_document_digest(live_policy) == expected_document
            )
            if (
                manifest.get("base_apim_revision") != base_revision
                or (manifest.get("base_policy_sha256") != live_hash and not matching_document)
            ):
                raise PolicyCompilationError(
                    "The current APIM policy changed after publication was queued"
                )
            parent = (
                validate_parent_policy(live_policy, canonical_parent, profiles)
                if (execution_enabled or image_release or snapshot_availability is not None
                    or "imageGenerationPolicyVersion" in live_policy)
                else compiler.patch_parent_policy(live_policy)
            )
            client.ensure_revision(
                publication.apim_revision, f"Turnstile publication {publication.id}"
            )
            client.put_api_policy(publication.apim_revision, parent)
            for operation in compiled.operations:
                client.ensure_operation(publication.apim_revision, operation)
            for operation_id, policy in compiled.availability_probe_policies:
                client.put_operation_policy(publication.apim_revision, operation_id, policy)
            client.put_operation_policy(
                publication.apim_revision,
                self._chat_completions_operation_id,
                compiled.chat_completions_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._responses_operation_id,
                compiled.responses_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._responses_compact_operation_id,
                compiled.responses_compact_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._messages_operation_id,
                compiled.messages_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._count_tokens_operation_id,
                compiled.count_tokens_policy,
            )
            client.put_operation_policy(
                publication.apim_revision,
                self._models_operation_id,
                compiled.models_policy,
            )
            if compiled.images_generations_policy is not None:
                client.put_operation_policy(
                    publication.apim_revision,
                    "images-generations",
                    compiled.images_generations_policy,
                )
            updates["policy_sha256"] = compiled.policy_sha256
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "parent_policy_sha256": hashlib.sha256(parent.encode("utf-8")).hexdigest(),
                **(
                    {
                        "parent_policy_contract_sha256": component_digest(
                            parse_policy(parent), normalize_text_defaults=False
                        ),
                    }
                    if image_release or execution_enabled or snapshot_availability is not None
                    else {}
                ),
                **({"images_generations_operation": True} if image_release else {}),
            }
        elif status == "verifying":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            if image_release or execution_enabled or snapshot_availability is not None:
                validate_parent_readback(
                    client.revision_api_policy(publication.apim_revision),
                    canonical_parent,
                    profiles,
                    expected_contract=publication.resource_manifest.get(
                        "parent_policy_contract_sha256"
                    ),
                    expected_raw=publication.resource_manifest.get("parent_policy_sha256"),
                )
            if image_release:
                authorization = ImageProbeAuthorization.model_validate(
                    publication.resource_manifest.get(
                        "image_probe_authorization", {"id": str(publication.id)}
                    )
                )
                planned = {}
                if publication.publication_kind is PublicationKind.MODEL_ADD:
                    binding = publication.desired_spec.bindings[-1]
                    model = binding.model
                    planned[model.model_key] = binding.logical_model_id or publication_model_id(
                        publication.id, model.model_key
                    )
                journal = PersistentProbeJournal(
                    self._repository,
                    f"publication:{publication.id}",
                    authorization.id,
                    authorization.attempt_limit,
                    heartbeat,
                    plan_context={
                        "policy_sha256": compiled.policy_sha256,
                        "desired_spec_sha256": publication.desired_spec_sha256,
                        "credential_generation": publication.resource_manifest.get(
                            "credential_generation"
                        ),
                    },
                    planned_model_ids=planned,
                )
                outcomes = client.probe_revision(
                    publication.apim_revision, routed_publication, journal=journal
                )
            else:
                outcomes = client.probe_revision(publication.apim_revision, routed_publication)
            if outcomes:
                updates["resource_manifest"] = {
                    **publication.resource_manifest,
                    "gateway_probe_results": [item.model_dump(mode="json") for item in outcomes],
                }
        elif status == "promoting":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            if heartbeat:
                heartbeat()
            if image_release or execution_enabled or snapshot_availability is not None:
                validate_parent_readback(
                    client.revision_api_policy(publication.apim_revision),
                    canonical_parent,
                    profiles,
                    expected_contract=publication.resource_manifest.get(
                        "parent_policy_contract_sha256"
                    ),
                    expected_raw=publication.resource_manifest.get("parent_policy_sha256"),
                )
            if not already_promoted:
                client.promote_revision(
                    publication.apim_revision, f"turnstile-{publication.id.hex[:12]}"
                )
            if client.current_revision() != publication.apim_revision:
                raise RetryablePublicationError(
                    "APIM did not report the promoted revision as current"
                )
            active = self._repository.activate_gateway_publication(publication.id, worker_id)
            return GatewayPublication.model_validate(active)
        else:
            return publication

        next_status = self._NEXT_STATUS[status]
        row = self._repository.transition_gateway_publication(
            publication.id, status, next_status, updates, worker_id
        )
        if row is None:
            raise RuntimeError("Publication changed while the worker held its lease")
        return GatewayPublication.model_validate(row)