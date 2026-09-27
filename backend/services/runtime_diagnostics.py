from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException

from turnstile_core.config import Settings
from turnstile_core.domain.application_access import application_id_for
from turnstile_core.domain.gateway_targets import (
    ApimTarget,
    GatewayDeployment,
    SelfHostedGatewayProvisionSpec,
)
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    HealthStatus,
    InvocationMetadata,
    ModelInvocationRequest,
    RuntimeCheckOption,
    RuntimeCheckPlan,
    RuntimeCheckSelection,
    RuntimeDiagnosticAccess,
    RuntimeHealth,
    RuntimeKind,
)
from turnstile_core.integrations.apim_target_client_factory import (
    AzureApimProbeAccessChecker,
    AzureApimTargetProbeCredentialResolver,
    TargetProbeCredentialResolver,
)
from turnstile_core.integrations.gateway import GatewayInvocationError, GatewayRouter
from turnstile_core.persistence.repository_contract import QueryRepository

logger = logging.getLogger(__name__)
DIAGNOSTIC_PREFIX = "runtime-diagnostic:v1:"
_KNOWN_ERRORS = (
    "model_not_assigned",
    "application_model_not_assigned",
    "budget_ledger_unavailable",
    "application_ledger_unavailable",
    "application_tpm_unavailable",
    "application_map_unavailable",
    "application_unconfigured",
    "application_inactive",
    "budget_protocol_fenced",
    "budget_count_bound_unavailable",
    "budget_configuration_invalid",
    "budget_protocol_validation_scope",
    "budget_model_unattributed",
    "budget_request_analysis_failed",
    "budget_request_invalid",
    "budget_plan_failed",
    "budget_stream_evidence_unavailable",
    "budget_output_limit_unavailable",
    "budget_output_limit_invalid",
    "budget_consumer_disabled",
    "budget_exhausted",
    "budget_request_bound_exceeds_balance",
    "budget_delegation_invalid",
    "budget_identity_unavailable",
    "budget_exceeded",
    "budget_temporarily_reserved",
    "model_unavailable",
    "rate_limit_exceeded",
)


class RuntimeDiagnostics:
    def __init__(
        self,
        repository: QueryRepository,
        settings: Settings,
        probe_credentials: TargetProbeCredentialResolver | None = None,
        router: GatewayRouter | None = None,
        *,
        access_checker: Callable[[ApimTarget], RuntimeDiagnosticAccess] | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._credentials = probe_credentials or AzureApimTargetProbeCredentialResolver()
        self._router = router
        self._access_checker = access_checker or AzureApimProbeAccessChecker().check

    @staticmethod
    def configuration_fingerprint(context: Mapping[str, Any]) -> str | None:
        fields = {
            "runtime": (
                "id",
                "provider_id",
                "gateway_profile_id",
                "runtime_kind",
                "enabled",
                "config",
            ),
            "provider": ("id", "provider_kind", "endpoint_url", "auth_type", "enabled", "config"),
            "gateway": ("id", "implementation", "enabled"),
            "model": ("id", "provider_id", "runtime_id", "model_key", "enabled", "capabilities"),
            "target": (
                "gateway_profile_id",
                "tenant_id",
                "subscription_id",
                "resource_group",
                "service_name",
                "arm_resource_id",
                "api_id",
                "api_path",
                "principal_id",
                "probe_subscription_id",
                "management_auth_profile_ref",
            ),
            "deployment": (
                "id",
                "apim_target_id",
                "kind",
                "native_gateway_id",
                "endpoint_url",
                "declared_state",
                "retired_at",
            ),
        }
        if any(not isinstance(context.get(kind), dict) for kind in fields):
            return None
        payload: dict[str, Any] = {
            kind: {key: context[kind].get(key) for key in keys} for kind, keys in fields.items()
        }
        model, metadata = context["model"], context.get("model_metadata") or {}
        payload["model"].update(
            family_key=metadata.get("family_key") or "generic",
            upstream_model_id=metadata.get("upstream_model_id") or model["model_key"],
            capabilities=sorted(model.get("capabilities") or []),
        )
        ciphertext = context["provider"].get("credential_ciphertext")
        if isinstance(ciphertext, str):
            ciphertext = (
                bytes.fromhex(ciphertext[2:])
                if ciphertext.startswith("\\x")
                else ciphertext.encode()
            )
        payload["provider"]["credential_version"] = (
            hashlib.sha256(bytes(ciphertext)).hexdigest() if ciphertext else None
        )
        binding = context.get("binding")
        payload["binding"] = (
            {
                key: binding.get(key)
                for key in (
                    "logical_model_id",
                    "apim_target_id",
                    "runtime_id",
                    "enabled",
                )
            }
            if binding
            else None
        )
        payload["deployment_in_scope"] = context.get("deployment_in_scope")
        published = context.get("published_route")
        payload["published_route"] = None
        if isinstance(published, dict):
            payload["published_route"] = {
                key: value
                for key, value in published.items()
                if key
                not in {
                    "model",
                    "provider_name",
                    "runtime_name",
                    "gateway_name",
                    "display_name",
                    "deployment_scope",
                    "serving_deployments",
                }
            }
            payload["published_route"]["serving_deployments"] = [
                {
                    key: deployment.get(key)
                    for key in (
                        "deployment_id",
                        "apim_target_id",
                        "native_gateway_id",
                        "endpoint_url",
                        "kind",
                    )
                }
                for deployment in published.get("serving_deployments", [])
                if str(deployment.get("deployment_id")) == str(context["gateway_deployment_id"])
            ]
            payload["published_route"]["model"] = {
                key: published.get("model", {}).get(key)
                for key in ("model_key", "upstream_model_id", "family_key", "capabilities")
            }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return "v1:" + hashlib.sha256(encoded).hexdigest()

    def project_results(self, runtimes: Sequence[dict[str, Any]]) -> dict[UUID, RuntimeHealth]:
        results: dict[UUID, RuntimeHealth] = {}
        selections: list[tuple[UUID, UUID, UUID, UUID]] = []
        for runtime in runtimes:
            message = runtime.get("health_message") or ""
            if not message.startswith(DIAGNOSTIC_PREFIX):
                continue
            try:
                result = RuntimeHealth.model_validate_json(message[len(DIAGNOSTIC_PREFIX) :])
            except ValueError:
                continue
            if result.runtime_id != runtime["id"]:
                continue
            results[result.runtime_id] = result.model_copy(
                update={
                    "configuration_status": "unverified",
                    "configuration_checked_at": None,
                }
            )
            if result.selection and re.fullmatch(
                r"v1:[0-9a-f]{64}", result.configuration_fingerprint or ""
            ):
                selections.append(
                    (
                        result.runtime_id,
                        result.selection.model_id,
                        result.selection.gateway_profile_id,
                        result.selection.gateway_deployment_id,
                    )
                )
        if not selections:
            return results
        try:
            indexed = {
                UUID(str(context["runtime_id"])): context
                for context in self._repository.runtime_diagnostic_contexts(selections)
            }
            checked_at = datetime.now(UTC)
            for runtime_id, _model_id, _gateway_id, _deployment_id in selections:
                context = indexed.get(runtime_id)
                if context is not None:
                    result = results[runtime_id]
                    matches = (
                        self.configuration_fingerprint(context) == result.configuration_fingerprint
                    )
                    results[runtime_id] = result.model_copy(
                        update={
                            "configuration_status": "current" if matches else "changed",
                            "configuration_checked_at": checked_at,
                        }
                    )
        except Exception as error:
            logger.warning(
                "Diagnostic configuration unavailable error_type=%s", type(error).__name__
            )
        return results

    def plan(self, runtime_id: UUID) -> RuntimeCheckPlan:
        plan = self._plan(runtime_id)
        if not plan.options:
            return plan
        target = self._target(plan.options[0].gateway_profile_id)
        return plan.model_copy(update={"access": self._access_checker(target)})

    def _target(self, target_id: UUID) -> ApimTarget:
        row = self._repository.get_apim_target(target_id)
        if row is None:
            raise HTTPException(status_code=409, detail="A registered APIM target is required")
        return ApimTarget.model_validate({key: row[key] for key in ApimTarget.model_fields})

    def _plan(self, runtime_id: UUID) -> RuntimeCheckPlan:
        registry = self._repository.registry()
        runtime = next((row for row in registry["runtimes"] if row["id"] == runtime_id), None)
        if runtime is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        plan = RuntimeCheckPlan(runtime_id=runtime_id, options=[])
        if not runtime["enabled"]:
            return plan.model_copy(update={"unavailable_reason": "Connection is disabled"})
        gateway_id = runtime.get("gateway_profile_id")
        if not gateway_id or self._repository.get_apim_target(gateway_id) is None:
            return plan.model_copy(
                update={"unavailable_reason": "A registered APIM target is required"}
            )
        target = self._target(gateway_id)
        gateway = next((row for row in registry["gateways"] if row["id"] == gateway_id), None)
        provider = next(
            (row for row in registry["providers"] if row["id"] == runtime["provider_id"]), None
        )
        if not gateway or not gateway["enabled"] or not provider or not provider["enabled"]:
            return plan.model_copy(update={"unavailable_reason": "Provider or Gateway is disabled"})
        try:
            self._system_application(target)
        except ValueError as error:
            return plan.model_copy(update={"unavailable_reason": str(error)})
        bindings = self._repository.list_apim_target_route_bindings(apim_target_ids=[gateway_id])
        deployments = self._repository.list_gateway_deployments(gateway_id)
        options = []
        for model in registry["models"]:
            capabilities = model.get("capabilities") or []
            if (
                model["runtime_id"] != runtime_id
                or not model["enabled"]
                or "chat" not in capabilities
                or "image_generation" in capabilities
            ):
                continue
            binding = next(
                (row for row in bindings if row["logical_model_id"] == model["id"]), None
            )
            if binding and (not binding["enabled"] or binding["runtime_id"] != runtime_id):
                continue
            for row in deployments:
                deployment = GatewayDeployment.model_validate(
                    {key: row[key] for key in GatewayDeployment.model_fields},
                )
                scope = binding["deployment_scope"] if binding else None
                if (
                    scope
                    and scope["mode"] == "selected"
                    and deployment.id not in scope["deployment_ids"]
                ):
                    continue
                if binding is None and deployment.kind != "managed":
                    continue
                try:
                    self._validate_endpoint(target, deployment)
                except ValueError:
                    continue
                options.append(
                    RuntimeCheckOption(
                        model_id=model["id"],
                        model_key=model["model_key"],
                        model_name=model["display_name"],
                        gateway_profile_id=gateway_id,
                        gateway_deployment_id=deployment.id,
                        gateway_name=deployment.display_name,
                        native_gateway_id=deployment.native_gateway_id,
                    )
                )
        options.sort(key=lambda option: (option.model_key, option.native_gateway_id))
        return plan.model_copy(
            update={
                "options": options,
                "unavailable_reason": None
                if options
                else "No eligible text model and trusted Gateway route",
            }
        )

    def _system_application(self, target: ApimTarget) -> None:
        if (
            target.drift_state != "in_sync"
            or target.management_auth_profile_ref != "system_managed_identity"
        ):
            raise ValueError("Synchronize and verify the APIM target before running diagnostics")
        if not target.probe_subscription_id:
            raise ValueError("APIM system diagnostic subscription is not configured")
        application_id = application_id_for(target.gateway_profile_id, target.probe_subscription_id)
        application = self._repository.get_gateway_application(application_id)
        if (
            not application
            or application["status"] != "active"
            or application["application_type"] != "system"
            or not application["system_managed"]
            or application["gateway_profile_id"] != target.gateway_profile_id
        ):
            raise ValueError("Synchronize the target's system diagnostic subscription")
        matches = [
            row
            for row in self._repository.list_gateway_application_subscriptions([application_id])
            if row["gateway_profile_id"] == target.gateway_profile_id
            and row["apim_subscription_id"] == target.probe_subscription_id
            and row["application_id"] == application_id
            and row["state"] == "active"
            and row["scope_exists"]
            and (
                row["scope_type"] == "api"
                and row["scope_id"].casefold() == target.api_id.casefold()
                or row["scope_type"] == "product"
                and bool(row["scope_id"])
            )
        ]
        if len(matches) != 1:
            raise ValueError("The diagnostic subscription must belong to the selected APIM target")

    def _validate_endpoint(self, target: ApimTarget, deployment: GatewayDeployment) -> None:
        if (
            deployment.apim_target_id != target.gateway_profile_id
            or deployment.retired_at is not None
            or deployment.declared_state != "present"
            or deployment.endpoint_url is None
            or deployment.reachability_state == "unreachable"
        ):
            raise ValueError("Gateway endpoint is unavailable")
        endpoint = str(deployment.endpoint_url).rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Gateway endpoint is not trusted")
        matches_api_endpoint = (
            parsed.port in (None, 443)
            and parsed.path.rstrip("/") == ("/" + target.api_path.strip("/")).rstrip("/")
        )
        inventory = self._repository.get_gateway_deployment_inventory(deployment.id) or {}
        hosts = {
            str(row["hostname"]).casefold() for row in inventory.get("hostname_configurations", [])
        }
        if deployment.kind == "managed":
            if (
                deployment.native_gateway_id != "managed"
                or not matches_api_endpoint
                or (
                    parsed.hostname.casefold() != f"{target.service_name}.azure-api.net".casefold()
                    and parsed.hostname.casefold() not in hosts
                )
            ):
                raise ValueError("Managed Gateway endpoint does not match the APIM target")
            return
        if deployment.native_gateway_id == "managed":
            raise ValueError("Self-hosted Gateway identity is invalid")
        if not self._settings.self_hosted_gateway_enabled:
            raise ValueError("Self-hosted Gateway diagnostics are disabled")
        if not any(
            str(value).casefold() == target.api_id.casefold()
            for value in inventory.get("associated_api_ids", [])
        ):
            raise ValueError("Gateway is not associated with the target API")
        trusted = self._settings.self_hosted_gateway_trusted_endpoints.get(deployment.id)
        if (matches_api_endpoint and parsed.hostname.casefold() in hosts) or (
            trusted and str(trusted).rstrip("/") == endpoint
        ):
            return
        if deployment.provisioning_operation_id is None:
            raise ValueError("Gateway endpoint is not trusted")
        operation = self._repository.get_gateway_release_operation(
            deployment.provisioning_operation_id
        )
        if (
            not operation
            or operation["status"] != "succeeded"
            or operation["operation_kind"] != "self_hosted_gateway_provision"
        ):
            raise ValueError("Gateway endpoint is not trusted")
        spec = SelfHostedGatewayProvisionSpec.model_validate(operation["semantic_preview"])
        if (
            operation["gateway_profile_id"] != target.gateway_profile_id
            or spec.deployment_id != deployment.id
            or spec.native_gateway_id != deployment.native_gateway_id
            or spec.target.arm_resource_id.casefold() != target.arm_resource_id.casefold()
            or spec.target.api_id != target.api_id
            or spec.target.api_path != target.api_path
            or spec.target.tenant_id != target.tenant_id
            or not spec.configuration.endpoint_control_confirmed
            or str(spec.configuration.endpoint_url).rstrip("/") != endpoint
        ):
            raise ValueError("Gateway creation evidence does not match the current endpoint")

    @staticmethod
    def _protocol(route: Mapping[str, Any]) -> dict[str, Any]:
        if route["runtime_kind"] not in {RuntimeKind.FOUNDRY, RuntimeKind.OPENAI_COMPATIBLE}:
            raise ValueError("Unsupported diagnostic runtime")
        config = dict(route["runtime_config"])
        if route.get("model_family_key") == "claude":
            config.update(
                path="/v1/messages", api_format="anthropic_messages", anthropic_version="2023-06-01"
            )
        api_format = config.get("api_format", "openai_chat")
        paths = (
            {"/v1/messages"}
            if api_format == "anthropic_messages"
            else {
                "/chat/completions",
                "/v1/chat/completions",
                "/openai/v1/chat/completions",
            }
        )
        if (
            api_format not in {"openai_chat", "anthropic_messages"}
            or str(config.get("path", "/v1/chat/completions")) not in paths
            or config.get("max_tokens_field", "max_tokens")
            not in {"max_tokens", "max_completion_tokens"}
        ):
            raise ValueError("Unsupported diagnostic protocol or output limit")
        config.update(timeout_seconds=12, control_plane_managed=True)
        return config

    def check(
        self, runtime_id: UUID, selection: RuntimeCheckSelection, actor: str
    ) -> RuntimeHealth:
        plan = self.plan(runtime_id)
        selected = next(
            (
                option
                for option in plan.options
                if option.model_id == selection.model_id
                and option.gateway_deployment_id == selection.gateway_deployment_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail=plan.unavailable_reason or "Diagnostic selection changed; refresh the plan",
            )
        if plan.access is None or plan.access.status != "ready":
            raise HTTPException(
                status_code=409, detail="Diagnostic access is not ready; refresh the plan"
            )
        route = self._repository.invocation_route(
            runtime_id,
            selected.model_id,
            gateway_profile_id=selected.gateway_profile_id,
            gateway_deployment_id=selected.gateway_deployment_id,
        )
        if (
            route is None
            or str(route["runtime_id"]) != str(runtime_id)
            or str(route["model_id"]) != str(selected.model_id)
            or str(route["gateway_id"]) != str(selected.gateway_profile_id)
            or str(route.get("gateway_deployment_id")) != str(selected.gateway_deployment_id)
            or route.get("gateway_implementation") != "apim"
        ):
            raise HTTPException(
                status_code=409, detail="The selected diagnostic route is no longer available"
            )
        target = self._target(selected.gateway_profile_id)
        deployment_row = self._repository.get_gateway_deployment(selected.gateway_deployment_id)
        if deployment_row is None:
            raise HTTPException(status_code=409, detail="Diagnostic target changed")
        deployment = GatewayDeployment.model_validate(
            {key: deployment_row[key] for key in GatewayDeployment.model_fields},
        )
        try:
            self._system_application(target)
            self._validate_endpoint(target, deployment)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if str(route["gateway_base_url"]).rstrip("/") != str(deployment.endpoint_url).rstrip("/"):
            raise HTTPException(status_code=409, detail="Diagnostic endpoint changed")
        context_selection = (
            runtime_id,
            selected.model_id,
            selected.gateway_profile_id,
            selected.gateway_deployment_id,
        )
        contexts = self._repository.runtime_diagnostic_contexts([context_selection])
        fingerprint = self.configuration_fingerprint(contexts[0]) if len(contexts) == 1 else None
        if fingerprint is None:
            raise HTTPException(
                status_code=409, detail="Diagnostic configuration could not be verified"
            )
        request_id, started_at = str(uuid4()), datetime.now(UTC)
        marker = DIAGNOSTIC_PREFIX + "pending:" + request_id
        if not self._repository.claim_runtime_diagnostic(
            runtime_id,
            marker,
            started_at,
            started_at - timedelta(seconds=60),
        ):
            raise HTTPException(
                status_code=429,
                detail="A diagnostic is running or was recently completed",
                headers={"Retry-After": "60"},
            )
        logger.info(
            "Runtime diagnostic actor=%s runtime=%s model=%s gateway=%s request=%s",
            actor,
            runtime_id,
            selected.model_id,
            selected.gateway_deployment_id,
            request_id,
        )
        result = RuntimeHealth(
            runtime_id=runtime_id,
            status=HealthStatus.UNKNOWN,
            message="Diagnostic could not be completed",
            checked_at=started_at,
            check_kind="model_inference",
            outcome="configuration_error",
            selection=selected,
            request_id=request_id,
            configuration_fingerprint=fingerprint,
        )
        started, dispatched = time.monotonic(), False
        try:
            protocol = self._protocol(route)
            credential = self._credentials.resolve(target)
            if credential is None:
                raise ValueError("Diagnostic credential unavailable")
            current_runtime = next(
                (row for row in self._repository.registry()["runtimes"] if row["id"] == runtime_id),
                None,
            )
            current_contexts = self._repository.runtime_diagnostic_contexts([context_selection])
            if (
                self._target(target.gateway_profile_id) != target
                or self._repository.get_gateway_deployment(deployment.id) != deployment_row
                or self._repository.invocation_route(
                    runtime_id,
                    selected.model_id,
                    gateway_profile_id=selected.gateway_profile_id,
                    gateway_deployment_id=selected.gateway_deployment_id,
                )
                != route
                or current_runtime is None
                or current_runtime.get("health_message") != marker
                or selected not in self._plan(runtime_id).options
                or len(current_contexts) != 1
                or self.configuration_fingerprint(current_contexts[0]) != fingerprint
            ):
                raise ValueError("Diagnostic target or ownership changed before dispatch")
            diagnostic_route = {
                key: value
                for key, value in route.items()
                if "credential" not in key and key != "budget_delegation_token"
            }
            diagnostic_route.update(
                runtime_config=protocol,
                gateway_auth_type="api_key",
                gateway_config={"header_name": "Ocp-Apim-Subscription-Key"},
                gateway_credential=credential.get_secret_value(),
                request_id=request_id,
            )
            request = ModelInvocationRequest(
                runtime_id=runtime_id,
                model_id=selected.model_id,
                metadata=InvocationMetadata(
                    organization_id="system",
                    organization="Turnstile",
                    department_id="system",
                    department="Diagnostics",
                    project_id="system",
                    project="Runtime diagnostics",
                    agent_id="runtime-diagnostic",
                    agent="Runtime diagnostic",
                    user_id="system-runtime-diagnostic",
                    user="Runtime diagnostic",
                    workflow="runtime-diagnostic",
                    model_id=str(selected.model_id),
                    model=selected.model_key,
                    runtime=str(route["runtime_name"]),
                    request_source="runtime-diagnostic",
                    run_id=request_id,
                    turn_index=1,
                ),
                messages=[ChatMessage(role="user", content="Reply with OK.")],
                max_output_tokens=64,
                temperature=None,
                stream=False,
            )
            with httpx.Client(follow_redirects=False, timeout=12) as client:
                router = self._router or GatewayRouter(client)
                if (
                    datetime.now(UTC) >= started_at + timedelta(seconds=45)
                    or time.monotonic() - started >= 45
                ):
                    raise ValueError("Diagnostic dispatch deadline expired")
                dispatched = True
                response = router.adapter(diagnostic_route).invoke(request, diagnostic_route)
            result = result.model_copy(
                update={
                    "status": HealthStatus.AVAILABLE,
                    "outcome": "passed",
                    "message": "Selected model inference succeeded",
                    "http_status": 200,
                    "correlation_id": response.correlation_id,
                }
            )
        except GatewayInvocationError as error:
            received = error.__cause__
            status = (
                received.response.status_code
                if isinstance(received, httpx.HTTPStatusError)
                else None
            )
            payload: object = None
            if isinstance(received, httpx.HTTPStatusError):
                with suppress(ValueError):
                    payload = received.response.json()
            nested = payload.get("error", payload) if isinstance(payload, dict) else None
            raw_code = nested.get("code") if isinstance(nested, dict) else nested
            code = (
                raw_code
                if isinstance(raw_code, str) and raw_code in _KNOWN_ERRORS
                else next(
                    (
                        value
                        for value in _KNOWN_ERRORS
                        if re.search(r"(?<![a-z_])" + value + r"(?![a-z_])", str(error))
                    ),
                    None,
                )
            )
            blocked = code is not None or status in {401, 403, 409, 429}
            upstream = status in {500, 502, 503} and not blocked
            result = result.model_copy(
                update={
                    "status": HealthStatus.UNAVAILABLE if upstream else HealthStatus.UNKNOWN,
                    "outcome": "blocked"
                    if blocked
                    else "upstream_error"
                    if upstream
                    else "inconclusive",
                    "message": "Diagnostic blocked by gateway policy"
                    if blocked
                    else "Selected route returned a server error"
                    if upstream
                    else "Diagnostic did not establish availability",
                    "http_status": status,
                    "error_code": code,
                    "correlation_id": error.headers.get("x-correlation-id"),
                }
            )
        except Exception as error:
            logger.warning(
                "Runtime diagnostic unavailable request=%s error_type=%s",
                request_id,
                type(error).__name__,
            )
            if dispatched:
                result = result.model_copy(
                    update={
                        "outcome": "inconclusive",
                        "message": "Diagnostic response could not be verified",
                    }
                )
        completed_at = datetime.now(UTC)
        result = result.model_copy(
            update={
                "checked_at": completed_at,
                "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
            }
        )
        if not self._repository.finish_runtime_diagnostic(
            runtime_id,
            marker,
            result.status.value,
            DIAGNOSTIC_PREFIX + result.model_dump_json(),
            completed_at,
        ):
            raise HTTPException(
                status_code=409,
                detail="A newer diagnostic superseded this result",
                headers={"x-request-id": request_id},
            )
        return self.project_results(
            [
                {
                    "id": runtime_id,
                    "health_message": DIAGNOSTIC_PREFIX + result.model_dump_json(),
                }
            ]
        )[runtime_id]
