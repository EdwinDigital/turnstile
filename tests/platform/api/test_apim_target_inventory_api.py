from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.api import app, control_plane_service
from backend.http.session import SessionIdentity, require_authenticated_session
from tests.backend.model_platform.test_gateway_targets import TARGET, deployment, target
from tests.platform.api.test_model_management_api import (
    client,
)
from tests.platform.api.test_model_management_api import (
    management_runtime_service as management_runtime_service,
)
from tests.platform.api.test_model_management_api import (
    publication_api as publication_api,
)
from turnstile_core.config import Settings
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.control_plane import GatewayControlPlaneService


def test_target_inventory_sync_is_a_durable_owner_operation(
    publication_api: InMemoryRepository,
) -> None:
    path = f"/api/v1/model-management/apim-targets/{TARGET}/inventory-sync"
    assert client.post(path).status_code == 503
    publication_api.upsert_apim_target(target())
    service = GatewayControlPlaneService(publication_api, target_inventory_sync_enabled=True)
    with patch.dict(app.dependency_overrides, {control_plane_service: lambda: service}):
        response = client.post(path)
        assert response.status_code == 202
        assert response.json()["operation"]["operation_kind"] == "target_inventory_sync"
        assert response.json()["operation"]["status"] == "queued"
        assert client.post(path).status_code == 409
    assert len(publication_api.gateway_release_operations) == 1


def payload() -> dict[str, str]:
    return {
        "gateway_profile_id": str(TARGET),
        "name": "Unit APIM",
        "tenant_id": str(target().tenant_id),
        "subscription_id": str(target().subscription_id),
        "resource_group": "unit-group",
        "service_name": "unit-service",
        "api_id": "unit-api",
        "management_endpoint": "https://management.azure.com" + target().arm_resource_id,
        "gateway_endpoint": "https://unit.azure-api.net/unit/llm",
    }


def test_target_api_returns_persisted_topology_and_not_found(
    publication_api: InMemoryRepository,
) -> None:
    assert client.get("/api/v1/model-management/apim-targets").json()["total"] == 0
    assert client.get(f"/api/v1/model-management/apim-targets/{TARGET}").status_code == 404
    publication_api.upsert_apim_target(target())
    publication_api.upsert_gateway_deployment(deployment())
    response = client.get("/api/v1/model-management/apim-targets")
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["connection_state"] == "unknown"


@pytest.mark.usefixtures("management_runtime_service")
def test_target_registration_honors_feature_and_management_gates(
    publication_api: InMemoryRepository,
) -> None:
    path = "/api/v1/model-management/apim-targets"
    with patch("backend.http.model_platform.get_settings", return_value=Settings.model_construct()):
        assert client.post(path, json=payload()).status_code == 401
        response = client.post(
            path, json=payload(), headers={"Authorization": "Bearer management-secret"}
        )
        assert response.status_code == 503
    publication_api.upsert_apim_target(target())
    expected = GatewayControlPlaneService(publication_api).apim_target_topology(TARGET)
    with (
        patch(
            "backend.http.model_platform.get_settings",
            return_value=Settings.model_construct(
                apim_target_registry_enabled=True,
            ),
        ),
        patch("backend.http.model_platform.register_existing_apim_target", return_value=expected),
    ):
        response = client.post(
            path, json=payload(), headers={"Authorization": "Bearer management-secret"}
        )
    assert response.status_code == 201
    assert response.json()["target"]["gateway_profile_id"] == str(TARGET)


@pytest.mark.usefixtures("management_runtime_service")
@pytest.mark.parametrize("access", ["anonymous", "member", "foreign-origin"])
def test_target_registration_preserves_session_owner_and_origin_guards(
    publication_api: InMemoryRepository,
    access: str,
) -> None:
    del publication_api
    owner = app.dependency_overrides[require_authenticated_session]()

    def identity() -> SessionIdentity:
        if access == "anonymous":
            raise HTTPException(status_code=401, detail="Sign in required")
        return SessionIdentity(
            id=owner.id,
            email=owner.email,
            name=owner.name,
            method=owner.method,
            role="member" if access == "member" else "owner",
            session_expires_at=owner.session_expires_at,
        )

    headers = {"Authorization": "Bearer management-secret"}
    if access == "foreign-origin":
        headers["Origin"] = "https://foreign.example.test"
    with (
        patch.dict(app.dependency_overrides, {require_authenticated_session: identity}),
        patch("backend.http.model_platform.register_existing_apim_target") as register,
    ):
        response = client.post(
            "/api/v1/model-management/apim-targets", json=payload(), headers=headers
        )
    assert response.status_code == (401 if access == "anonymous" else 403)
    register.assert_not_called()
