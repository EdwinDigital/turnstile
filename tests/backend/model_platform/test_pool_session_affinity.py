from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from tests.backend.model_platform.control_plane_support import (
    GatewayControlPlaneService,
    StubTokenProvider,
    bedrock_publication,
    publisher_settings,
)
from turnstile_core.domain.control_plane import (
    COUNT_TOKENS_REJECTIONS,
    ApiFormat,
    AuthStrategy,
    GatewayBackendPoolConfig,
    GatewayBackendPoolMember,
    GatewayPublication,
    GatewayRateLimitCircuitBreaker,
    GatewayRateLimitResilience,
    PublicationKind,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    BackendPoolMemberResource,
    BackendPoolResource,
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.persistence.in_memory import InMemoryRepository


def _pooled_publication(affinity: bool = False) -> GatewayPublication:
    publication = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication("affinity-model"), "owner@example.com"
    )
    binding = publication.desired_spec.bindings[-1]
    primary_id = UUID("00000000-0000-4000-8000-000000000001")
    pool = GatewayBackendPoolConfig(
        members=[
            GatewayBackendPoolMember(
                runtime_id=primary_id,
                runtime_name="Primary connection",
                backend_url=HttpUrl(str(binding.backend_url)),
                auth_strategy=binding.auth_strategy,
                named_value_name=binding.named_value_name,
                priority=0,
                weight=1,
            ),
            GatewayBackendPoolMember(
                runtime_id=UUID("00000000-0000-4000-8000-000000000002"),
                runtime_name="Secondary connection",
                backend_url=HttpUrl("https://secondary.example.test"),
                auth_strategy=AuthStrategy.NAMED_VALUE_BEARER,
                named_value_name="test-secondary-key",
                priority=0,
                weight=1,
            ),
        ],
        rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2,
            retry_interval_seconds=1,
            first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(
                failure_count=1,
                interval_seconds=60,
                trip_duration_seconds=60,
                accept_retry_after=True,
            ),
        ),
    )
    config = {
        **binding.runtime_config,
        "apim_backend_pool": pool.model_dump(mode="json", exclude={"session_affinity"}),
    }
    if affinity:
        config["apim_backend_pool_session_affinity"] = True
    updated_binding = binding.model_copy(
        update={"runtime_id": primary_id, "runtime_config": config}
    )
    return GatewayPublication.model_validate(
        publication.model_copy(
            update={
                "desired_spec": publication.desired_spec.model_copy(
                    update={"bindings": [updated_binding]}
                ),
            }
        ).model_dump(mode="python")
    )


@pytest.mark.parametrize("affinity", (False, True))
def test_pool_affinity_sidecar_preserves_legacy_pool_records(affinity: bool) -> None:
    publication = _pooled_publication(affinity)
    binding = publication.desired_spec.bindings[-1]
    raw_pool = binding.runtime_config["apim_backend_pool"]
    assert isinstance(raw_pool, dict)
    assert "session_affinity" not in raw_pool
    pool = binding.backend_pool
    assert pool is not None and pool.session_affinity is affinity
    assert GatewayBackendPoolConfig.model_validate(raw_pool).session_affinity is False
    assert (
        publication.model_dump(mode="json")["desired_spec"]["bindings"][-1]["runtime_config"][
            "apim_backend_pool"
        ]
        == raw_pool
    )


@pytest.mark.parametrize("value", ("true", 1, [], {}))
def test_pool_affinity_sidecar_rejects_nonboolean_values(value: object) -> None:
    publication = _pooled_publication()
    payload = publication.model_dump(mode="python")
    payload["desired_spec"]["bindings"][-1]["runtime_config"][
        "apim_backend_pool_session_affinity"
    ] = value
    with pytest.raises(ValueError, match="must be a boolean"):
        GatewayPublication.model_validate(payload)


@pytest.mark.parametrize("affinity", (False, True))
def test_pool_resource_identity_is_stable_only_when_affinity_is_enabled(affinity: bool) -> None:
    publication = _pooled_publication(affinity)
    compiler = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.test",
        usage_observer_key_named_value="test-observer-key",
    )
    first = compiler.compile(publication)
    next_release = compiler.compile(publication.model_copy(update={"id": uuid4(), "generation": 2}))
    first_pool = next(item for item in first.backends if isinstance(item, BackendPoolResource))
    next_pool = next(
        item for item in next_release.backends if isinstance(item, BackendPoolResource)
    )
    assert [member.priority for member in first_pool.members] == [0, 0]
    assert [member.weight for member in first_pool.members] == [1, 1]
    assert (first_pool.id == next_pool.id) is affinity
    assert (
        [item.id for item in first.backends] == [item.id for item in next_release.backends]
    ) is affinity
    if affinity:
        assert first_pool.session_cookie_name is not None
        assert first_pool.session_cookie_name.startswith("TurnstileAffinity-")
        assert first_pool.session_cookie_name == next_pool.session_cookie_name
    else:
        assert first_pool.session_cookie_name is None
        assert first_pool.id.startswith(f"turnstile-pool-{publication.id.hex[:12]}-")
    binding = publication.desired_spec.bindings[-1]
    pool = binding.backend_pool
    assert pool is not None
    assert {
        compiler.pool_member_backend_id(publication, binding, member) for member in pool.members
    } == {member.backend_id for member in first_pool.members}


def test_pool_affinity_toggle_is_visible_as_a_release_change() -> None:
    old = _pooled_publication(False)
    binding = old.desired_spec.bindings[-1]
    new_binding = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool_session_affinity": True,
            }
        }
    )
    changed = old.model_copy(
        update={"desired_spec": old.desired_spec.model_copy(update={"bindings": [new_binding]})}
    )
    changes = GatewayControlPlaneService._release_changes(changed, old)
    assert changes.changed_models == [binding.model.model_key]
    assert changes.changed_backend_pools == []
    assert changes.added_backend_pools == []
    assert changes.removed_backend_pools == []


@pytest.mark.parametrize("affinity", (False, True))
def test_pool_affinity_arm_configuration_and_readback(affinity: bool) -> None:
    requests: list[httpx.Request] = []
    stored: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        expected_version = "2025-09-01-preview" if affinity else "2024-05-01"
        assert request.url.params["api-version"] == expected_version
        if request.method == "PUT":
            stored.update(json.loads(request.content)["properties"])
            return httpx.Response(201, json={"properties": stored})
        return httpx.Response(200, json={"properties": stored}) if stored else httpx.Response(404)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    backend = BackendPoolResource(
        id="test-pool",
        title="Test Pool",
        url="",
        members=(BackendPoolMemberResource("one", 0, 1), BackendPoolMemberResource("two", 0, 1)),
        session_cookie_name="TurnstileAffinity-test" if affinity else None,
    )
    client.ensure_backend(backend)
    assert [member["priority"] for member in stored["pool"]["services"]] == [1, 1]
    if affinity:
        assert requests[-1].method == "GET"
        assert stored["pool"]["sessionAffinity"] == {
            "sessionId": {"source": "cookie", "name": "TurnstileAffinity-test"}
        }
        stored["pool"]["sessionAffinity"]["sessionId"]["name"] = "unexpected-cookie"
        with pytest.raises(PolicyCompilationError, match="different settings"):
            client.ensure_backend(backend)
    else:
        assert "sessionAffinity" not in stored["pool"]
        client.ensure_backend(backend)
    assert sum(request.method == "PUT" for request in requests) == 1


def test_pool_affinity_readback_mismatch_never_reports_success() -> None:
    stored: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            stored.update(json.loads(request.content)["properties"])
            stored["pool"].pop("sessionAffinity")
            return httpx.Response(200)
        return httpx.Response(200, json={"properties": stored}) if stored else httpx.Response(404)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryablePublicationError, match="readback differs"):
        client.ensure_backend(
            BackendPoolResource(
                id="test-pool",
                title="Test Pool",
                url="",
                members=(BackendPoolMemberResource("one", 0, 1),),
                session_cookie_name="TurnstileAffinity-test",
            )
        )


def test_pool_integrity_classifies_members_by_resource_type() -> None:
    publication = _pooled_publication(True)
    compiled = ApimPolicyCompiler().compile(publication)
    expected_pool = next(
        item for item in compiled.backends if isinstance(item, BackendPoolResource)
    )
    publication = publication.model_copy(
        update={
            "resource_manifest": {
                "backends": [item.id for item in compiled.backends],
                "named_values": [],
            }
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/" + expected_pool.id):
            assert request.url.params["api-version"] == "2025-09-01-preview"
        return httpx.Response(404)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    dependencies = client.inspect_revision_dependencies(publication)
    assert dependencies.backend_pools == [expected_pool.id]
    assert dependencies.backends == sorted(
        item.id for item in compiled.backends if not isinstance(item, BackendPoolResource)
    )


def test_pool_baseline_selectors_are_read_from_raw_revision_policy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert ";rev=previous" in request.url.path
        if request.url.path.endswith("/operations"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "actual-chat-operation",
                            "properties": {"method": "POST", "urlTemplate": "/chat/completions"},
                        }
                    ]
                },
            )
        assert "/actual-chat-operation/policies/policy" in request.url.path
        assert request.url.params["format"] == "rawxml"
        return httpx.Response(
            200,
            headers={"content-type": "application/xml"},
            text=(
                '<policies><inbound><choose><when condition="@(true &amp;&amp; true)">'
                '<set-backend-service backend-id="old-member" />'
                "</when></choose></inbound></policies>"
            ),
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client._pool_baseline_backend_ids("previous", ApiFormat.OPENAI_CHAT) == {"old-member"}
    assert client._pool_baseline_backend_ids("previous", ApiFormat.ANTHROPIC_MESSAGES) == set()


@pytest.mark.parametrize("previous_affinity", (False, True))
@pytest.mark.parametrize("next_affinity", (False, True))
@pytest.mark.parametrize("known_member", (False, True))
def test_pool_probe_baseline_uses_only_verified_previous_member_ids(
    previous_affinity: bool,
    next_affinity: bool,
    known_member: bool,
) -> None:
    previous = _pooled_publication(previous_affinity)
    binding = previous.desired_spec.bindings[-1]
    compiler = ApimPolicyCompiler()
    pool = binding.backend_pool
    assert pool is not None
    old_member_ids = {
        compiler.pool_member_backend_id(previous, binding, member) for member in pool.members
    }
    next_binding = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool_session_affinity": next_affinity,
            }
        }
    )
    candidate = previous.model_copy(
        update={
            "id": uuid4(),
            "base_release_id": previous.id,
            "publication_kind": PublicationKind.ROUTE_RECONCILE,
            "desired_spec": previous.desired_spec.model_copy(update={"bindings": [next_binding]}),
            "resource_manifest": {"base_apim_revision": "previous"},
        }
    )
    next_member_ids = {
        compiler.pool_member_backend_id(candidate, next_binding, member) for member in pool.members
    }
    baseline_requests: list[httpx.Request] = []
    candidate_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/v1/models"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": model.id} for model in candidate.desired_spec.discovery_models]
                },
            )
        if request.method == "GET" and request.url.path.endswith("/operations"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "actual-message-operation",
                            "properties": {"method": "POST", "urlTemplate": "/v1/messages"},
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path.endswith("/policies/policy"):
            ids = sorted(old_member_ids) if known_member else ["unrelated-member"]
            selection = compiler._backend_selection("unit-old-pool", tuple(ids))
            policy = f"<policies><inbound>{selection}</inbound></policies>"
            return httpx.Response(200, headers={"content-type": "application/xml"}, text=policy)
        body = json.loads(request.content)
        if request.url.path.endswith("/count_tokens"):
            rejection = COUNT_TOKENS_REJECTIONS[
                "unavailable" if body["model"] == binding.model.model_key else "unpublished"
            ]
            return httpx.Response(rejection.status_code, json=rejection.payload())
        if body.get("model") != binding.model.model_key:
            rejection = COUNT_TOKENS_REJECTIONS["unpublished"]
            return httpx.Response(rejection.status_code, json=rejection.payload())
        if "x-turnstile-pool-member" not in request.headers:
            return httpx.Response(200, json={"content": []})
        if ";rev=previous" in request.url.path:
            baseline_requests.append(request)
            assert request.headers["x-turnstile-pool-member"] in old_member_ids
        else:
            candidate_requests.append(request)
            assert request.headers["x-turnstile-pool-member"] in next_member_ids
        return httpx.Response(403, json={"error": "preexisting-provider-failure"})

    settings = publisher_settings().model_copy(
        update={
            "apim_probe_subscription_key": SecretStr("unit-probe-key"),
            "apim_regression_model_key": binding.model.model_key,
        }
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if known_member:
        client.probe_revision("candidate", candidate, baseline_publication=previous)
        assert len(baseline_requests) == 2
        assert len(candidate_requests) == 2
    else:
        with pytest.raises(httpx.HTTPStatusError):
            client.probe_revision("candidate", candidate, baseline_publication=previous)
        assert baseline_requests == []
