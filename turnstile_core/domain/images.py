from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from .image_profiles import (
    IMAGE_RESPONSE_SAFETY_BYTES,
    ImageGenerationProfile,
    validate_image_profile,
)
from .models import StrictModel
from .runtime_models import InvocationMetadata, InvocationUsage

ImageFormat = Literal["png", "jpeg", "webp"]
IMAGE_MAX_RESPONSE_BYTES = IMAGE_RESPONSE_SAFETY_BYTES


class ResolvedImageOptions(StrictModel):
    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    prompt: JsonValue = Field(repr=False)
    size: JsonValue = None
    quality: JsonValue = None
    output_format: JsonValue = None
    n: Literal[1] = 1
    stream: Literal[False] = False

    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return (
            (name, value) for name, value in super().__repr_args__()
            if name in type(self).model_fields
        )


class ImageGenerationOptions(ResolvedImageOptions):
    @field_validator("n", mode="before")
    @classmethod
    def require_integer_count(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("The image count must be an integer")
        return value

    @field_validator("stream", mode="before")
    @classmethod
    def require_boolean_stream(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("The stream option must be a boolean")
        return value

    def resolve(self, value: object) -> ResolvedImageOptions:
        validate_image_profile(value)
        return ResolvedImageOptions.model_validate(
            self.model_dump(exclude={
                "metadata", "model_id", "runtime_id", "gateway_profile_id", "gateway_deployment_id",
            }, exclude_unset=True)
        )


class ImageInvocationRequest(ImageGenerationOptions):
    metadata: InvocationMetadata
    model_id: UUID
    runtime_id: UUID | None = None
    gateway_profile_id: UUID | None = None
    gateway_deployment_id: UUID | None = None

    @model_validator(mode="after")
    def validate_gateway_selection(self) -> ImageInvocationRequest:
        if (self.gateway_profile_id is None) != (self.gateway_deployment_id is None):
            raise ValueError("APIM instance and Gateway must be selected together")
        return self

    @field_validator("metadata")
    @classmethod
    def require_apim_domain(cls, value: InvocationMetadata) -> InvocationMetadata:
        if value.usage_domain != "apim":
            raise ValueError("Image generation belongs to the APIM usage domain")
        return value

    def provider_payload(self, model: str, profile: ImageGenerationProfile) -> dict[str, object]:
        return {**self.resolve(profile).model_dump(exclude_unset=True), "model": model}

    def validate_profile(self, value: object) -> ImageGenerationProfile:
        profile = validate_image_profile(value)
        self.resolve(profile)
        return profile

    def reservation_tokens(self, model: str, profile: ImageGenerationProfile) -> int:
        self.validate_profile(profile)
        serialized = json.dumps(
            self.provider_payload(model, profile), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(serialized) > profile.max_request_bytes:
            raise ValueError("The image request exceeds its published body limit")
        return len(serialized) + profile.output_reservation_tokens


class GeneratedImage(StrictModel):
    b64_json: str = Field(min_length=1, max_length=IMAGE_MAX_RESPONSE_BYTES, repr=False)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]


class ImageInvocationResponse(StrictModel):
    request_id: str
    correlation_id: str
    provider: str
    runtime: str
    model: str
    gateway: str
    latency_ms: int = Field(ge=0)
    data: list[GeneratedImage] = Field(min_length=1, max_length=1, repr=False)
    size: str
    quality: str | None
    output_format: ImageFormat
    usage: InvocationUsage | None
    estimated_cost: float | None = Field(default=None, ge=0)