from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from turnstile_core.domain.image_profiles import (
    IMAGE_RESPONSE_SAFETY_BYTES,
    ImageGenerationLimits,
    create_image_profile,
    validate_image_profile,
)
from turnstile_core.domain.images import ImageGenerationOptions, ImageInvocationRequest
from turnstile_core.domain.runtime_models import InvocationMetadata


def _limits(**overrides: Any) -> ImageGenerationLimits:
    return ImageGenerationLimits.model_validate({
        "max_request_bytes": 4096,
        "max_response_bytes": IMAGE_RESPONSE_SAFETY_BYTES,
        "output_reservation_tokens": 2048,
        "timeout_seconds": 180,
        **overrides,
    })


def _request(**parameters: Any) -> ImageInvocationRequest:
    metadata = InvocationMetadata.model_validate({
        **{name: "unit" for name in InvocationMetadata.model_fields},
        "usage_domain": "apim",
        "turn_index": 1,
    })
    return ImageInvocationRequest.model_validate({
        "metadata": metadata,
        "model_id": "10000000-0000-4000-8000-000000000001",
        "prompt": "A ceramic cup",
        **parameters,
    })


def test_current_profile_has_only_budget_and_transport_configuration() -> None:
    limits = _limits()
    profile = create_image_profile(limits, "unit-image")
    assert profile.version == 4
    assert profile.configuration == limits
    assert profile.limits.burst_reservation_tokens == 6144
    assert "sizes" not in profile.model_dump()
    assert "default_quality" not in profile.model_dump()
    assert profile == validate_image_profile(profile.model_dump())
    assert profile == create_image_profile(limits, "unit-image")


def test_image_gateway_selection_is_complete_and_never_reaches_provider_payload() -> None:
    identity = "10000000-0000-4000-8000-000000000001"
    with pytest.raises(ValidationError):
        _request(gateway_profile_id=identity)
    with pytest.raises(ValidationError):
        _request(gateway_deployment_id=identity)
    selected = _request(gateway_profile_id=identity, gateway_deployment_id=identity)
    profile = create_image_profile(_limits(), "unit-image")
    payload = selected.provider_payload("unit-image", profile)
    platform_fields = {"gateway_profile_id", "gateway_deployment_id", "model_id", "metadata"}
    assert not platform_fields & payload.keys()
    assert selected.reservation_tokens("unit-image", profile) == _request().reservation_tokens(
        "unit-image", profile,
    )


@pytest.mark.parametrize("change", [
    {"provider_model": "different-model"}, {"max_request_bytes": 4000},
    {"backend_path": "/another-route"}, {"public_path": "/another-route"},
    {"default_quality": "low"}, {"version": 1},
])
def test_profile_rejects_modified_snapshots_and_private_legacy_configuration(
    change: dict[str, object],
) -> None:
    profile = create_image_profile(_limits(), "unit-image")
    with pytest.raises(ValueError):
        validate_image_profile({**profile.model_dump(), **change})


@pytest.mark.parametrize("change", [
    {"max_request_bytes": True}, {"max_request_bytes": 0},
    {"max_response_bytes": IMAGE_RESPONSE_SAFETY_BYTES + 1},
    {"output_reservation_tokens": 2**31 - 1}, {"timeout_seconds": 241},
    {"timeout_seconds": 180.0},
])
def test_transport_and_reservation_limits_are_strict(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _limits(**change)


@pytest.mark.parametrize("parameters", [
    {"prompt": " "}, {"prompt": "a" * 4001}, {"size": 42},
    {"quality": {"vendor": "new"}}, {"quality": None}, {"output_format": "webp"},
    {"background": "transparent"}, {"vendor_option": ["new", 3]},
])
def test_provider_parameters_pass_through_without_platform_defaults(
    parameters: dict[str, object],
) -> None:
    supplied = {"prompt": "A cup", **parameters}
    options = ImageGenerationOptions.model_validate(supplied)
    profile = create_image_profile(_limits(), "unit-image")
    assert options.resolve(profile).model_dump(exclude_unset=True) == supplied


@pytest.mark.parametrize("parameters", [
    {"n": 0}, {"n": 2}, {"n": True}, {"n": 1.0},
    {"n": "1"}, {"stream": True}, {"stream": 0},
])
def test_single_image_nonstreaming_mode_is_strict(parameters: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ImageGenerationOptions.model_validate({"prompt": "A cup", **parameters})


def test_provider_payload_does_not_leak_identity_or_allow_model_retargeting() -> None:
    profile = create_image_profile(_limits(), "unit-image")
    request = _request(
        prompt="sensitive input", quality=None, vendor_option="sensitive option",
        model="untrusted-model", runtime_id="10000000-0000-4000-8000-000000000002",
    )
    assert request.provider_payload("authorized-model", profile) == {
        "prompt": "sensitive input", "quality": None,
        "vendor_option": "sensitive option", "model": "authorized-model",
    }
    assert "sensitive input" not in repr(request)
    assert "sensitive option" not in repr(request)


def test_images_reject_non_apim_attribution() -> None:
    metadata = _request().metadata.model_copy(update={"usage_domain": "github_copilot"})
    with pytest.raises(ValueError, match="APIM usage domain"):
        _request(metadata=metadata)


def test_reservation_uses_actual_utf8_payload_and_enforces_body_limit() -> None:
    request = _request(prompt="\u676f\u5b50", vendor_option=["new", 3])
    profile = create_image_profile(_limits(), "unit-image")
    body = json.dumps(
        request.provider_payload("unit-image", profile), ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert request.reservation_tokens("unit-image", profile) == len(body) + 2048
    small = create_image_profile(_limits(max_request_bytes=len(body) - 1), "unit-image")
    with pytest.raises(ValueError, match="body limit"):
        request.reservation_tokens("unit-image", small)


def test_invocation_requires_an_explicitly_published_profile() -> None:
    with pytest.raises(ValueError, match="explicitly published"):
        _request().validate_profile(None)