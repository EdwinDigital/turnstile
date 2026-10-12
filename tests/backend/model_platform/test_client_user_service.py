from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any, cast
from unittest.mock import Mock
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
    def issue_download(self, *args: Any) -> None:
        pass

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


def test_personal_package_has_bound_authorization_and_distinct_identity() -> None:
    service, _, identity = client_service()
    with ZipFile(BytesIO(service.package(identity))) as archive:
        assert all(name.startswith("aigateway-proxy-user/") for name in archive.namelist())
        config = json.loads(archive.read("aigateway-proxy-user/aigateway-user.config.json"))
        assert config["providerId"].startswith("aigateway-user-")
        assert config["userId"] == identity.id
        assert not {"subscriptionKey", "password"}.intersection(config)
        assert config["authorization"]["access"].startswith("tsca_")
        assert config["authorization"]["refresh"].startswith("tscr_")
        assert config["authorization"]["installationId"] == config["installationId"]
        assert config["selection"]["mode"] == "personal"
        assert "subscription_id" not in config["selection"]
        assert config["authorization"]["accountId"] == identity.name
        assert config["authorization"]["uid"] == identity.id
        assert config["catalog"]["models"]
        assert "images.mjs" in {name.rsplit("/", 1)[-1] for name in archive.namelist()}
        package = json.loads(archive.read("aigateway-proxy-user/package.json"))
        assert package["name"].startswith("opencode-aigateway-user-")
        assert package["version"] == "0.4.0"
        assert all((f.external_attr >> 16) & 0o777 == 0o600 for f in archive.infolist())


def test_personal_routing_does_not_require_owned_applications() -> None:
    service, repo, owner = client_service()
    repo.gateway_applications.clear()
    repo.gateway_application_subscriptions.clear()
    member = replace(owner, role="member", id=str(uuid4()), email="member@example.com")
    selected = service.default_route(member)
    assert selected["mode"] == "personal"
    assert selected["models"]
    assert "application_id" not in selected
    repo.user_model_policies[member.email] = {"user_id": member.email, "model_ids": []}
    assert not service.selected(member, selected, fresh=True)["models"]


def test_catalog_protocol_aliases_and_live_empty_policy() -> None:
    service, repo, owner = client_service()
    choice = service.default_route(owner)
    grant = {"id": uuid4(), "selection": choice}
    data = service.catalog(grant, owner)
    assert data["models"] and data["aliases"]
    assert all(
        m["provider"]["npm"] in {"@ai-sdk/openai", "@ai-sdk/anthropic", "@ai-sdk/openai-compatible"}
        for m in data["models"].values()
    )
    repo.gateway_application_subscriptions[0]["state"] = "suspended"
    assert service.catalog(grant, owner)["models"]
    repo.user_model_policies[owner.email] = {"user_id": owner.email, "model_ids": []}
    assert service.catalog(grant, owner, fresh=True)["models"] == {}
    assert service.catalog({"id": uuid4(), "selection": None}, owner, fresh=True)["models"] == {}


def test_disabled_feature_cannot_export_a_user_package() -> None:
    service, _, owner = client_service()
    service.enabled = False
    with pytest.raises(HTTPException) as failure:
        service.package(owner)
    assert failure.value.status_code == 503


def test_personal_gateway_is_explicit_and_never_falls_back_to_applications() -> None:
    service, repo, owner = client_service()
    first = service.default_route(owner)
    repo.gateways[0]["config"]["client_user_default_subscriptions"] = {owner.id: str(uuid4())}
    assert service.default_route(owner) == first
    with pytest.raises(HTTPException) as failure:
        service.selected(owner, {"mode": "personal", "gateway_id": str(uuid4())}, fresh=True)
    assert failure.value.status_code == 409
    repo.gateways[0]["enabled"] = False
    with pytest.raises(HTTPException):
        service.default_route(owner)


def test_legacy_user_grants_normalize_to_personal_without_changing_history() -> None:
    service, repo, owner = client_service()
    old = {
        "application_id": str(uuid4()),
        "subscription_id": str(uuid4()),
        "gateway_id": str(uuid4()),
    }
    grant = {"id": uuid4(), "selection": old}
    repo.gateway_applications.clear()
    result = service.catalog(grant, owner)
    assert result["selection"]["mode"] == "personal"
    assert "application_id" not in result["selection"]
    assert grant["selection"] == old


def test_model_permission_revocation_is_checked_even_with_cached_catalog() -> None:
    service, repo, owner = client_service()
    selected = service.default_route(owner)
    model = selected["models"][0]
    service.validate_model(owner, selected, model)
    repo.user_model_policies[owner.email] = {"user_id": owner.email, "model_ids": []}
    with pytest.raises(HTTPException) as denied:
        service.validate_model(owner, selected, model)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("mode", ["audit", "block"])
def test_usage_shows_only_the_verified_personal_monthly_budget(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    service, repo, owner = client_service()
    owner = replace(owner, governance_user_id="person@example.com")
    personal = Mock(return_value={"used_tokens": 22555, "token_limit": 60000000, "mode": mode})
    application = Mock(side_effect=AssertionError("Usage must not read the shared application"))
    selection = Mock(side_effect=AssertionError("Usage must not depend on a subscription"))
    monkeypatch.setattr(repo, "person_budget_state", personal)
    monkeypatch.setattr(service.applications, "application", application)
    monkeypatch.setattr(service, "selected", selection)

    result = service.usage({"id": uuid4(), "selection": {"application_id": str(uuid4())}}, owner)

    assert result["user"] == "Test"
    assert len(result["windows"]) == 1
    window = result["windows"][0]
    assert window["name"] == "个人月度 Token"
    assert window["amount"] == 22555
    assert window["limit"] == 60000000
    assert window["unit"] == "tokens"
    assert window["used"] == pytest.approx(22555 / 60000000 * 100)
    assert window["aside"] is (mode == "audit")
    start = datetime.fromisoformat(result["as_of"]).date().replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    personal.assert_called_once_with("person@example.com", start, end)
    assert window["resetsAt"] == datetime(end.year, end.month, 1, tzinfo=UTC).isoformat()
    assert window["span"] == (end - start).days * 86400
    application.assert_not_called()
    selection.assert_not_called()


def test_usage_without_personal_budget_does_not_fall_back_to_an_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, owner = client_service()
    monkeypatch.setattr(repo, "person_budget_state", Mock(return_value=None))
    application = Mock(side_effect=AssertionError("No shared allowance fallback"))
    monkeypatch.setattr(service.applications, "application", application)

    result = service.usage({"id": uuid4(), "selection": {}}, owner)

    assert result["windows"] == []
    assert result["balance"] == "未配置额度"
    assert result["signIn"] == "kept"
    application.assert_not_called()


def test_personal_usage_keeps_the_sixty_second_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repo, owner = client_service()
    personal = Mock(return_value={"used_tokens": 10, "token_limit": 100, "mode": "audit"})
    clock = Mock(return_value=10)
    monkeypatch.setattr(repo, "person_budget_state", personal)
    monkeypatch.setattr("backend.services.client_user.time.monotonic", clock)
    grant = {"id": uuid4(), "selection": None}
    first = service.usage(grant, owner)
    personal.return_value = {"used_tokens": 20, "token_limit": 100, "mode": "audit"}
    clock.return_value = 69
    assert service.usage(grant, owner) == first
    personal.assert_called_once()
    clock.return_value = 71
    assert service.usage(grant, owner)["windows"][0]["amount"] == 20
    assert personal.call_count == 2
    assert first["cache_seconds"] == 60
