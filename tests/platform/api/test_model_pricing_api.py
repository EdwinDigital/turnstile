from __future__ import annotations

from copy import deepcopy

from backend.api import app
from backend.http.session import require_authenticated_session
from backend.services.runtime_service import ModelRuntimeService
from tests.platform.api.test_model_management_api import (
    bedrock_publication,
    client,
    gateway_id,
)
from tests.platform.api.test_model_management_api import (
    management_runtime_service as management_runtime_service,
)
from tests.platform.api.test_model_management_api import (
    publication_api as publication_api,
)
from tests.platform.pricing.test_models_dev import document
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import CompositeCatalog
from turnstile_core.pricing.models_dev import ModelsDevCatalog


def test_owner_preview_is_read_only(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    model = publication_api.models[0]
    management_runtime_service._price_catalog = CompositeCatalog([
        ModelsDevCatalog(fetch=document),
    ])
    before = deepcopy(publication_api.models)
    response = client.post(
        "/api/v1/model-management/price-preview", headers={"Origin": "http://localhost:5173"},
        json={"model_id": str(model["id"]), "upstream_model_id": "gpt-5"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "matched"
    assert response.json()["context_window"] == 400000
    assert publication_api.models == before


def test_preview_rejects_an_untrusted_origin(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    response = client.post(
        "/api/v1/model-management/price-preview", headers={"Origin": "https://untrusted.test"},
        json={"model_id": str(publication_api.models[0]["id"])},
    )
    assert response.status_code == 403


def test_preview_is_owner_only(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    from dataclasses import replace

    owner = app.dependency_overrides[require_authenticated_session]()
    app.dependency_overrides[require_authenticated_session] = lambda: replace(owner, role="member")
    try:
        response = client.post(
            "/api/v1/model-management/price-preview",
            headers={"Origin": "http://localhost:5173"},
            json={"model_id": str(publication_api.models[0]["id"])},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: owner


def test_creation_carries_manual_choice_in_the_publication_snapshot(
    publication_api: InMemoryRepository, management_runtime_service: ModelRuntimeService,
) -> None:
    del management_runtime_service
    payload = bedrock_publication(gateway_id(publication_api))
    payload["model"]["price_source"] = "manual"  # type: ignore[index]
    response = client.post(
        "/api/v1/model-management/publications",
        headers={"Origin": "http://localhost:5173"}, json=payload,
    )
    assert response.status_code == 202
    snapshot = publication_api.gateway_publications[-1]["desired_spec"]["bindings"][-1]["model"]
    assert snapshot["price_configuration"]["price_source"] == "manual"
