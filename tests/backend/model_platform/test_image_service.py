from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException

from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_image_options import _request
from tests.backend.model_platform.test_image_publication import image_publication
from turnstile_core.config import Settings
from turnstile_core.domain.control_plane import publication_model_id
from turnstile_core.domain.images import GeneratedImage, ImageInvocationRequest
from turnstile_core.domain.runtime_models import InvocationUsage
from turnstile_core.integrations.gateway import GatewayInvocationError, GatewayRouter
from turnstile_core.integrations.image_generation import ImageGatewayResult
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.control_plane import GatewayControlPlaneService


class ImageRouter(GatewayRouter):
    def __init__(self, failure: GatewayInvocationError | None = None) -> None:
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    def generate_image(
        self, request: ImageInvocationRequest, route: dict[str, Any]
    ) -> ImageGatewayResult:
        self.calls.append(route)
        if self.failure:
            raise self.failure
        return ImageGatewayResult(
            image=GeneratedImage(b64_json="unit-only-image", media_type="image/png"),
            usage=InvocationUsage(
                input_tokens=10, cached_tokens=2, output_tokens=20, estimated=False
            ),
            correlation_id="unit-image-correlation",
            size="32x48",
            quality=None,
        )


def image_fixture(assigned: bool = True) -> tuple[InMemoryRepository, ImageInvocationRequest]:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="unit-principal",
    ).publish(image_publication(repository), "owner@example.com")
    row = next(item for item in repository.gateway_publications if item["id"] == publication.id)
    row["status"] = "promoting"
    repository.claim_gateway_publication("unit-worker", 180)
    repository.activate_gateway_publication(publication.id, "unit-worker")
    model_id = publication_model_id(
        publication.id, publication.desired_spec.bindings[-1].model.model_key
    )
    request = _request(model_id=model_id)
    if assigned:
        repository.user_model_policies[request.metadata.user_id] = {
            "user_id": request.metadata.user_id,
            "model_ids": [model_id],
        }
    return repository, request


def test_image_service_records_exact_attempt_without_image_or_prompt() -> None:
    repository, request = image_fixture()
    router = ImageRouter()
    service = ModelRuntimeService(repository, Settings(), router)
    result = service.generate_image(request, role="owner")
    attempt = repository.billable_requests[UUID(result.request_id)]
    assert attempt.state == "exact" and attempt.actual_tokens == 32
    assert attempt.correlation_id == result.correlation_id
    assert isinstance(request.prompt, str)
    assert request.prompt not in str(attempt.model_dump())
    assert result.data[0].b64_json not in str(attempt.model_dump())
    assert len(router.calls) == 1


@pytest.mark.parametrize("measured", [False, True])
def test_image_failure_retains_uncertainty_or_real_usage(measured: bool) -> None:
    repository, request = image_fixture()
    usage = (
        InvocationUsage(input_tokens=10, cached_tokens=0, output_tokens=20, estimated=False)
        if measured
        else None
    )
    router = ImageRouter(
        GatewayInvocationError(
            "bounded failure",
            status_code=502,
            headers={"x-correlation-id": "failed"},
            usage=usage,
        )
    )
    service = ModelRuntimeService(repository, Settings(), router)
    with pytest.raises(HTTPException) as raised:
        service.generate_image(request, role="owner")
    assert raised.value.headers is not None
    attempt = repository.billable_requests[UUID(raised.value.headers["x-request-id"])]
    assert attempt.state == ("exact" if measured else "uncertain")
    assert attempt.actual_tokens == (30 if measured else None)
    assert len(router.calls) == 1


@pytest.mark.parametrize(
    "assigned,role,expected",
    [
        (False, "owner", 403),
        (True, "service", 403),
    ],
)
def test_image_denial_occurs_before_attempt_or_dispatch(
    assigned: bool, role: str, expected: int
) -> None:
    repository, request = image_fixture(assigned)
    router = ImageRouter()
    service = ModelRuntimeService(repository, Settings(), router)
    with pytest.raises(HTTPException) as raised:
        service.generate_image(request, role=role)
    assert raised.value.status_code == expected
    assert not repository.billable_requests and not router.calls
