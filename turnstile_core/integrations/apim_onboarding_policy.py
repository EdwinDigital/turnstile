from __future__ import annotations

import hashlib
import json
import re

from ..domain.gateway_targets import ApimOnboardingConfiguration, ApimOnboardingRequest
from .apim_budget_execution import budget_execution_fence, compose_budget_execution_parent
from .apim_control_plane_contract import PolicyCompilationError
from .apim_policy_components import parse_policy, serialize_policy, validate_parent_policy


def onboarding_logger_id(request: ApimOnboardingRequest) -> str:
    return f"{request.api_id}-usage"


def onboarding_canonical_parent(
    canonical_parent: str, api_id: str,
    configuration: ApimOnboardingConfiguration | None = None,
    *, ledger_table_endpoint: str | None = None, ledger_table_name: str | None = None,
) -> str:
    source = compose_budget_execution_parent(canonical_parent, canonical_parent, enabled=True)
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("APIM initialization requires an inbound policy")
    base = inbound.find("base")
    if base is None:
        raise PolicyCompilationError("APIM initialization must preserve policy inheritance")
    inbound.insert(list(inbound).index(base) + 1, budget_execution_fence([api_id]))
    if configuration is not None:
        ledger = configuration.ledger_table_resource_id.split("/")
        ledger_table_endpoint = f"https://{ledger[8]}.table.core.windows.net"
        ledger_table_name = ledger[-1]
    if ledger_table_endpoint is not None or ledger_table_name is not None:
        if not ledger_table_endpoint or not ledger_table_name:
            raise PolicyCompilationError("The canonical initialization ledger is incomplete")

        def bind(value: str) -> str:
            return value.replace("__LEDGER_TABLE_ENDPOINT__", str(ledger_table_endpoint)).replace(
                "__LEDGER_TABLE_NAME__", str(ledger_table_name),
            )

        for node in root.iter():
            for name, value in node.attrib.items():
                node.set(name, bind(value))
            if node.text:
                node.text = bind(node.text)
            if node.tail:
                node.tail = bind(node.tail)
    return serialize_policy(root)


def onboarding_policy_configuration_digest(
    configuration: ApimOnboardingConfiguration, canonical_parent: str,
) -> str:
    payload = {
        "version": 1, "configuration": configuration.model_dump(mode="json"),
        "canonical_parent_sha256": hashlib.sha256(
            onboarding_canonical_parent(canonical_parent, "turnstile-onboarding").encode()
        ).hexdigest(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def render_onboarding_parent(
    request: ApimOnboardingRequest,
    configuration: ApimOnboardingConfiguration,
    canonical_parent: str,
) -> str:
    ledger = configuration.ledger_table_resource_id.split("/")
    canonical = onboarding_canonical_parent(canonical_parent, request.api_id, configuration)
    root = parse_policy(canonical)

    def quoted(value: str) -> str:
        return json.dumps(value, ensure_ascii=True)[1:-1]

    values = {
        "__GATEWAY_PROFILE_ID__": str(request.gateway_profile_id),
        "__LOGGER_ID__": onboarding_logger_id(request),
        "__EMPLOYEE_TOKEN_ENABLED__": "true",
        "__EMPLOYEE_TENANT_ID__": str(configuration.employee_tenant_id),
        "__EMPLOYEE_CLIENT_ID__": str(configuration.employee_client_id),
        "__EMPLOYEE_AUDIENCE__": quoted(configuration.employee_audience),
        "__EMPLOYEE_ORG_ID__": quoted(configuration.organization_id),
        "__EMPLOYEE_ORG_NAME__": quoted(configuration.organization_name),
        "__EMPLOYEE_TOKENS_PER_MINUTE__": str(configuration.employee_tokens_per_minute),
        "__TOKENS_PER_MINUTE__": str(configuration.employee_tokens_per_minute),
        "__DEFAULT_MAX_OUTPUT_TOKENS__": str(configuration.default_max_output_tokens),
        "__LEDGER_TABLE_ENDPOINT__": f"https://{ledger[8]}.table.core.windows.net",
        "__LEDGER_TABLE_NAME__": ledger[-1],
        "__EMPLOYEE_DEPARTMENT_MAP__": quoted(json.dumps(configuration.department_map)),
        "__EMPLOYEE_CLIENT_MAP__": quoted(json.dumps(configuration.client_map)),
        "__EMPLOYEE_SURFACE_MAP__": quoted(json.dumps(
            configuration.model_dump(mode="json")["surface_map"]
        )),
        "__SUBSCRIPTION_AGENT_MAP__": "{}",
        "__LEGACY_PROVIDER_ROUTING__": "",
        "__USAGE_OBSERVER_HEADERS__": "",
    }
    placeholder = re.compile(r"__[A-Z][A-Z0-9_]*__")

    def bind(text: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            if match.group() not in values:
                raise PolicyCompilationError("Unsupported onboarding parameter: " + match.group())
            return values[match.group()]
        return placeholder.sub(replacement, text)

    for node in root.iter():
        for name, value in node.attrib.items():
            node.set(name, bind(value))
        if node.text:
            node.text = bind(node.text)
        if node.tail:
            node.tail = bind(node.tail)
    rendered = serialize_policy(root)
    validate_parent_policy(rendered, canonical)
    return rendered