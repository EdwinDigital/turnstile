from __future__ import annotations

import json
from uuid import UUID
from xml.etree import ElementTree

import httpx
import pytest
from pydantic import SecretStr

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    ROOT,
    FakeApimClient,
    GatewayControlPlaneService,
    GatewayPublicationWorker,
    StubTokenProvider,
    bedrock_publication,
    foundry_publication,
    publisher_settings,
    transition_publication,
)
from turnstile_core.domain.control_plane import (
    GatewayModelBinding,
    GatewayPublicationCreate,
    GatewayPublicationRetry,
    ModelCreateTarget,
    RuntimeTarget,
)
from turnstile_core.domain.runtime_models import ProviderTarget, databricks_connection_config
from turnstile_core.integrations.apim_control_plane import (
    ApimPolicyCompiler,
    AuthorizationRequiredError,
    AzureApimPublisherClient,
    BackendResource,
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_control_plane_contract import ImageProbeJournal
from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy
from turnstile_core.persistence.in_memory import InMemoryRepository


def test_compiler_sets_trusted_metadata_before_parent_admission() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "owner@example.com"
    )

    compiled = ApimPolicyCompiler().compile(publication)
    ElementTree.fromstring(compiled.chat_completions_policy)
    ElementTree.fromstring(compiled.messages_policy)
    ElementTree.fromstring(compiled.count_tokens_policy)
    ElementTree.fromstring(compiled.models_policy)

    before_parent = compiled.messages_policy.split("<base />", 1)[0]
    assert 'name="selectedProviderName" value="amazon_bedrock"' in before_parent
    assert (
        'name="selectedRuntimeName" '
        'value="Amazon Bedrock Claude (ap-southeast-2) via APIM"'
        in before_parent
    )
    assert 'name="selectedRequiresAssignment"' in before_parent
    assert "context.Api.IsCurrentRevision == false" in before_parent
    assert "context.Subscription.Id == &quot;turnstile-publisher-probe&quot;" in before_parent
    assert "gateway-publication-probe" in before_parent
    assert 'body[&quot;stream&quot;] = false' in before_parent
    assert 'backend-id="turnstile-dyn-' in compiled.messages_policy
    named_value_name = publication.desired_spec.bindings[-1].named_value_name
    assert named_value_name is not None
    assert f"Bearer {{{{{named_value_name}}}}}" in compiled.messages_policy
    assert "The requested model is not published" in compiled.messages_policy
    assert 'name="selectedModelKnown"' in before_parent
    assert compiled.messages_policy.index("<base />") < compiled.messages_policy.index(
        "The requested model is not published"
    )
    assert "Token counting is unavailable" in compiled.count_tokens_policy
    assert "claude-sonnet-4-6-bedrock" in compiled.models_policy
    assert "claude-sonnet-4-6-bedrock" not in compiled.chat_completions_policy
    assert "gpt-5.6-luna" in compiled.chat_completions_policy
    assert "gpt-5.6-luna" not in compiled.messages_policy
    assert len(compiled.backends) == 1
    assert compiled.backends[0].url == "https://bedrock-runtime.ap-southeast-2.amazonaws.com"
    assert 'name="x-hive-runtime" exists-action="override"' in before_parent

@pytest.mark.parametrize("oauth", (False, True))
def test_databricks_compiler_preserves_native_messages_and_scoped_authentication(
    oauth: bool,
) -> None:
    publication = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication(), "owner@example.com",
    )
    workspace = "https://adb-unit.1.azuredatabricks.net"
    config = databricks_connection_config(workspace, str(UUID(int=7)))
    if oauth:
        config.update(
            auth_strategy="oauth_client_credentials", managed_identity_resource=None,
            oauth={
                "provider_id": f"turnstile-oauth-{UUID(int=8).hex}",
                "client_id": str(UUID(int=9)), "token_url": f"{workspace}/oidc/v1/token",
            },
        )
    binding = GatewayModelBinding.model_validate({
        **publication.desired_spec.bindings[-1].model_dump(),
        "provider_brand_key": "azure_databricks", "runtime_brand_key": "azure_databricks",
        "runtime_name": "Unit Databricks", "backend_url": workspace,
        "backend_path": config["backend_path"], "auth_strategy": config["auth_strategy"],
        "managed_identity_resource": config["managed_identity_resource"],
        "named_value_name": None, "streaming_mode": "native", "runtime_config": config,
    })
    publication.desired_spec = publication.desired_spec.model_copy(update={"bindings": [binding]})
    compiled = ApimPolicyCompiler().compile(publication)
    policy = compiled.messages_policy
    ElementTree.fromstring(policy)
    assert "/serving-endpoints/anthropic/v1/messages" in policy
    assert 'name="anthropic-version"' in policy
    assert 'body[&quot;model&quot;]' in policy
    assert 'body.Remove(&quot;stream&quot;)' not in policy
    assert 'body.Remove(&quot;model&quot;)' not in policy
    unavailable = ElementTree.fromstring(compiled.count_tokens_policy).find("./inbound/choose/when")
    assert unavailable is not None
    assert binding.model.model_key in str(unavailable.get("condition"))
    if oauth:
        assert "get-authorization-context" in policy
        assert len(compiled.oauth_credentials) == 1 and not compiled.named_values
        assert compiled.oauth_credentials[0].client_secret is None
    else:
        assert 'resource="2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"' in policy
        assert not compiled.oauth_credentials and not compiled.named_values


def test_parent_policy_hooks_are_idempotent_and_preserve_inference_invariants() -> None:
    compiler = ApimPolicyCompiler()
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()

    patched = compiler.patch_parent_policy(source)
    assert compiler.patch_parent_policy(patched) == patched
    assert patched.count("<send-request") == 5
    assert patched.count("<log-to-eventhub") == 5
    assert "selectedProviderName" in patched
    assert "selectedRuntimeName" in patched
    assert "selectedRequiresAssignment" in patched
    assert "policy_unconfigured&quot; &amp;&amp; assignmentRequired" in patched
    assert patched.count("selectedModelKnown") == 2
    assert patched.index("validate-azure-ad-token") < patched.index("selectedModelKnown")
    assert patched.index("selectedModelKnown") < patched.index('name="inputBound"')
    assert patched.rindex("selectedModelKnown") < patched.index('name="subscriptionModel"')
    assert patched.rindex("selectedModelKnown") < patched.index(
        'name="applicationMapPartition"'
    )
    assert 'token-quota="__MONTHLY_TOKEN_QUOTA__"' not in patched
    assert patched.index('name="applicationMapPartition"') < patched.index(
        'name="effectiveApplicationTpm"'
    )

def test_parent_policy_rejects_publication_without_compact_admission() -> None:
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    source = source.replace('name="isResponsesCompactOperation"', 'name="removedCompact"')

    with pytest.raises(
        PolicyCompilationError,
        match="does not contain governed Responses compact admission",
    ):
        ApimPolicyCompiler().patch_parent_policy(source)

def test_parent_policy_rejects_direct_executable_children_under_choose() -> None:
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    root = parse_policy(source)
    choose = root.find("./inbound/choose")
    assert choose is not None
    ElementTree.SubElement(
        choose, "set-variable", {"name": "invalidDirectChild", "value": "@(false)"}
    )
    malformed = serialize_policy(root)

    with pytest.raises(
        PolicyCompilationError,
        match="invalid direct child under choose",
    ):
        ApimPolicyCompiler().patch_parent_policy(malformed)

def test_parent_policy_patch_tolerates_portal_whitespace() -> None:
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    source = source.replace(
        '        <set-header name="x-request-source" exists-action="override">\n'
        "          <value>employee-desktop</value>\n"
        "        </set-header>",
        '                                <set-header name="x-request-source" '
        'exists-action="override">\n'
        "                                        <value>employee-desktop</value>\n"
        "                                </set-header>",
    ).replace(
        "        <!-- A subscription caller that omits the attribution headers",
        "                                <!-- A subscription caller that omits the "
        "attribution headers",
    )

    patched = ApimPolicyCompiler().patch_parent_policy(source)

    assert patched.count("selectedModelKnown") == 2
    ElementTree.fromstring(patched)

def test_parent_policy_patch_guards_a_live_legacy_provider_fallback() -> None:
        source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
        legacy = """    <choose>
            <when condition="@((bool)context.Variables[&quot;isAnthropicOperation&quot;])">
                <set-backend-service backend-id="legacy-databricks" />
                <authentication-managed-identity resource="2ff814a6-3304-4ab8-85cb-cd0e6f879c1d" />
            </when>
            <otherwise>
                <set-backend-service backend-id="legacy-foundry" />
                <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
            </otherwise>
        </choose>
"""
        source = source.replace("  </inbound>", legacy + "  </inbound>", 1)

        patched = ApimPolicyCompiler().patch_parent_policy(source)

        assert 'backend-id="legacy-databricks"' in patched
        assert 'backend-id="legacy-foundry"' in patched
        assert patched.count(
            "!context.Variables.GetValueOrDefault&lt;bool&gt;"
            "(&quot;selectedRoutingManaged&quot;, false)"
        ) == 2
        legacy_slice = patched[
            patched.index('backend-id="legacy-databricks"') - 300 :
            patched.index('backend-id="legacy-foundry"') + 300
        ]
        assert "<otherwise>" not in legacy_slice
        assert 'name="telemetryWorkflow"' in patched
        assert patched.count('name="x-hive-user" exists-action="delete"') == 1
        assert ApimPolicyCompiler().patch_parent_policy(patched) == patched
        ElementTree.fromstring(patched)

def test_parent_policy_patch_excludes_responses_from_chat_stream_usage() -> None:
        source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
        legacy = """    <choose>
            <when condition="@(!(bool)context.Variables[&quot;isAnthropicOperation&quot;])">
                <set-body>@{
                    var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
                    var streamOptions = body[&quot;stream_options&quot;] as JObject
                        ?? new JObject();
                    streamOptions[&quot;include_usage&quot;] = true;
                    body[&quot;stream_options&quot;] = streamOptions;
                    return body.ToString(Newtonsoft.Json.Formatting.None);
                }</set-body>
            </when>
        </choose>
"""
        source = source.replace("  </inbound>", legacy + "  </inbound>", 1)
        compiler = ApimPolicyCompiler()

        patched = compiler.patch_parent_policy(source)

        assert (
                '@(!(bool)context.Variables[&quot;isAnthropicOperation&quot;] &amp;&amp; '
                '!(bool)context.Variables[&quot;isResponsesOperation&quot;])'
                in patched
        )
        assert compiler.patch_parent_policy(patched) == patched
        ElementTree.fromstring(patched)

def test_parent_policy_header_hygiene_patch_is_idempotent() -> None:
    compiler = ApimPolicyCompiler()
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()

    first = compiler.patch_parent_policy(source)
    second = compiler.patch_parent_policy(first)

    assert second == first
    assert first.count('name="telemetryWorkflow"') == 1
    assert first.count('name="telemetryOrganization"') == 1
    assert first.count('name="x-request-id" exists-action="delete"') == 1
    assert first.count('name="x-request-source" exists-action="delete"') == 1
    assert first.count('name="x-hive-organization" exists-action="delete"') == 1
    assert first.count('name="x-hive-runtime" exists-action="delete"') == 1
    codex_cleanup = first.index(
        'name="x-codex-installation-id" exists-action="delete"'
    )
    choose_start = first.rfind("<choose>", 0, codex_cleanup)
    assert "isResponsesOperation" in first[choose_start:codex_cleanup]

def test_compiled_release_never_contains_a_literal_credential() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "owner@example.com"
    )
    compiled = ApimPolicyCompiler().compile(publication)

    all_policy = "\n".join(
        (
            compiled.chat_completions_policy,
            compiled.responses_policy,
            compiled.responses_compact_policy,
            compiled.messages_policy,
            compiled.count_tokens_policy,
            compiled.models_policy,
        )
    )
    named_value_name = publication.desired_spec.bindings[-1].named_value_name
    assert named_value_name is not None and named_value_name in all_policy
    assert "bedrock-api-key" not in all_policy
    assert "AWS_SECRET" not in all_policy
    assert "AKIA" not in all_policy

def test_worker_promotes_only_after_dependencies_policies_and_probes() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "owner@example.com"
    )
    client = FakeApimClient()
    worker = GatewayPublicationWorker(
        repository,
        client,
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
    )

    statuses = []
    for _ in range(6):
        result = worker.run_once("worker-a")
        assert result is not None
        statuses.append(result.status.value)

    assert statuses == [
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
        "promoting",
        "active",
    ]
    call_names = [name for name, _ in client.calls]
    assert call_names.index("backend") < call_names.index("revision")
    assert call_names.count("revision") == 2
    assert call_names.count("chat-completions") == 1
    assert call_names.count("responses") == 1
    assert call_names.count("responses-compact") == 1
    assert call_names.index("api-policy") < call_names.index("probe")
    assert call_names.index("probe") < call_names.index("promote")
    assert repository.effective_gateway_releases[APIM_ID] == publication.id

def test_foundry_publication_waits_for_rbac_and_resumes_the_same_candidate() -> None:
    class AuthorizationClient(FakeApimClient):
        authorized = False

        def probe_revision(
            self,
            revision: str,
            publication: object,
            *,
            journal: ImageProbeJournal | None = None,
        ) -> None:
            if not self.authorized:
                raise AuthorizationRequiredError(
                    "Foundry rejected the APIM managed identity"
                )
            super().probe_revision(revision, publication, journal=journal)

    repository = InMemoryRepository()
    foundry_provider_ids = {
        item["id"]
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    repository.models = [
        item for item in repository.models if item["provider_id"] not in foundry_provider_ids
    ]
    repository.runtimes = [
        item for item in repository.runtimes if item["provider_id"] not in foundry_provider_ids
    ]
    repository.providers = [
        item for item in repository.providers if item["id"] not in foundry_provider_ids
    ]
    service = GatewayControlPlaneService(
        repository,
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    )
    service.publish(foundry_publication(), "owner@example.com")
    client = AuthorizationClient()
    worker = GatewayPublicationWorker(repository, client, client.policy)

    for expected in (
        "validating",
        "provisioning",
        "building_revision",
        "verifying",
    ):
        result = worker.run_once("worker")
        assert result is not None and result.status == expected
    waiting = worker.run_once("worker")

    assert waiting is not None and waiting.status == "awaiting_authorization"
    assert waiting.apim_revision is not None
    assert client.current == "1"
    assert APIM_ID not in repository.effective_gateway_releases
    assert service.publication(waiting.id).status == "awaiting_authorization"

    resumed = service.resume_authorization(waiting.id, "owner@example.com")
    client.authorized = True
    verified = worker.run_once("worker")
    active = worker.run_once("worker")

    assert resumed.status == "verifying"
    assert resumed.apim_revision == waiting.apim_revision
    assert verified is not None and verified.status == "promoting"
    assert active is not None and active.status == "active"

def test_failed_foundry_publication_retries_without_an_api_key() -> None:
    repository = InMemoryRepository()
    foundry_provider_ids = {
        item["id"]
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    repository.models = [
        item for item in repository.models if item["provider_id"] not in foundry_provider_ids
    ]
    repository.runtimes = [
        item for item in repository.runtimes if item["provider_id"] not in foundry_provider_ids
    ]
    repository.providers = [
        item for item in repository.providers if item["id"] not in foundry_provider_ids
    ]
    service = GatewayControlPlaneService(
        repository,
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    )
    publication = service.publish(foundry_publication(), "owner@example.com")
    transition_publication(
        repository,
        publication.id,
        "queued",
        "failed",
        {"error_code": "probe_failed", "error_message": "probe failed"},
        "worker",
    )

    retried = service.retry(
        publication.id,
        GatewayPublicationRetry(),
        "owner@example.com",
    )

    assert retried.status == "queued"
    assert retried.id == publication.id

def test_worker_removes_foundry_alias_before_physically_deleting_model() -> None:
    repository = InMemoryRepository()
    model = next(
        item for item in repository.models if item["model_key"] == "gpt-5.6-luna"
    )
    GatewayControlPlaneService(repository).remove_model(
        model["id"], "owner@example.com"
    )
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy)

    for _ in range(5):
        result = worker.run_once("worker")
        assert result is not None
        assert any(item["id"] == model["id"] for item in repository.models)

    active = worker.run_once("worker")

    assert active is not None and active.status.value == "active"
    assert all(item["id"] != model["id"] for item in repository.models)
    assert ("chat-completions", active.apim_revision) in client.calls
    assert not any(name == "backend" for name, _ in client.calls)

def test_managed_foundry_removal_preserves_the_shared_project_connection() -> None:
    repository = InMemoryRepository()
    foundry_provider_ids = {
        item["id"]
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    repository.models = [
        item for item in repository.models if item["provider_id"] not in foundry_provider_ids
    ]
    repository.runtimes = [
        item for item in repository.runtimes if item["provider_id"] not in foundry_provider_ids
    ]
    repository.providers = [
        item for item in repository.providers if item["id"] not in foundry_provider_ids
    ]
    service = GatewayControlPlaneService(
        repository,
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    )
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy)

    first_publication = service.publish(
        foundry_publication("gpt-4o"), "owner@example.com"
    )
    for _ in range(6):
        assert worker.run_once("worker") is not None
    first_model = next(
        item for item in repository.models if item["publication_id"] == first_publication.id
    )
    runtime = next(
        item for item in repository.runtimes if item["id"] == first_model["runtime_id"]
    )
    provider = next(
        item for item in repository.providers if item["id"] == first_model["provider_id"]
    )
    runtime_config = dict(runtime["config"])

    assert runtime_config["path"] == "/chat/completions"
    assert runtime_config["backend_path"] == "/openai/v1/chat/completions"

    second_publication = service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=provider["id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
                        model=ModelCreateTarget(deployment_name="routing-model"),
        ),
        "owner@example.com",
    )
    for _ in range(6):
        assert worker.run_once("worker") is not None
    second_model = next(
        item for item in repository.models if item["publication_id"] == second_publication.id
    )

    removal = service.remove_model(first_model["id"], "owner@example.com")
    compiled = ApimPolicyCompiler().compile(removal)

    assert first_model["model_key"] not in compiled.chat_completions_policy
    assert second_model["model_key"] in compiled.chat_completions_policy
    for _ in range(5):
        assert worker.run_once("worker") is not None
        assert any(item["id"] == first_model["id"] for item in repository.models)

    active = worker.run_once("worker")

    assert active is not None and active.status == "active"
    assert all(item["id"] != first_model["id"] for item in repository.models)
    assert any(item["id"] == second_model["id"] for item in repository.models)
    retained_runtime = next(
        item for item in repository.runtimes if item["id"] == runtime["id"]
    )
    assert retained_runtime["config"] == runtime_config
    assert any(item["id"] == provider["id"] for item in repository.providers)

def test_worker_failure_never_materializes_the_registry() -> None:
    class FailingClient(FakeApimClient):
        def ensure_backend(self, backend: BackendResource) -> None:
            raise RuntimeError(f"backend rejected: {backend.id}")

    repository = InMemoryRepository()
    GatewayControlPlaneService(repository).publish(bedrock_publication(), "owner@example.com")
    worker = GatewayPublicationWorker(
        repository,
        FailingClient(),
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
    )

    assert worker.run_once("worker-a").status == "validating"  # type: ignore[union-attr]
    failed = worker.run_once("worker-a")

    assert failed is not None and failed.status == "failed"
    assert "backend rejected" in (failed.error_message or "")
    assert all(model["model_key"] != "claude-sonnet-4-6-bedrock" for model in repository.models)
    assert APIM_ID not in repository.effective_gateway_releases

def test_transient_apim_failure_requeues_the_same_step() -> None:
    class RetryClient(FakeApimClient):
        failed_once = False

        def ensure_backend(self, backend: BackendResource) -> None:
            if not self.failed_once:
                self.failed_once = True
                raise RetryablePublicationError("APIM returned HTTP 429")
            super().ensure_backend(backend)

    repository = InMemoryRepository()
    GatewayControlPlaneService(repository).publish(bedrock_publication(), "owner@example.com")
    client = RetryClient()
    worker = GatewayPublicationWorker(
        repository,
        client,
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
    )

    assert worker.run_once("worker-a").status == "validating"  # type: ignore[union-attr]
    retry = worker.run_once("worker-a")
    advanced = worker.run_once("worker-a")

    assert retry is not None and retry.status == "validating"
    assert advanced is not None and advanced.status == "provisioning"

@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (404, {"error": "candidate not propagated"}),
        (200, {"data": []}),
    ],
)
def test_candidate_discovery_propagation_is_retryable(
    status_code: int, payload: dict[str, object]
) -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(repository).publish(
        bedrock_publication(), "owner@example.com"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(status_code, json=payload)

    settings = publisher_settings().model_copy(
        update={"apim_probe_subscription_key": SecretStr("probe-key")}
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RetryablePublicationError):
        client.probe_revision("turnstile-test", publication)

def test_first_foundry_regression_probe_reports_missing_authorization() -> None:
    repository = InMemoryRepository()
    foundry_provider_ids = {
        item["id"]
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    }
    repository.models = [
        item for item in repository.models if item["provider_id"] not in foundry_provider_ids
    ]
    repository.runtimes = [
        item for item in repository.runtimes if item["provider_id"] not in foundry_provider_ids
    ]
    repository.providers = [
        item for item in repository.providers if item["id"] not in foundry_provider_ids
    ]
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="39deeba0-9799-4806-96c4-2f65eb0f22d1",
    ).publish(foundry_publication(), "owner@example.com")
    alias = publication.desired_spec.bindings[-1].model.model_key

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": alias}]},
            )
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 8
        assert "max_tokens" not in payload
        return httpx.Response(403, json={"error": {"message": "Forbidden"}})

    settings = publisher_settings().model_copy(
        update={"apim_probe_subscription_key": SecretStr("probe-key")}
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(AuthorizationRequiredError):
        client.probe_revision("turnstile-test", publication)

def test_foundry_claude_candidate_probe_uses_messages_operation() -> None:
    repository = InMemoryRepository()
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    runtime = next(
        item for item in repository.runtimes if item["provider_id"] == provider["id"]
    )
    project_endpoint = (
        "https://example-foundry-resource.services.ai.azure.com/"
        "api/projects/example-project"
    )
    runtime["config"].update(
        control_plane_managed=True,
        project_endpoint=project_endpoint,
        backend_url=project_endpoint,
        auth_strategy="managed_identity",
        managed_identity_resource="https://ai.azure.com",
        streaming_mode="native",
    )
    publication = GatewayControlPlaneService(repository).publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=provider["id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(deployment_name="claude-opus-5"),
        ),
        "owner@example.com",
    )
    alias = publication.desired_spec.bindings[-1].model.model_key

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": item.id}
                        for item in publication.desired_spec.discovery_models
                    ]
                },
            )
        payload = json.loads(request.content)
        assert request.url.path.endswith("/v1/messages")
        assert payload["model"] == alias
        assert payload["max_tokens"] == 8
        assert "max_completion_tokens" not in payload
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "OK"}]},
        )

    settings = publisher_settings().model_copy(
        update={
            "apim_probe_subscription_key": SecretStr("probe-key"),
            "apim_regression_model_key": alias,
        }
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.probe_revision("turnstile-test", publication)
