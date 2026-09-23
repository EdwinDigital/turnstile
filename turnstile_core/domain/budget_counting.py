from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from .control_plane import (
    ApiFormat,
    GatewayModelBinding,
    GatewayPublication,
    PublicationKind,
    publication_model_id,
)
from .models import StrictModel

BudgetOperation = Literal[
    "chat-completions", "responses", "anthropic-messages", "responses-compact",
    "images-generations",
]
BudgetRequestFeature = Literal[
    "conversation_state", "opaque_state", "multimodal_input", "server_tools", "background",
]


class NativeCountContract(StrictModel):
    kind: Literal["anthropic_messages", "openai_responses"]
    endpoint: HttpUrl
    assurance: Literal["estimate", "upper_bound"] = "estimate"
    upper_bound_padding_tokens: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    supported_features: list[BudgetRequestFeature] = Field(default_factory=list)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_endpoint(self) -> NativeCountContract:
        if (
            self.endpoint.scheme != "https"
            or self.endpoint.username
            or self.endpoint.password
            or self.endpoint.query
            or self.endpoint.fragment
        ):
            raise ValueError("Native counting requires a credential-free HTTPS endpoint")
        suffix = (
            "/messages/count_tokens" if self.kind == "anthropic_messages"
            else "/responses/input_tokens"
        )
        if not (self.endpoint.path or "").endswith(suffix):
            raise ValueError("Native counting cannot target an inference operation")
        if self.assurance == "estimate" and self.upper_bound_padding_tokens:
            raise ValueError("An arbitrary margin does not make an estimate a verified upper bound")
        if any(feature in self.supported_features for feature in ("server_tools", "background")):
            raise ValueError("Input counting does not bound future server-side work")
        return self


class ProtocolCountContract(StrictModel):
    algorithm: Literal[
        "context_input_ceiling_v1", "request_total_ceiling_v1", "legacy_request_estimate_v1",
    ] = "context_input_ceiling_v1"
    input_ceiling: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    output_ceiling: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    output_field: Literal["max_tokens", "max_completion_tokens", "max_output_tokens", "none"]
    request_token_ceiling: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)
    output_estimate: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)
    supported_features: list[BudgetRequestFeature] = Field(default_factory=list)
    operations: tuple[BudgetOperation, ...] = Field(
        default=("chat-completions", "responses", "anthropic-messages"), min_length=1,
    )
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: AwareDatetime
    native_counter: NativeCountContract | None = None

    @model_validator(mode="after")
    def validate_ceiling(self) -> ProtocolCountContract:
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Counting operations must be unique")
        if len(set(self.supported_features)) != len(self.supported_features):
            raise ValueError("Verified request features must be unique")
        if self.algorithm == "legacy_request_estimate_v1":
            if (
                self.output_estimate is None or self.native_counter is not None
                or self.input_ceiling or self.output_ceiling
                or self.request_token_ceiling is not None
                or (self.output_field == "none") != all(
                    operation in {"responses-compact", "images-generations"}
                    for operation in self.operations
                )
                or (any(operation in {"responses-compact", "images-generations"}
                        for operation in self.operations) and self.output_field != "none")
            ):
                raise ValueError("Legacy reservation estimates cannot assert a verified ceiling")
            return self
        if self.output_estimate is not None:
            raise ValueError("Verified counting contracts cannot use a legacy estimate")
        if self.algorithm == "request_total_ceiling_v1":
            if (self.request_token_ceiling is None or self.output_field != "none"
                    or self.native_counter is not None):
                raise ValueError("A whole-request ceiling preserves provider output parameters")
        elif (
            self.input_ceiling <= 0 or self.output_ceiling <= 0 or self.output_field == "none"
            or self.input_ceiling + self.output_ceiling > 2**63 - 1
            or any(value in self.operations for value in (
                "responses-compact", "images-generations",
            ))
            or self.request_token_ceiling is not None
        ):
            raise ValueError("Input counting requires a valid enforced text output ceiling")
        return self


class ModelCountContract(ProtocolCountContract):
    model_id: UUID
    protocols: dict[BudgetOperation, ProtocolCountContract] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_protocols(self) -> ModelCountContract:
        if any(operation not in contract.operations
               for operation, contract in self.protocols.items()):
            raise ValueError("A protocol override must cover its declared operation")
        return self


class BudgetPublicationContracts(StrictModel):
    version: Literal[1] = 1
    publication_id: UUID
    gateway_profile_id: UUID
    epoch: UUID
    release_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: dict[str, str]
    model_contracts: dict[str, ModelCountContract]

    @staticmethod
    def binding_digest(binding: GatewayModelBinding) -> str:
        return hashlib.sha256(json.dumps(
            binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    def validate_publication(self, publication: GatewayPublication) -> None:
        if (self.publication_id != publication.id
                or self.gateway_profile_id != publication.gateway_profile_id
                or self.release_spec_sha256 != publication.desired_spec_sha256):
            raise ValueError("Budget contracts belong to another publication")
        expected = {
            binding.model.model_key: self.binding_digest(binding)
            for binding in publication.desired_spec.bindings
        }
        if self.binding_sha256 != expected or set(self.model_contracts) != set(expected):
            raise ValueError("Budget contract binding changed after publication was queued")

    @classmethod
    def capture(
        cls, publication: GatewayPublication,
        registry: Mapping[str, Sequence[Mapping[str, object]]], fence: Mapping[str, object],
        previous: GatewayPublication | None = None, *, new_model_key: str | None = None,
    ) -> BudgetPublicationContracts:
        if (fence.get("protocol_version") != 2 or fence.get("mode") != "active"
                or fence.get("gateway_profile_id") != str(publication.gateway_profile_id)):
            raise ValueError("Budget publication requires its active Gateway fence")
        epoch = UUID(str(fence.get("epoch")))
        raw_contracts = fence.get("model_contracts")
        if not isinstance(raw_contracts, dict):
            raise ValueError("Budget publication requires explicit existing model contracts")
        inherited = None
        if previous is not None and previous.resource_manifest.get("budget_contracts") is not None:
            inherited = cls.model_validate(previous.resource_manifest["budget_contracts"])
            if (inherited.publication_id != previous.id
                    or inherited.gateway_profile_id != publication.gateway_profile_id
                    or inherited.epoch != epoch):
                raise ValueError("Inherited budget contracts belong to another publication epoch")
        contracts = {}
        digests = {}
        candidate_key = new_model_key or (
            publication.desired_spec.bindings[-1].model.model_key
            if publication.desired_spec.bindings else None
        )
        for binding in publication.desired_spec.bindings:
            alias = binding.model.model_key
            prior = inherited.model_contracts.get(alias) if inherited is not None else None
            identity_hint = prior.model_id if prior is not None else None
            recorded = raw_contracts.get(alias)
            if identity_hint is None and isinstance(recorded, dict):
                identity_hint = UUID(str(recorded.get("model_id")))
            matches = [model for model in registry["models"]
                       if str(model["model_key"]).casefold() == alias.casefold() and (
                           str(model.get("runtime_id")) == str(binding.runtime_id)
                           or (binding.runtime_id is None and identity_hint is not None
                               and str(model["id"]) == str(identity_hint))
                       )]
            new_model = (publication.publication_kind is PublicationKind.MODEL_ADD
                         and alias == candidate_key and not matches)
            if len(matches) > 1:
                raise ValueError("Budget model identity is ambiguous")
            model_id = UUID(str(matches[0]["id"])) if matches else None
            if model_id is None and new_model:
                model_id = binding.logical_model_id or publication_model_id(publication.id, alias)
            if model_id is None:
                raise ValueError("Budget model identity is unavailable")
            digest = cls.binding_digest(binding)
            if prior is not None and prior.model_id == model_id:
                if (inherited is not None and inherited.binding_sha256.get(alias) != digest
                        and any(contract.algorithm != "legacy_request_estimate_v1"
                                for contract in (prior, *prior.protocols.values()))):
                    raise ValueError("Verified counting cannot be inherited by a changed route")
                contracts[alias] = prior.model_copy(deep=True)
            elif alias in raw_contracts:
                contract = ModelCountContract.model_validate(raw_contracts[alias])
                if contract.model_id != model_id:
                    if not new_model:
                        raise ValueError("Budget contract belongs to another model identity")
                    contract = cls._estimate(publication, binding, model_id, digest)
                contracts[alias] = contract
            elif new_model:
                contracts[alias] = cls._estimate(publication, binding, model_id, digest)
            else:
                raise ValueError("An existing model is missing its approved budget contract")
            digests[alias] = digest
        return cls(
            publication_id=publication.id, gateway_profile_id=publication.gateway_profile_id,
            epoch=epoch, release_spec_sha256=publication.desired_spec_sha256,
            binding_sha256=digests, model_contracts=contracts,
        )

    @staticmethod
    def _estimate(
        publication: GatewayPublication, binding: GatewayModelBinding, model_id: UUID, digest: str,
    ) -> ModelCountContract:
        protocols: dict[BudgetOperation, ProtocolCountContract] = {}
        fields: dict[BudgetOperation, Literal[
            "max_tokens", "max_completion_tokens", "max_output_tokens", "none",
        ]]
        if binding.api_format is ApiFormat.OPENAI_IMAGES:
            fields = {"images-generations": "none"}
        elif binding.api_format is ApiFormat.ANTHROPIC_MESSAGES:
            fields = {"anthropic-messages": "max_tokens"}
        else:
            openai_v1 = (binding.routing_managed
                         and binding.backend_path.rstrip("/") == "/openai/v1/chat/completions")
            fields = {"chat-completions": "max_completion_tokens" if openai_v1 else "max_tokens"}
            if openai_v1:
                fields.update({"responses": "max_output_tokens", "responses-compact": "none"})
        for operation, field in fields.items():
            protocols[operation] = ProtocolCountContract(
                algorithm="legacy_request_estimate_v1", output_field=field,
                output_estimate=8192 if operation == "images-generations" else 4096,
                operations=(operation,), evidence_sha256=digest, verified_at=publication.created_at,
            )
        primary = next(iter(protocols.values()))
        return ModelCountContract(**primary.model_dump(), model_id=model_id, protocols=protocols)