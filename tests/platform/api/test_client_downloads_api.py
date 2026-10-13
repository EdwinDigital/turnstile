from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import quote
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi import HTTPException

from backend.api import app
from backend.http.client_downloads import client_download_service
from backend.services.auth_service import SessionIdentity
from backend.services.client_downloads import ClientDownloadRequest, ClientDownloadService
from tests.backend.model_platform.test_application_access import _discovery
from tests.platform.api.api_support import client
from tests.platform.api.test_application_access_api import (
    ForbiddenSubscriptionKeyClient,
    StubSubscriptionKeyClient,
)
from turnstile_core.config import Settings
from turnstile_core.domain.application_access import GatewayApplicationModelAccessUpdate
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

pytest_plugins = ["tests.platform.api.api_fixtures"]


@pytest.fixture
def service() -> Iterator[ClientDownloadService]:
    repository = InMemoryRepository()
    gateway = repository.gateways[0]
    gateway["base_url"] = "https://gateway.example.com/turnstile/llm"
    publication_id = uuid4()
    repository.effective_gateway_releases[gateway["id"]] = publication_id
    repository.gateway_publications.append({
        "id": publication_id,
        "desired_spec": {"bindings": [], "discovery_models": [
            {"id": model["model_key"]} for model in repository.models
        ]},
    })
    key_client = StubSubscriptionKeyClient()
    applications = ApplicationAccessService(
        repository, sync_available=True, key_management_available=True, key_client=key_client,
    )
    applications.sync_discovery(_discovery("cc-switch", "turnstile-dashboard", "master"), "test")
    value = ClientDownloadService(
        repository, applications, Settings(apim_gateway_url=str(gateway["base_url"])),
    )
    app.dependency_overrides[client_download_service] = lambda: value
    yield value
    app.dependency_overrides.pop(client_download_service, None)


def identity(role: str = "owner") -> SessionIdentity:
    return SessionIdentity(
        id=str(uuid4()), email="edwin@example.com", name="Test",
        role="owner" if role == "owner" else "member", method="password",
        session_expires_at=datetime.now(UTC),
    )


def test_subscription_download_includes_full_platform_prices(
    service: ClientDownloadService,
) -> None:
    option = service.options(identity()).items[0]
    repo = service.repository
    assert isinstance(repo, InMemoryRepository)
    row = next(model for model in repo.models if model["model_key"] == option.models[0].id)
    row.update(input_cost_per_million=2.0, output_cost_per_million=10.0,
               cached_cost_per_million=0.2, cache_write_cost_per_million=2.5)
    response = client.post("/api/v1/client-downloads/magpie", json={
        "application_id": str(option.application_id),
        "subscription_id": str(option.subscription_id),
        "gateway_id": str(option.gateway_id), "key_kind": "primary",
    })
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        config = json.loads(archive.read("aigateway-proxy/aigateway.config.json"))
        assert config["models"][row["model_key"]]["cost"] == {
            "input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5,
        }
        assert "aigateway-proxy/pricing.mjs" in archive.namelist()


def test_options_filter_system_subscriptions_and_never_read_keys(
    service: ClientDownloadService,
) -> None:
    response = client.get("/api/v1/client-downloads/options")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [item["subscription_name"] for item in response.json()["items"]] == ["Cc Switch"]
    assert "secret" not in response.text
    assert response.json()["items"][0]["models"]


def test_download_is_private_complete_zip(service: ClientDownloadService) -> None:
    selected = service.options(identity()).items[0]
    request = ClientDownloadRequest(
        application_id=selected.application_id, subscription_id=selected.subscription_id,
        gateway_id=selected.gateway_id, key_kind="secondary",
    )
    response = client.post("/api/v1/client-downloads/magpie", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["access-control-expose-headers"] == "Content-Disposition"
    disposition = response.headers["content-disposition"]
    assert 'filename="aigateway-proxy-Cc-Switch.zip"' in disposition
    assert str(selected.subscription_id) not in disposition
    assert selected.subscription_id.hex not in disposition
    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            f"aigateway-proxy/{name}"
            for name in (
                "index.mjs", "pricing.mjs", "package.json", "README.md", "aigateway.config.json",
                "icons/icon.svg", "icons/icon-light.svg", "icons/icon-dark.svg",
                "icons/manifest.json",
            )
        }
        config = json.loads(archive.read("aigateway-proxy/aigateway.config.json"))
        assert config["subscriptionKey"] == "secondary-secret"
        assert config["subscriptionId"] == str(selected.subscription_id)
        assert config["providerId"].startswith("aigateway-subscription-cc-switch-")
        assert selected.subscription_id.hex not in config["providerId"]
        assert config["name"] == f"AIGateway Subscription · {selected.subscription_name}"
        assert config["accountId"] == selected.subscription_name
        package = json.loads(archive.read("aigateway-proxy/package.json"))
        assert package["version"] == "0.1.3"
        assert package["magpie"]["icon"] == config["icon"]
        template = archive.read("aigateway-proxy/index.mjs").decode()
        assert "primary-secret" not in template and "secondary-secret" not in template
        assert all((item.external_attr >> 16) & 0o777 == 0o600 for item in archive.infolist())


def test_subscription_filename_preserves_unicode_and_sanitizes_paths(
    service: ClientDownloadService,
) -> None:
    selected = service.options(identity()).items[0]
    repository = service.repository
    assert isinstance(repository, InMemoryRepository)
    subscription = next(
        row for row in repository.gateway_application_subscriptions
        if row["id"] == selected.subscription_id
    )
    subscription["display_name"] = '../../研发 "Agent": A\\B\r\n'
    response = client.post("/api/v1/client-downloads/magpie", json={
        "application_id": str(selected.application_id),
        "subscription_id": str(selected.subscription_id), "gateway_id": str(selected.gateway_id),
    })
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert f"filename*=UTF-8''{quote('aigateway-proxy-研发-Agent-A-B.zip')}" in disposition
    assert str(selected.subscription_id) not in disposition


@pytest.mark.parametrize("change", ["disabled-gateway", "foreign-gateway",
                                   "suspended-subscription", "missing-scope", "retired-application",
])
def test_invalid_state_is_rechecked(service: ClientDownloadService, change: str) -> None:
    selected = service.options(identity()).items[0]
    repository = service.repository
    assert isinstance(repository, InMemoryRepository)
    person = identity()
    if change == "disabled-gateway":
        repository.gateways[0]["enabled"] = False
    elif change == "foreign-gateway":
        repository.gateways[0]["base_url"] = "https://attacker.example.com/turnstile/llm"
    elif change == "suspended-subscription":
        repository.gateway_application_subscriptions[0]["state"] = "suspended"
    elif change == "missing-scope":
        repository.gateway_application_subscriptions[0]["scope_exists"] = False
    else:
        repository.gateway_applications[0]["status"] = "retired"
    with pytest.raises(HTTPException):
        service.download(person, ClientDownloadRequest(
            application_id=selected.application_id, subscription_id=selected.subscription_id,
            gateway_id=selected.gateway_id,
        ))


def test_deny_all_models_does_not_fall_back(service: ClientDownloadService) -> None:
    selected = service.options(identity()).items[0]
    service.applications.update_application_model_access(
        selected.application_id,
        GatewayApplicationModelAccessUpdate(mode="restricted", model_ids=[]),
        "test",
    )
    assert service.options(identity()).items[0].models == []
    with pytest.raises(HTTPException) as error:
        service.download(identity(), ClientDownloadRequest(
            application_id=selected.application_id, subscription_id=selected.subscription_id,
            gateway_id=selected.gateway_id,
        ))
    assert error.value.status_code == 409


def test_export_rejects_wrong_gateway_and_cross_origin(service: ClientDownloadService) -> None:
    selected = service.options(identity()).items[0]
    data = {"application_id": str(selected.application_id),
            "subscription_id": str(selected.subscription_id), "gateway_id": str(uuid4())}
    assert client.post("/api/v1/client-downloads/magpie", json=data).status_code == 404
    assert client.post("/api/v1/client-downloads/magpie", json=data,
                       headers={"Origin": "https://attacker.example.com"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/v1/client-downloads/options").status_code == 401


def test_validation_body_limits_and_secret_errors(service: ClientDownloadService) -> None:
    selected = service.options(identity()).items[0]
    request = {"application_id": str(selected.application_id),
               "subscription_id": str(selected.subscription_id),
               "gateway_id": str(selected.gateway_id)}
    invalid = client.post("/api/v1/client-downloads/magpie", json={**request, "key": "DO-NOT-ECHO"})
    assert invalid.status_code == 422 and "DO-NOT-ECHO" not in invalid.text
    assert client.post("/api/v1/client-downloads/magpie", content=b"x" * 8193).status_code == 413
    service.applications._key_client = ForbiddenSubscriptionKeyClient()
    response = client.post("/api/v1/client-downloads/magpie", json=request)
    assert response.status_code == 503 and "secret" not in response.text
    assert response.headers["cache-control"] == "no-store"
