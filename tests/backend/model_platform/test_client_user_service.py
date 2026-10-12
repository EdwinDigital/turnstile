from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, cast
from uuid import uuid4
from zipfile import ZipFile

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr

from backend.services.auth_service import SessionIdentity
from backend.services.client_user import ClientUserService
from backend.services.client_user_store import ClientUserStore
from tests.backend.model_platform.test_application_access import _discovery
from tests.platform.api.test_application_access_api import StubSubscriptionKeyClient
from turnstile_core.config import Settings
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService


class DesktopStore:
    def activate(self, grant_id: Any) -> dict[str, Any]:
        return {"id": grant_id, "activated": True}


def client_service() -> tuple[ClientUserService, InMemoryRepository, SessionIdentity]:
    repository = InMemoryRepository()
    gateway = repository.gateways[0]
    gateway["base_url"] = "https://gateway.example.com/turnstile/llm"
    pub = uuid4()
    repository.effective_gateway_releases[gateway["id"]] = pub
    repository.gateway_publications.append(
        {
            "id": pub,
            "desired_spec": {
                "bindings": [],
                "discovery_models": [{"id": m["model_key"]} for m in repository.models],
            },
        }
    )
    applications = ApplicationAccessService(
        repository,
        sync_available=True,
        key_management_available=True,
        key_client=StubSubscriptionKeyClient(),
    )
    applications.sync_discovery(_discovery("cc-switch", "master"), "test")
    settings = Settings(
        apim_gateway_url=str(gateway["base_url"]),
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    service = ClientUserService(
        cast(ClientUserStore, DesktopStore()),
        repository,
        applications,
        settings,
        enabled=True,
        origin="https://platform.example.com",
    )
    identity = SessionIdentity(
        id=str(uuid4()),
        email="test@example.com",
        name="Test",
        role="owner",
        method="password",
        session_expires_at=datetime.now(UTC),
    )
    return service, repository, identity


def test_personal_package_has_no_runtime_credentials_and_distinct_identity() -> None:
    service, _, identity = client_service()
    with ZipFile(BytesIO(service.package(identity))) as archive:
        assert all(name.startswith("aigateway-proxy-user/") for name in archive.namelist())
        config = json.loads(archive.read("aigateway-proxy-user/aigateway-user.config.json"))
        assert config["providerId"].startswith("aigateway-user-")
        assert config["userId"] == identity.id
        assert not {"access", "refresh", "subscriptionKey", "password"}.intersection(config)
        assert "images.mjs" in {name.rsplit("/", 1)[-1] for name in archive.namelist()}
        package = json.loads(archive.read("aigateway-proxy-user/package.json"))
        assert package["name"].startswith("opencode-aigateway-user-")
        assert package["version"] == "0.2.0"
        assert all((f.external_attr >> 16) & 0o777 == 0o600 for f in archive.infolist())


def test_personal_subscriptions_are_not_the_legacy_owner_inventory() -> None:
    service, repo, owner = client_service()
    assert len(service.subscriptions(owner)) == 1
    member = SessionIdentity(
        id=str(uuid4()),
        email="member@example.com",
        name="Member",
        role="member",
        method="password",
        session_expires_at=datetime.now(UTC),
    )
    assert service.subscriptions(member) == []
    repo.gateway_applications[0]["owner_id"] = member.id
    assert len(service.subscriptions(member, force=True)) == 1
    first = service.subscriptions(member)[0]
    repo.gateway_applications[0]["owner_id"] = "another-user"
    with pytest.raises(HTTPException):
        service.selected(member, first)


def test_catalog_protocol_aliases_and_live_empty_policy() -> None:
    service, repo, owner = client_service()
    choice = service.subscriptions(owner)[0]
    grant = {"id": uuid4(), "selection": choice}
    data = service.catalog(grant, owner)
    assert data["models"] and data["aliases"]
    assert all(
        m["provider"]["npm"] in {"@ai-sdk/openai", "@ai-sdk/anthropic", "@ai-sdk/openai-compatible"}
        for m in data["models"].values()
    )
    repo.gateway_application_subscriptions[0]["state"] = "suspended"
    with pytest.raises(HTTPException):
        service.catalog(grant, owner)
    assert service.catalog({"selection": None}, owner)["models"] == {}


def test_disabled_feature_cannot_export_a_user_package() -> None:
    service, _, owner = client_service()
    service.enabled = False
    with pytest.raises(HTTPException) as failure:
        service.package(owner)
    assert failure.value.status_code == 503
