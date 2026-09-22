from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx

from ..domain.budget_counting import BudgetPublicationContracts
from ..domain.control_plane import (
    ApiFormat,
    GatewayPublication,
    ImageProbeAuthorization,
    PublicationKind,
    publication_model_id,
)
from ..domain.image_profiles import validate_image_profile
from ..integrations.apim_control_plane import (
    ApimPolicyCompiler,
    ApimPublisherClient,
    AuthorizationRequiredError,
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    validate_parent_policy,
    validate_parent_readback,
)
from ..persistence.repository import QueryRepository
from ..security import CredentialCipher
from .control_plane import GatewayControlPlaneService
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
        client: ApimPublisherClient,
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
    ) -> None:
        self._repository = repository
        self._client = client
        self._parent_policy = parent_policy
        self._parent_policy_overrides = dict(parent_policy_overrides or {})
        self._budget_configuration = budget_configuration
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
        if publication.attempt_count > max_attempts:
            promoted_but_not_recorded = (
                publication.status.value == "promoting"
                and publication.apim_revision is not None
                and self._client.current_revision() == publication.apim_revision
            )
            if promoted_but_not_recorded:
                with WorkerLease(
                    lambda: self._repository.renew_gateway_publication_lease(
                        publication.id, worker_id, lease_seconds
                    ),
                    lease_seconds,
                ) as lease:
                    return self._advance(publication, worker_id, lease.heartbeat)
            failed = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                "failed",
                {
                    "error_code": "max_attempts_exceeded",
                    "error_message": "Gateway publication exceeded its retry limit",
                },
                worker_id,
            )
            return GatewayPublication.model_validate(failed) if failed else publication
        try:
            with WorkerLease(
                lambda: self._repository.renew_gateway_publication_lease(
                    publication.id, worker_id, lease_seconds
                ),
                lease_seconds,
            ) as lease:
                return self._advance(publication, worker_id, lease.heartbeat)
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
                {"error_code": code, "error_message": message},
                worker_id,
            )
            return GatewayPublication.model_validate(queued) if queued else publication
        except Exception as error:
            code = type(error).__name__
            message = str(error).replace("\n", " ")[:1000]
            if (
                publication.status.value == "promoting"
                and publication.apim_revision is not None
                and self._client.current_revision() == publication.apim_revision
            ):
                queued = self._repository.transition_gateway_publication(
                    publication.id,
                    publication.status.value,
                    publication.status.value,
                    {"error_code": code, "error_message": message},
                    worker_id,
                )
                return GatewayPublication.model_validate(queued) if queued else publication
            failed = self._repository.transition_gateway_publication(
                publication.id,
                publication.status.value,
                "failed",
                {"error_code": code, "error_message": message},
                worker_id,
            )
            return GatewayPublication.model_validate(failed) if failed else publication

    def _advance(
        self,
        publication: GatewayPublication,
        worker_id: str,
        heartbeat: Callable[[], None] | None = None,
    ) -> GatewayPublication:
        registry = self._repository.registry()
        status = publication.status.value
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
        compiled = self._compiler.compile(routed_publication)
        if (execution_enabled and status in {"verifying", "promoting"}
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
            base_revision, live_policy = self._client.current_api_policy()
            if execution_enabled or image_release or "imageGenerationPolicyVersion" in live_policy:
                validate_parent_policy(live_policy, canonical_parent, profiles)
            else:
                self._compiler.patch_parent_policy(canonical_parent)
                self._compiler.patch_parent_policy(live_policy)
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "base_apim_revision": base_revision,
                "base_policy_sha256": hashlib.sha256(live_policy.encode("utf-8")).hexdigest(),
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
                self._client.ensure_oauth_credential(materialized_oauth)
            for named_value in compiled.named_values:
                materialized = (
                    replace(named_value, value=credential)
                    if credential is not None and named_value.id == latest_named_value
                    else named_value
                )
                self._client.ensure_named_value(materialized)
            if encrypted is not None:
                self._repository.delete_gateway_publication_credential(publication.id)
            for backend in compiled.backends:
                self._client.ensure_backend(backend)
            updates["resource_manifest"] = {
                **publication.resource_manifest,
                "backends": [backend.id for backend in compiled.backends],
                "named_values": [value.id for value in compiled.named_values],
                **({"oauth_credentials": [
                    item.config.provider_id for item in compiled.oauth_credentials
                ]} if compiled.oauth_credentials else {}),
            }
        elif status == "provisioning":
            revision = f"turnstile-{publication.generation}-{publication.id.hex[:8]}"
            self._client.ensure_revision(revision, f"Turnstile publication {publication.id}")
            updates["apim_revision"] = revision
        elif status == "building_revision":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            self._client.ensure_revision(
                publication.apim_revision, f"Turnstile publication {publication.id}"
            )
            base_revision, live_policy = self._client.current_api_policy()
            manifest = publication.resource_manifest
            live_hash = hashlib.sha256(live_policy.encode("utf-8")).hexdigest()
            if (
                manifest.get("base_apim_revision") != base_revision
                or manifest.get("base_policy_sha256") != live_hash
            ):
                raise PolicyCompilationError(
                    "The current APIM policy changed after publication was queued"
                )
            parent = (
                validate_parent_policy(live_policy, canonical_parent, profiles)
                if (execution_enabled or image_release
                    or "imageGenerationPolicyVersion" in live_policy)
                else self._compiler.patch_parent_policy(live_policy)
            )
            self._client.put_api_policy(publication.apim_revision, parent)
            for operation in compiled.operations:
                self._client.ensure_operation(publication.apim_revision, operation)
            self._client.put_operation_policy(
                publication.apim_revision,
                self._chat_completions_operation_id,
                compiled.chat_completions_policy,
            )
            self._client.put_operation_policy(
                publication.apim_revision,
                self._responses_operation_id,
                compiled.responses_policy,
            )
            self._client.put_operation_policy(
                publication.apim_revision,
                self._responses_compact_operation_id,
                compiled.responses_compact_policy,
            )
            self._client.put_operation_policy(
                publication.apim_revision,
                self._messages_operation_id,
                compiled.messages_policy,
            )
            self._client.put_operation_policy(
                publication.apim_revision,
                self._count_tokens_operation_id,
                compiled.count_tokens_policy,
            )
            self._client.put_operation_policy(
                publication.apim_revision,
                self._models_operation_id,
                compiled.models_policy,
            )
            if compiled.images_generations_policy is not None:
                self._client.put_operation_policy(
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
                    if image_release or execution_enabled
                    else {}
                ),
                **({"images_generations_operation": True} if image_release else {}),
            }
        elif status == "verifying":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            if image_release or execution_enabled:
                validate_parent_readback(
                    self._client.revision_api_policy(publication.apim_revision),
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
                    model = publication.desired_spec.bindings[-1].model
                    planned[model.model_key] = publication_model_id(publication.id, model.model_key)
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
                self._client.probe_revision(
                    publication.apim_revision, routed_publication, journal=journal
                )
            else:
                self._client.probe_revision(publication.apim_revision, routed_publication)
        elif status == "promoting":
            if not publication.apim_revision:
                raise RuntimeError("Publication is missing its APIM revision")
            if heartbeat:
                heartbeat()
            if image_release or execution_enabled:
                validate_parent_readback(
                    self._client.revision_api_policy(publication.apim_revision),
                    canonical_parent,
                    profiles,
                    expected_contract=publication.resource_manifest.get(
                        "parent_policy_contract_sha256"
                    ),
                    expected_raw=publication.resource_manifest.get("parent_policy_sha256"),
                )
            if self._client.current_revision() != publication.apim_revision:
                self._client.promote_revision(
                    publication.apim_revision, f"turnstile-{publication.id.hex[:12]}"
                )
                if self._client.current_revision() != publication.apim_revision:
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