from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from ..domain.application_access import (
    ApplicationActorType,
    UsageApplicationAttribution,
)
from ..domain.control_plane import GatewayPublication, PublicationKind, publication_model_id
from ..domain.ledger import LedgerAdmissionReceipt
from ..domain.models import ModelIdentity, ModelPrice, TokenUsageRecord, UsageEvent
from ..persistence.repository import OpsDbProxy

logger = logging.getLogger(__name__)

# Event Hub delivers batches, so the registry is read at most this often instead of once per event.
_PRICE_CACHE_SECONDS = 60.0


def calculate_et(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    coefficient_m: float,
) -> float:
    return round(
        coefficient_m * (input_tokens + 0.1 * cached_tokens + 4.0 * output_tokens),
        6,
    )


class CoefficientResolver:
    def __init__(self, coefficients: Mapping[str, float]) -> None:
        self._coefficients = dict(coefficients)

    def resolve(self, provider: str, model: str) -> float:
        exact = self._coefficients.get(f"{provider}/{model}")
        if exact is not None:
            return exact
        for key, value in self._coefficients.items():
            if key.endswith("*") and f"{provider}/{model}".startswith(key[:-1]):
                return value
        return self._coefficients.get("default", 1.0)


class UsageProcessor:
    def __init__(
        self, repository: OpsDbProxy, resolver: CoefficientResolver, *,
        execution_enabled: bool = False,
        settle_committed_usage: Callable[[str], object] | None = None,
    ) -> None:
        self._repository = repository
        self._resolver = resolver
        self._execution_enabled = execution_enabled
        self._settle_committed_usage = settle_committed_usage
        self._prices: dict[str, ModelPrice] = {}
        self._identities: dict[str, ModelIdentity] = {}
        self._registry_read_at = 0.0
        self._application_maps: dict[
            UUID, tuple[float, dict[str, Mapping[str, Any]]]
        ] = {}
        self._probe_publications: dict[UUID, tuple[float, GatewayPublication]] = {}

    def _refresh_registry(self) -> None:
        now = time.monotonic()
        if now - self._registry_read_at < _PRICE_CACHE_SECONDS:
            return
        try:
            self._prices = self._repository.model_prices()
            self._identities = self._repository.model_identities()
        except Exception:
            # A registry read failure must not drop telemetry. The row lands unpriced with the
            # caller's own identifiers, keeping the gap visible instead of recording a zero cost.
            logger.exception("Model registry lookup failed; recording usage without a cost")
            self._prices = {}
            self._identities = {}
        self._registry_read_at = now

    def _price_for(self, model_id: str, model: str) -> ModelPrice | None:
        self._refresh_registry()
        return self._prices.get(model_id) or self._prices.get(model)

    def _identity_for(self, model_id: str, model: str) -> ModelIdentity | None:
        self._refresh_registry()
        return self._identities.get(model_id) or self._identities.get(model)

    def _publication_probe_model(
        self, event: UsageEvent,
    ) -> tuple[ModelIdentity | None, ModelPrice | None] | None:
        if (event.request_source != "gateway-publication-probe"
                or event.user_id != "system-gateway-publication"):
            return None
        try:
            publication_id = UUID(event.run_id)
        except ValueError:
            return None
        now = time.monotonic()
        cached = self._probe_publications.get(publication_id)
        if cached is not None and now - cached[0] < _PRICE_CACHE_SECONDS:
            publication = cached[1]
        else:
            row = self._repository.get_gateway_publication(publication_id)
            if row is None:
                raise ValueError("Publication probe snapshot is unavailable")
            publication = GatewayPublication.model_validate(row)
            if len(self._probe_publications) >= 64:
                self._probe_publications.pop(next(iter(self._probe_publications)))
            self._probe_publications[publication_id] = (now, publication)
        if event.gateway_profile_id != publication.gateway_profile_id:
            raise ValueError("Publication probe belongs to another APIM target")
        attribution = self._application_attribution(event)
        if attribution is None or attribution.actor_type != "system":
            raise ValueError("Publication probe requires a synchronized system subscription")
        raw_model_id = event.model_id or event.model
        for binding in publication.desired_spec.bindings:
            alias = binding.model.model_key
            identity = self._identity_for(alias, alias)
            if (publication.publication_kind is PublicationKind.MODEL_ADD
                    and binding is publication.desired_spec.bindings[-1]):
                model_id = str(publication_model_id(publication.id, alias))
            else:
                model_id = identity.model_id if identity is not None else alias
            if raw_model_id.casefold() not in {model_id.casefold(), alias.casefold()}:
                continue
            if event.model.casefold() not in {
                model_id.casefold(), alias.casefold(), binding.model.display_name.casefold(),
            }:
                raise ValueError("Publication probe model identity does not match")
            model = binding.model
            price = ModelPrice.from_registry(model.model_dump(mode="python"))
            return ModelIdentity(model_id=model_id, display_name=model.display_name), price
        for removed in publication.desired_spec.removed_models:
            if raw_model_id in {str(removed.model_id), removed.model_key}:
                return (
                    ModelIdentity(
                        model_id=str(removed.model_id), display_name=removed.display_name,
                    ),
                    None,
                )
        if any(raw_model_id == model.id for model in publication.desired_spec.discovery_models):
            return (
                self._identity_for(raw_model_id, event.model),
                self._price_for(raw_model_id, event.model),
            )
        raise ValueError("Publication probe model is absent from its snapshot")

    def _application_attribution(
        self, event: UsageEvent
    ) -> UsageApplicationAttribution | None:
        gateway_profile_id = event.gateway_profile_id
        apim_subscription_id = event.apim_subscription_id
        if gateway_profile_id is None or not apim_subscription_id:
            return None
        now = time.monotonic()
        cached_at, mappings = self._application_maps.get(
            gateway_profile_id, (0.0, {})
        )
        if now - cached_at >= _PRICE_CACHE_SECONDS:
            try:
                mappings = {
                    str(item["apim_subscription_id"]).casefold(): item
                    for item in self._repository.gateway_application_attribution_map(
                        gateway_profile_id
                    )
                }
            except Exception:
                logger.exception(
                    "Application attribution lookup failed for gateway %s",
                    gateway_profile_id,
                )
                mappings = {}
            self._application_maps[gateway_profile_id] = (now, mappings)
        mapping = mappings.get(apim_subscription_id.casefold())
        if mapping is None:
            logger.warning(
                "APIM subscription %s is not synchronized for gateway %s",
                apim_subscription_id,
                gateway_profile_id,
            )
            return None
        actor_type: ApplicationActorType
        if str(mapping["application_type"]) == "system":
            actor_type = "system"
            person_id = None
            actor_id = f"system:{apim_subscription_id}"
        elif event.application_actor_type == "person":
            if not event.application_actor_id or event.user_id == "unattributed":
                logger.warning(
                    "Delegated Application request %s has no verified person identity",
                    event.id,
                )
                return None
            actor_type = "person"
            person_id = event.user_id
            actor_id = event.application_actor_id
        else:
            actor_type = "service"
            person_id = None
            actor_id = event.application_actor_id or f"service:{apim_subscription_id}"
        return UsageApplicationAttribution(
            application_id=UUID(str(mapping["application_id"])),
            application_subscription_id=UUID(
                str(mapping["application_subscription_id"])
            ),
            application_name_snapshot=str(mapping["application_name"]),
            apim_subscription_id=apim_subscription_id,
            actor_type=actor_type,
            actor_id=actor_id,
            person_id=person_id,
            application_admission=event.application_admission,
        )

    def normalize(self, raw_event: Mapping[str, Any]) -> TokenUsageRecord | None:
        try:
            event = UsageEvent.model_validate(raw_event)
        except ValidationError as error:
            logger.warning("Malformed usage event skipped: %s", error.errors(include_input=False))
            return None

        ingest_error = event.ingest_error
        estimated = event.estimated
        token_fields = (
            "input_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "output_tokens",
            "tokens_consumed",
        )
        invalid_usage = any(
            raw_event.get(field) is not None and type(raw_event[field]) is not int
            for field in token_fields
        )
        if event.cached_tokens is not None and event.cache_write_tokens is not None:
            invalid_usage = invalid_usage or event.cache_write_tokens > event.cached_tokens
        if (
            event.tokens_consumed is not None
            and event.input_tokens is not None
            and event.cached_tokens is not None
            and event.output_tokens is not None
        ):
            invalid_usage = invalid_usage or event.tokens_consumed != (
                event.input_tokens + event.cached_tokens + event.output_tokens
            )
        if invalid_usage:
            estimated = True
            ingest_error = "usage_validation_failed"
        if event.input_tokens is None or event.cached_tokens is None or event.output_tokens is None:
            # The provider breakdown is unavailable. Persist the request with zeroed counts
            # and an explicit ingest_error instead of inventing a split or dropping the event;
            # reconciliation against the gateway LLM log restores the real values later.
            status_code = event.status_code or _status_code(event.status)
            input_tokens = 0
            cached_tokens = 0
            cache_write_tokens = 0
            output_tokens = 0
            estimated = True
            if status_code >= 400:
                ingest_error = ingest_error or "failed_request_has_no_usage"
            elif event.tokens_consumed is None:
                ingest_error = ingest_error or "usage_unavailable"
            else:
                ingest_error = ingest_error or "stream_usage_breakdown_unavailable"
        else:
            input_tokens = event.input_tokens
            cached_tokens = event.cached_tokens
            # Writes are a subset of the cache bucket; a gateway that does not report the split
            # leaves every cached token priced as a read rather than guessing a division.
            cache_write_tokens = min(event.cache_write_tokens or 0, cached_tokens)
            output_tokens = event.output_tokens

        coefficient = self._resolver.resolve(event.provider, event.model)
        raw_model_id = event.model_id or event.model
        # The same model arrives as a registry UUID from the dashboard BFF and as a model key from
        # an employee desktop client. Both are collapsed onto the registry identity so aggregates
        # do not split one model across two rows. Unknown models keep the caller's own values.
        try:
            probe_model = self._publication_probe_model(event)
        except Exception as error:
            logger.warning("Publication probe snapshot lookup failed: %s", type(error).__name__)
            probe_model = (None, None)
            ingest_error = ingest_error or "publication_probe_snapshot_unavailable"
        identity = (
            probe_model[0] if probe_model is not None
            else self._identity_for(raw_model_id, event.model)
        )
        model_id = identity.model_id if identity else raw_model_id
        model_name = identity.display_name if identity else event.model
        # APIM emits no cost, so it is derived here from the registry and stored with the unit
        # prices used, making the amount reproducible and immune to later price edits.
        price = (
            probe_model[1] if probe_model is not None
            else self._price_for(raw_model_id, event.model)
        )
        context_snapshot = price.model_dump(mode="json") if price and price.context_tiers else None
        if context_snapshot is not None:
            if (estimated or invalid_usage
                or event.input_tokens is None or event.cached_tokens is None):
                price = None
                ingest_error = ingest_error or "context_pricing_input_unavailable"
            elif price is not None:
                price = price.for_input_tokens(input_tokens + cached_tokens)
        estimated_cost = (
            price.cost(input_tokens, cached_tokens, output_tokens, cache_write_tokens)
            if price and context_snapshot is not None
            else 0.0 if context_snapshot is not None
            else event.estimated_cost
            if event.estimated_cost and probe_model is None
            else price.cost(input_tokens, cached_tokens, output_tokens, cache_write_tokens)
            if price
            else 0.0
        )
        is_copilot_usage = event.ingest_source == "copilot_cli" or (
            event.request_source or ""
        ).startswith("copilot-assistant")
        correlation_id = event.correlation_id or event.request_id or event.id
        return TokenUsageRecord(
            id=event.id if is_copilot_usage else correlation_id,
            request_id=event.request_id or event.id,
            correlation_id=correlation_id,
            ts=event.ts,
            team=event.team or "unattributed",
            organization=event.organization or "unattributed",
            organization_id=event.organization_id or "unattributed",
            department=event.department or "unattributed",
            department_id=event.department_id or "unattributed",
            project=event.project or "unattributed",
            project_id=event.project_id or "unattributed",
            user=event.user or "unattributed",
            user_id=event.user_id or "unattributed",
            agent=event.agent or "unattributed",
            agent_id=event.agent_id or "unattributed",
            workflow=event.workflow or "unattributed",
            run_id=event.run_id or "unattributed",
            turn_index=event.turn_index,
            provider=event.provider,
            model=model_name,
            model_id=model_id,
            runtime=event.runtime or "unattributed",
            runtime_authoritative=(
                event.runtime_authoritative
                and not estimated
                and not is_copilot_usage
                and event.ingest_source == "eventhub"
                and event.runtime not in {"", "unattributed"}
            ),
            request_source=event.request_source or "unattributed",
            usage_domain="github_copilot" if is_copilot_usage else "apim",
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            et=calculate_et(input_tokens, cached_tokens, output_tokens, coefficient),
            et_coeff_m=coefficient,
            latency_ms=event.latency_ms,
            status=event.status,
            status_code=event.status_code or _status_code(event.status),
            estimated_cost=estimated_cost,
            input_price_per_million=price.input_price_per_million if price else None,
            cached_price_per_million=price.cached_price_per_million if price else None,
            cache_write_price_per_million=price.cache_write_price_per_million if price else None,
            output_price_per_million=price.output_price_per_million if price else None,
            price_context_tier=price.selected_tier if price else None,
            price_context_snapshot=context_snapshot,
            error_message=event.error_message,
            estimated=estimated,
            ingest_source=event.ingest_source,
            ingest_error=ingest_error,
            budget_admission=event.budget_admission,
            model_admission=event.model_admission,
        )

    def process(self, raw_event: Mapping[str, Any]) -> bool:
        record = self.normalize(raw_event)
        if record is None:
            return False
        event = UsageEvent.model_validate(raw_event)
        if event.budget_execution:
            if not self._execution_enabled:
                raise RuntimeError("Budget execution receipts require the enabled execution reader")
            if (record.usage_domain != "apim" or event.ingest_source != "eventhub"
                    or event.usage_event_source not in {"apim", "observer"}
                    or event.gateway_profile_id is None or not event.native_gateway_id):
                raise ValueError("Budget execution receipts require trusted gateway attribution")
            receipts = [LedgerAdmissionReceipt.model_validate(value)
                        for value in event.budget_execution]
            self._repository.write_token_usage(
                record, self._application_attribution(event),
                execution_receipts=receipts,
                gateway_profile_id=event.gateway_profile_id,
                native_gateway_id=event.native_gateway_id,
                evidence_source=event.usage_event_source,
                usage_complete=(
                    event.usage_complete and not record.estimated and record.ingest_error is None
                    and all(type(raw_event.get(field)) is int for field in (
                        "input_tokens", "cached_tokens", "cache_write_tokens",
                        "output_tokens", "tokens_consumed",
                    ))
                ),
            )
            if self._settle_committed_usage is not None:
                try:
                    self._settle_committed_usage(record.id)
                except Exception as error:
                    logger.warning("Budget settlement wakeup failed after commit error_type=%s",
                                   type(error).__name__)
        else:
            self._repository.write_token_usage(record, self._application_attribution(event))
        return True


def _status_code(status: str) -> int:
    try:
        return int(status)
    except ValueError:
        return 200 if status.lower() in {"ok", "success", "succeeded"} else 500
