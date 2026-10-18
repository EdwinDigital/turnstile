"""Create the first password Owner and bootstrap the initial APIM target."""

from __future__ import annotations

import os
import sys
from typing import Protocol
from uuid import UUID

from backend.services.gateway_targets import (
    DEFAULT_SEEDED_APIM_GATEWAY_ID,
    register_existing_apim_target,
)
from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.gateway_targets import ApimTargetWrite
from turnstile_core.persistence.auth_store import AuthStore
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_contract import QueryRepository


class InitialOwnerStore(Protocol):
    def create_initial_owner(self, email: str, password_hash: str) -> dict[str, object] | None: ...


def bootstrap_initial_owner(store: InitialOwnerStore, settings: Settings) -> bool:
    email = settings.bootstrap_owner_email.strip().lower()
    configured_hash = settings.bootstrap_owner_password_hash
    if not email or configured_hash is None:
        return False
    return store.create_initial_owner(email, configured_hash.get_secret_value()) is not None


def bootstrap_initial_apim_target(
    repository: QueryRepository,
    settings: Settings,
    *,
    tenant_id: str | None = None,
) -> bool:
    if repository.list_apim_targets():
        return False
    identity = settings.legacy_apim_identity
    gateway_url = (settings.apim_gateway_url or "").strip()
    resolved_tenant = (tenant_id or os.environ.get("AZURE_TENANT_ID") or "").strip()
    if identity is None or not gateway_url or not resolved_tenant:
        return False
    try:
        parsed_tenant = UUID(resolved_tenant)
    except ValueError:
        return False
    subscription_id = UUID(identity[0])
    resource_group = (settings.apim_resource_group or "").strip()
    service_name = (settings.apim_service_name or "").strip()
    api_id = (settings.apim_api_id or "").strip()
    arm_resource_id = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{service_name}"
    )
    registration = ApimTargetWrite.model_validate(
        {
            "gateway_profile_id": DEFAULT_SEEDED_APIM_GATEWAY_ID,
            "name": service_name,
            "tenant_id": parsed_tenant,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "service_name": service_name,
            "api_id": api_id,
            "management_endpoint": f"https://management.azure.com{arm_resource_id}",
            "gateway_endpoint": gateway_url,
            "probe_subscription_id": settings.apim_probe_subscription_id,
        }
    )
    register_existing_apim_target(
        registration,
        settings=settings,
        repository=repository,
    )
    return True


def main() -> int:
    settings = get_settings()
    if settings.bootstrap_owner_email:
        if not settings.database_url:
            print("DATABASE_URL is required to bootstrap the initial Owner.", file=sys.stderr)
            return 2
        store = AuthStore(settings.database_url)
        try:
            created = bootstrap_initial_owner(store, settings)
        finally:
            store.close()
        print("Initial Owner created." if created else "Initial Owner bootstrap skipped.")
    if settings.database_url and settings.legacy_apim_identity and settings.apim_gateway_url:
        repository = PostgreSqlOpsDbProxy(settings.database_url, apim_api_id=settings.apim_api_id)
        try:
            if bootstrap_initial_apim_target(repository, settings):
                print("Initial APIM target registered.")
        except Exception as error:
            print(f"Initial APIM target bootstrap skipped: {error}", file=sys.stderr)
        finally:
            repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())