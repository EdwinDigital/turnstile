from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from backend.api import app
from backend.http.client_user import client_user_service
from backend.services.client_user import ClientUserService
from backend.services.client_user_store import ClientUserStore
from tests.backend.model_platform.test_application_access import _discovery
from tests.platform.api.api_support import OWNER_SESSION, client
from tests.platform.api.test_application_access_api import StubSubscriptionKeyClient
from turnstile_core.config import Settings
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService

pytest_plugins = [
    "tests.platform.api.api_fixtures",
    "tests.backend.persistence.test_client_user_grants",
]
USER = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def desktop(desktop_postgres: ClientUserStore) -> Any:
    with desktop_postgres._connection() as c:
        c.execute(
            "INSERT INTO app_user(id,email,role) VALUES(%s,'owner@contoso.com','owner') "
            "ON CONFLICT DO NOTHING",
            (USER,),
        )
    repo = InMemoryRepository()
    gateway = repo.gateways[0]
    gateway["base_url"] = "https://gateway.example.com/turnstile/llm"
    publication = uuid4()
    repo.effective_gateway_releases[gateway["id"]] = publication
    repo.gateway_publications.append(
        {
            "id": publication,
            "desired_spec": {
                "bindings": [],
                "discovery_models": [{"id": x["model_key"]} for x in repo.models],
            },
        }
    )
    applications = ApplicationAccessService(
        repo,
        sync_available=True,
        key_management_available=True,
        key_client=StubSubscriptionKeyClient(),
    )
    applications.sync_discovery(_discovery("cc-switch"), "test")
    svc = ClientUserService(
        desktop_postgres,
        repo,
        applications,
        Settings(
            credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
            apim_gateway_url=str(gateway["base_url"]),
        ),
        enabled=True,
        origin="https://platform.example",
    )
    app.dependency_overrides[client_user_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(client_user_service, None)


def begin(service: ClientUserService, purpose: str = "import") -> dict[str, Any]:
    response = client.post(
        "/api/v1/client-auth/device-authorizations",
        json={
            "user_id": USER,
            "binding": service._binding(USER),
            "installation_id": str(uuid4()),
            "purpose": purpose,
        },
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def test_browser_approval_and_native_catalog(desktop: ClientUserService) -> None:
    options = client.get("/api/v1/client-downloads/user-options")
    assert options.status_code == 200
    assert options.headers["cache-control"] == "no-store"
    device = begin(desktop)
    uri = "/api/v1/client-auth/verification?user_code=" + device["user_code"]
    response = client.get(uri)
    assert response.status_code == 200, response.text
    chosen = response.json()["subscriptions"][0]
    fields = {k: chosen[k] for k in ("application_id", "subscription_id", "gateway_id")}
    approved = client.post(
        "/api/v1/client-auth/decision",
        json={
            "user_code": device["user_code"],
            "approve": True,
            **fields,
        },
    )
    assert approved.status_code == 200, approved.text
    token = client.post(
        "/api/v1/client-auth/token",
        json={
            "grant_type": "device_code",
            "device_code": device["device_code"],
        },
    )
    assert token.status_code == 200, token.text
    access = token.json()["access_token"]
    auth = {"Authorization": "Bearer " + access}
    models = client.get("/api/v1/client-runtime/catalog", headers=auth)
    assert models.status_code == 200, models.text
    assert models.json()["models"] and models.json()["aliases"]
    assert models.headers["cache-control"] == "no-store"
    usage = client.get("/api/v1/client-runtime/usage", headers=auth)
    assert usage.status_code == 200 and "windows" in usage.json()
    assert "subscriptionKey" not in models.text and "primary-secret" not in models.text
    revoked = client.post("/api/v1/client-auth/revoke", headers=auth)
    assert revoked.status_code == 200
    assert client.get("/api/v1/client-runtime/catalog", headers=auth).status_code == 401


def test_user_binding_session_and_sensitive_validation(desktop: ClientUserService) -> None:
    oversized = client.post(
        "/api/v1/client-auth/token",
        content=b"x" * (16 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    invalid = client.post(
        "/api/v1/client-auth/device-authorizations",
        json={
            "user_id": str(uuid4()),
            "binding": desktop._binding(USER),
            "installation_id": str(uuid4()),
        },
    )
    assert invalid.status_code == 400
    malformed = client.post(
        "/api/v1/client-auth/token",
        json={
            "grant_type": "unknown",
            "refresh_token": "private-fixture-value",
        },
    )
    assert malformed.status_code == 422 and "private-fixture-value" not in malformed.text
    device = begin(desktop, "reauth")
    status = client.get(
        "/api/v1/client-auth/verification", params={"user_code": device["user_code"]}
    )
    assert status.json()["reauth_required"]
    denied = client.post(
        "/api/v1/client-auth/decision",
        json={
            "user_code": device["user_code"],
            "approve": True,
        },
    )
    assert denied.status_code == 409
    client.cookies.clear()
    try:
        assert client.post("/api/v1/client-downloads/magpie-user").status_code == 401
        assert client.get("/api/v1/client-runtime/catalog").status_code == 401
    finally:
        client.cookies.set("turnstile_session", OWNER_SESSION)
