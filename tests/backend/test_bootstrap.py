from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

from pydantic import SecretStr

from backend.bootstrap import bootstrap_initial_apim_target, bootstrap_initial_owner
from backend.services.auth_service import hash_password, verify_password
from backend.services.gateway_targets import DEFAULT_SEEDED_APIM_GATEWAY_ID
from turnstile_core.config import Settings
from turnstile_core.persistence.in_memory import InMemoryRepository


class CapturingInitialOwnerStore:
    def __init__(self, *, created: bool = True) -> None:
        self.created = created
        self.calls: list[tuple[str, str]] = []

    def create_initial_owner(self, email: str, password_hash: str) -> dict[str, object] | None:
        self.calls.append((email, password_hash))
        return {"role": "owner"} if self.created else None


def test_bootstrap_creates_owner_with_configured_hash() -> None:
    password_hash = hash_password("a-long-bootstrap-password")
    settings = Settings(
        bootstrap_owner_email=" Owner@Contoso.com ",
        bootstrap_owner_password_hash=SecretStr(password_hash),
    )
    store = CapturingInitialOwnerStore()

    assert bootstrap_initial_owner(store, settings)
    assert store.calls[0][0] == "owner@contoso.com"
    assert verify_password("a-long-bootstrap-password", store.calls[0][1])


def test_bootstrap_is_a_noop_when_users_already_exist() -> None:
    settings = Settings(
        bootstrap_owner_email="owner@contoso.com",
        bootstrap_owner_password_hash=SecretStr(hash_password("a-long-bootstrap-password")),
    )

    assert not bootstrap_initial_owner(CapturingInitialOwnerStore(created=False), settings)


def test_bootstrap_configuration_must_be_paired() -> None:
    try:
        Settings(bootstrap_owner_email="owner@contoso.com")
    except ValueError as error:
        assert "must be configured together" in str(error)
    else:
        raise AssertionError("one-sided bootstrap configuration was accepted")


def test_bootstrap_initial_apim_target_registers_seeded_profile_when_empty() -> None:
    repository = InMemoryRepository()
    settings = Settings(
        azure_subscription_id="00000000-0000-4000-8000-000000000010",
        apim_resource_group="unit-group",
        apim_service_name="unit-apim",
        apim_api_id="turnstile-llm",
        apim_gateway_url="https://unit-apim.azure-api.net/turnstile/llm",
    )
    with patch("backend.bootstrap.register_existing_apim_target") as register:
        created = bootstrap_initial_apim_target(
            repository,
            settings,
            tenant_id="00000000-0000-4000-8000-000000000020",
        )
    assert created is True
    register.assert_called_once()
    registration = register.call_args.args[0]
    assert registration.gateway_profile_id == DEFAULT_SEEDED_APIM_GATEWAY_ID
    assert registration.name == "unit-apim"
    assert registration.tenant_id == UUID("00000000-0000-4000-8000-000000000020")
    assert str(registration.gateway_endpoint) == "https://unit-apim.azure-api.net/turnstile/llm"