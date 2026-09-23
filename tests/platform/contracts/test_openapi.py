"""Keeps `contracts/openapi.yaml` honest about the routes the app actually serves.

Contract drift fails nothing: the app keeps working, the linter still passes, and the
document quietly starts describing a different API. The assistant surface reached nine
operations before anyone noticed it was absent from the contract entirely, which is what
this test exists to prevent happening again.

Only paths and methods are compared. Asserting every schema field against the generated
document would fail on formatting differences that mean nothing -- FastAPI emits `anyOf`
where the hand-written contract says `type: [string, 'null']` -- and a test that has to be
loosened after each edit stops being read.

PyYAML is a pinned dev dependency rather than an optional import: a guard that skips when
something is missing reports success while checking nothing.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

from backend.api import app
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.domain.runtime_models import ModelVendorKey

CONTRACT = REPOSITORY_ROOT / "contracts" / "openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}

# Documented deliberately as internal, or served by something other than this app.
UNDOCUMENTED = {
    "/health",
    "/{path:path}",
}


def _operations(paths: dict[str, dict[str, object]]) -> set[tuple[str, str]]:
    return {(path, method) for path, item in paths.items() for method in item if method in METHODS}


@cache
def _yaml_document(path: Path) -> dict[str, object]:
    return cast(
        dict[str, object],
        yaml.safe_load(path.read_text(encoding="utf-8")),
    )


def _resolve_local_ref(ref: str) -> object:
    file_name, fragment = ref.split("#", 1)
    value: object = _yaml_document((CONTRACT.parent / file_name).resolve())
    for raw_part in fragment.removeprefix("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        value = cast(dict[str, object], value)[part]
    return value


def _resolve_index(values: dict[str, object]) -> dict[str, dict[str, object]]:
    def canonicalize(value: object) -> object:
        if isinstance(value, str):
            return value.removeprefix("../../openapi.yaml")
        if isinstance(value, list):
            return [canonicalize(item) for item in value]
        if isinstance(value, dict):
            return {key: canonicalize(item) for key, item in value.items()}
        return value

    resolved: dict[str, dict[str, object]] = {}
    for name, value in values.items():
        item = cast(dict[str, object], value)
        if set(item) == {"$ref"}:
            item = cast(dict[str, object], _resolve_local_ref(cast(str, item["$ref"])))
        resolved[name] = cast(dict[str, object], canonicalize(item))
    return resolved


def _documented_paths() -> dict[str, dict[str, object]]:
    document = _yaml_document(CONTRACT.resolve())
    return _resolve_index(cast(dict[str, object], document["paths"]))


def _documented_schemas() -> dict[str, dict[str, object]]:
    document = _yaml_document(CONTRACT.resolve())
    components = cast(dict[str, object], document["components"])
    return _resolve_index(cast(dict[str, object], components["schemas"]))


def test_execution_event_has_bounded_receipts_and_default_off_completion() -> None:
    schema = _documented_schemas()["UsageEvent"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    receipts = properties["budget_execution"]
    assert receipts["maxItems"] == 2 and receipts["default"] == []
    item = cast(dict[str, object], receipts["items"])
    assert item["additionalProperties"] is False
    assert set(cast(list[str], item["required"])) == {"identity", "epoch", "reserved_tokens"}
    fields = cast(dict[str, dict[str, object]], item["properties"])
    assert fields["reserved_tokens"]["minimum"] == 1
    assert set(cast(list[str], fields["identity"]["required"])) == {
        "scope", "gateway_profile_id", "native_gateway_id", "correlation_id", "model_id",
        "admitted_at",
    }
    assert properties["usage_complete"]["default"] is False
    assert properties["native_gateway_id"]["maxLength"] == 80
    assert "cache_write_tokens" in properties


def test_openapi_root_is_a_small_domain_index() -> None:
    root = _yaml_document(CONTRACT.resolve())
    paths = cast(dict[str, dict[str, str]], root["paths"])
    components = cast(dict[str, object], root["components"])
    schemas = cast(dict[str, dict[str, str]], components["schemas"])

    assert len(CONTRACT.read_text(encoding="utf-8").splitlines()) < 800
    assert len(paths) == 82
    assert len(schemas) == 200
    assert all(set(value) == {"$ref"} for value in paths.values())
    assert all(set(value) == {"$ref"} for value in schemas.values())
    assert {path.name for path in (CONTRACT.parent / "openapi" / "paths").glob("*.yaml")} == {
        "application-access.yaml",
        "assistant.yaml",
        "authentication.yaml",
        "budgets.yaml",
        "github-copilot.yaml",
        "model-platform.yaml",
        "observability.yaml",
    }


def test_gateway_target_contract_matches_actual_structures_and_owner_operations() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    generated = _yaml_document(CONTRACT.parent / "openapi/schemas/gateway-targets.yaml")
    for name in cast(dict[str, object], generated["schemas"]):
        assert documented[name] == actual[name]
    write = documented["ApimTargetWrite"]
    assert write["additionalProperties"] is False
    publication = documented["GatewayPublicationCreate"]
    assert "deployment_scope" not in cast(list[str], publication["required"])
    publication_properties = cast(dict[str, object], publication["properties"])
    assert publication_properties["deployment_scope"] == {
        "$ref": "#/components/schemas/GatewayDeploymentScope",
    }
    paths = _documented_paths()
    registration = paths["/api/v1/model-management/apim-targets"]["post"]
    assert cast(dict[str, object], registration)["security"] == [{"sessionCookie": []}]
    sync = paths["/api/v1/model-management/apim-targets/{gateway_profile_id}/inventory-sync"]
    operation = cast(dict[str, object], sync["post"])
    assert "202" in cast(dict[str, object], operation["responses"])


def test_gateway_name_contract_preserves_owner_only_name_write_and_minimal_response() -> None:
    schema = _documented_schemas()["GatewayProfileNameWrite"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert set(properties) == {"name"} and properties["name"]["maxLength"] == 120
    operation = cast(dict[str, object], _documented_paths()[
        "/api/v1/model-management/gateways/{item_id}/name"
    ]["patch"])
    assert operation["security"] == [{"sessionCookie": []}]
    responses = cast(dict[str, dict[str, object]], operation["responses"])
    assert {"200", "204", "401", "403", "404", "422"} <= set(responses)
    assert "content" not in responses["204"]
    actual = app.openapi()["components"]["schemas"]
    assert actual["GatewayProfileNameWrite"]["additionalProperties"] is False
    assert "gateway_name_edit_supported" in actual["RegistryResponse"]["properties"]


def test_publication_contract_describes_catalog_prices_and_discounts() -> None:
    schema = _documented_schemas()["GatewayModelTarget"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert properties["price_source"]["enum"] == ["manual", "azure_retail", "anthropic"]
    assert properties["price_source"]["default"] == "manual"
    assert properties["price_reference"]["maxLength"] == 1000
    assert properties["price_discount_percent"]["exclusiveMinimum"] == 0
    assert properties["price_discount_percent"]["maximum"] == 100
    assert properties["price_discount_percent"]["type"] == ["number", "null"]
    conditions = cast(list[dict[str, dict[str, object]]], schema["allOf"])
    image = cast(dict[str, dict[str, object]], conditions[0]["then"]["properties"])
    assert image["price_source"]["const"] == "manual"
    for condition, source in zip(conditions[1:3], ("azure_retail", "anthropic"), strict=True):
        assert condition["if"]["required"] == ["price_source"]
        alternatives = cast(list[dict[str, object]], condition["then"]["anyOf"])
        assert alternatives[0]["required"] == ["price_reference"]
        assert alternatives[1]["required"] == ["context_price_references"]
        fields = cast(dict[str, dict[str, object]], alternatives[0]["properties"])
        assert fields["price_reference"] == {"type": "string", "pattern": f"^{source}:"}


def test_custom_context_rule_contract_matches_strict_runtime_fields() -> None:
    from turnstile_core.domain.models import UsageRequestDetail, UsageRequestSummary
    from turnstile_core.domain.runtime_models import ContextPriceRule

    schemas = _documented_schemas()
    rule = schemas["ContextPriceRule"]
    actual = ContextPriceRule.model_json_schema()
    assert rule["additionalProperties"] is False
    assert rule["required"] == actual["required"] == ["short_context_max_tokens"]
    fields = cast(dict[str, dict[str, object]], rule["properties"])
    assert fields["short_context_max_tokens"]["maximum"] == (
        actual["properties"]["short_context_max_tokens"]["maximum"]
    )
    assert set(fields) == set(actual["properties"])
    registry = cast(dict[str, dict[str, object]], schemas["ModelRegistry"]["properties"])
    assert registry["context_price_rules_supported"]["default"] is False
    assert "cost_available" in UsageRequestSummary.model_fields
    assert {"input_price_per_million", "cached_price_per_million",
            "cache_write_price_per_million", "output_price_per_million",
            "price_context_tier", "price_context_snapshot"} <= set(UsageRequestDetail.model_fields)


def test_databricks_contract_describes_workspace_authentication_and_owned_adoption() -> None:
    schemas = _documented_schemas()
    connection = schemas["ModelConnectionCreate"]
    properties = cast(dict[str, dict[str, object]], connection["properties"])
    assert properties["databricks_workspace_url"]["format"] == "uri"
    assert properties["oauth_client_id"]["format"] == "uuid"
    assert "oauth_m2m" in cast(list[str], properties["auth_mode"]["enum"])
    assert len(cast(list[object], connection["oneOf"])) == 4
    for schema_name in (
        "GatewayRuntimeTarget", "GatewayPublicationRetry", "GatewayCredentialRotation",
    ):
        fields = cast(dict[str, dict[str, object]], schemas[schema_name]["properties"])
        assert fields["oauth_client_secret"]["writeOnly"] is True
        assert fields["oauth_client_secret"]["maxLength"] == 4096
    registry = cast(dict[str, dict[str, object]], schemas["ModelRegistry"]["properties"])
    assert registry["databricks_connections_supported"]["default"] is False
    assert registry["databricks_oauth_supported"]["default"] is False
    path = "/api/v1/model-management/connections/{runtime_id}/adopt"
    operation = cast(dict[str, object], _documented_paths()[path]["post"])
    assert operation["security"] == [{"sessionCookie": []}]
    assert (path, "post") in _operations(app.openapi()["paths"])
    mi = cast(
        dict[str, dict[str, object]], schemas["DatabricksAuthorizationRequirement"]["properties"],
    )
    assert mi["kind"]["const"] == "databricks_workspace" and mi["role_name"]["const"] == "CAN_QUERY"


def test_application_provisioning_defaults_are_nullable_positive_limits() -> None:
    schema = _documented_schemas()["GatewayApplicationList"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    defaults = properties["provisioning_defaults"]
    assert defaults["type"] == ["object", "null"]
    assert defaults["additionalProperties"] is False
    limits = cast(dict[str, dict[str, object]], defaults["properties"])
    assert set(limits) == {"monthly_token_limit", "tokens_per_minute"}
    assert all(value["minimum"] == 1 for value in limits.values())


def test_image_contract_is_single_nonstreaming_passthrough_and_session_protected() -> None:
    schemas = _documented_schemas()
    request_schema = schemas["ImageInvocationRequest"]
    properties = cast(dict[str, dict[str, object]], request_schema["properties"])
    assert request_schema["additionalProperties"] is True
    assert properties["n"]["const"] == 1
    assert properties["stream"]["const"] is False
    for field in ("prompt", "size", "quality", "output_format"):
        assert "enum" not in properties[field] and "maxLength" not in properties[field]
    response = cast(dict[str, dict[str, object]], schemas["ImageInvocationResponse"]["properties"])
    assert response["data"]["minItems"] == response["data"]["maxItems"] == 1
    profile = cast(dict[str, dict[str, object]], schemas["ImageGenerationProfile"]["properties"])
    assert profile["version"]["const"] == 4
    operation = _documented_paths()["/api/v1/model-gateway/images/generations"]["post"]
    assert cast(dict[str, object], operation)["security"] == [{"sessionCookie": []}]
    assert ("/api/v1/model-gateway/images/generations", "post") in _operations(
        app.openapi()["paths"]
    )
    retry = cast(dict[str, dict[str, object]], schemas["GatewayPublicationRetry"]["properties"])
    assert retry["authorize_image_probes"]["default"] is False
    view = cast(dict[str, dict[str, object]], schemas["GatewayPublication"]["properties"])
    assert view["retry_can_authorize_image_probes"]["default"] is False
    assert "retry_can_authorize_image_probes" in (
        app.openapi()["components"]["schemas"]["GatewayPublicationView"]["properties"]
    )


def test_direct_publication_vendor_contract_matches_runtime_dto() -> None:
    schema = _documented_schemas()["GatewayRuntimeTarget"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert set(cast(list[str], properties["model_vendor"]["enum"])) == {
        vendor.value for vendor in ModelVendorKey
    }
    actual = app.openapi()["components"]["schemas"]["RuntimeTarget"]
    assert "model_vendor" in actual["properties"]
    for branch in cast(list[dict[str, object]], schema["oneOf"]):
        rejected = cast(dict[str, object], branch["not"])["anyOf"]
        if "openai_base_url" in cast(list[str], branch["required"]):
            assert {"required": ["model_vendor"]} not in cast(list[object], rejected)
        else:
            assert {"required": ["model_vendor"]} in cast(list[object], rejected)


def test_application_ledger_fields_preserve_unknown_and_zero() -> None:
    schema = _documented_schemas()["GatewayApplicationBudget"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    numeric = (
        "pending_reserved_tokens",
        "pending_reservation_count",
        "finalized_upper_bound_tokens",
        "finalized_upper_bound_count",
        "stale_reservation_count",
        "available_tokens",
    )
    for field in numeric:
        assert properties[field]["type"] == ["integer", "null"]
        assert properties[field]["default"] is None
        assert properties[field]["minimum"] == 0
    required = cast(list[str], schema["required"])
    assert not set(numeric).intersection(required)
    for field in ("oldest_reservation_at", "ledger_snapshot_at"):
        assert properties[field]["type"] == ["string", "null"]
        assert properties[field]["format"] == "date-time"


def test_event_contract_domain_mirror_matches_the_inline_extension() -> None:
    root = _yaml_document(CONTRACT.resolve())
    mirror = _yaml_document((CONTRACT.parent / "openapi" / "events" / "eventhub.yaml").resolve())[
        "eventContracts"
    ]

    def normalize(value: object) -> object:
        if isinstance(value, str):
            return value.removeprefix("../../openapi.yaml")
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    assert normalize(mirror) == root["x-event-contracts"]


def _assistant_operations(
    operations: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    return {operation for operation in operations if "/assistant/" in operation[0]}


def test_every_assistant_route_is_in_the_contract() -> None:
    documented = _operations(_documented_paths())
    served = _operations(app.openapi()["paths"])

    missing = sorted(_assistant_operations(served) - _assistant_operations(documented))
    stale = sorted(_assistant_operations(documented) - _assistant_operations(served))

    assert not missing, f"Routes the app serves but the contract omits: {missing}"
    assert not stale, f"Routes the contract claims but the app does not serve: {stale}"


def test_the_contract_never_claims_a_route_that_does_not_exist() -> None:
    """The direction that produces a broken client rather than an undocumented one."""
    documented = _operations(_documented_paths())
    served = _operations(app.openapi()["paths"])
    stale = sorted(op for op in documented - served if op[0] not in UNDOCUMENTED)
    assert not stale, f"Contract describes routes the app does not serve: {stale}"


def test_foundry_key_runtime_contract_is_distinct_from_managed_identity() -> None:
    schema = _documented_schemas()["GatewayRuntimeTarget"]
    alternatives = cast(list[dict[str, object]], schema["oneOf"])

    assert [set(cast(list[str], item["required"])) for item in alternatives] == [
        {"existing_id"},
        {"bedrock_runtime_url", "api_key"},
        {"foundry_project_endpoint"},
        {"foundry_project_endpoint", "foundry_inference_endpoint", "api_key"},
        {"openai_base_url", "api_key"},
    ]
    managed_identity_exclusions = cast(
        dict[str, list[dict[str, list[str]]]], alternatives[2]["not"]
    )["anyOf"]
    assert {"foundry_inference_endpoint"} in {
        frozenset(item["required"]) for item in managed_identity_exclusions
    }
    assert {"api_key"} in {frozenset(item["required"]) for item in managed_identity_exclusions}


def test_openai_connection_contract_exposes_no_registration_secret() -> None:
    schemas = _documented_schemas()
    connection = cast(dict[str, dict[str, object]], schemas["ModelConnectionCreate"]["properties"])
    provider = cast(dict[str, dict[str, object]], schemas["GatewayProviderTarget"]["properties"])
    runtime = cast(dict[str, dict[str, object]], schemas["GatewayRuntimeTarget"]["properties"])

    assert "api_key" not in connection
    assert connection["openai_base_url"]["pattern"] == "^https://"
    assert connection["model_vendor"]["enum"] == [
        "generic", "kimi", "deepseek", "openai", "anthropic",
    ]
    assert "openai_compatible" in cast(list[str], provider["template"]["enum"])
    assert runtime["api_key"]["writeOnly"] is True
    alternatives = cast(list[dict[str, object]], schemas["GatewayRuntimeTarget"]["oneOf"])
    for alternative in alternatives[:-1]:
        exclusions = cast(dict[str, list[dict[str, list[str]]]], alternative["not"])["anyOf"]
        assert {"openai_base_url"} in {frozenset(item["required"]) for item in exclusions}


def test_gateway_release_reads_separate_recorded_and_live_integrity() -> None:
    paths = _documented_paths()
    schemas = _documented_schemas()

    def get_operation(path: str) -> dict[str, object]:
        return cast(dict[str, object], paths[path]["get"])

    assert get_operation("/api/v1/model-management/releases")["operationId"] == (
        "listGatewayReleases"
    )
    assert (
        get_operation("/api/v1/model-management/releases/{release_id}")["operationId"]
        == "getGatewayRelease"
    )
    assert (
        get_operation("/api/v1/model-management/releases/{release_id}/diff")["operationId"]
        == "getGatewayReleaseDiff"
    )
    assert (
        get_operation("/api/v1/model-management/releases/{release_id}/integrity")["operationId"]
        == "getGatewayReleaseIntegrity"
    )
    dependencies = cast(
        dict[str, dict[str, object]],
        schemas["GatewayReleaseDependencies"]["properties"],
    )
    assert dependencies["live_status"]["enum"] == [
        "not_checked",
        "healthy",
        "missing",
        "mismatched",
    ]
    summary = cast(
        dict[str, dict[str, object]],
        schemas["GatewayReleaseSummary"]["properties"],
    )
    assert "rollback_eligible" in summary
    assert "rollback_blockers" in summary


def test_native_apim_pool_contract_is_bounded_and_model_scoped() -> None:
    paths = _documented_paths()
    schemas = _documented_schemas()

    pool_path = paths["/api/v1/model-management/models/{item_id}/backend-pool"]
    assert set(pool_path) == {"get", "put", "delete"}
    write = cast(
        dict[str, dict[str, object]],
        schemas["GatewayBackendPoolWrite"]["properties"],
    )
    rate_limit = cast(
        dict[str, dict[str, object]],
        schemas["GatewayRateLimitResilience"]["properties"],
    )
    breaker = cast(
        dict[str, dict[str, object]],
        schemas["GatewayRateLimitCircuitBreaker"]["properties"],
    )

    assert write["members"]["minItems"] == 2
    assert write["members"]["maxItems"] == 30
    pool_config = cast(
        dict[str, dict[str, object]], schemas["GatewayBackendPoolConfig"]["properties"]
    )
    registry = cast(dict[str, dict[str, object]], schemas["ModelRegistry"]["properties"])
    for field in (
        write["session_affinity"], pool_config["session_affinity"],
        registry["backend_pool_session_affinity_supported"],
    ):
        assert field["type"] == "boolean"
        assert field["default"] is False
    assert rate_limit["max_attempts_per_request"] == {
        "type": "integer",
        "const": 2,
    }
    assert breaker["failure_count"] == {"type": "integer", "const": 1}
    assert breaker["interval_seconds"] == {"type": "integer", "const": 60}
    assert breaker["trip_duration_seconds"] == {"type": "integer", "const": 60}
    assert breaker["accept_retry_after"] == {"type": "boolean", "const": True}
    assert rate_limit["backend_timeout_seconds"] == {
        "type": "integer",
        "const": 120,
        "default": 120,
    }
    assert breaker["status_code_ranges"]["default"] == [
        {"minimum": 429, "maximum": 429},
        {"minimum": 408, "maximum": 408},
        {"minimum": 500, "maximum": 599},
    ]
    assert breaker["error_reasons"]["default"] == [
        "BackendConnectionFailure",
        "Timeout",
    ]
    assert "existing compatible managed Runtimes" in str(
        cast(dict[str, object], pool_path["put"])["description"]
    )
