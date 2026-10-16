# ruff: noqa: E501

from __future__ import annotations

from ..domain.image_profiles import ImageGenerationProfile, validate_image_profile

IMAGE_OPERATION_ID = "images-generations"


def image_request_validation(profile: ImageGenerationProfile) -> str:
    profile = validate_image_profile(profile)
    return f"""    <choose><when condition="@(context.Request.Body.As&lt;byte[]&gt;(preserveContent: true).Length &gt; {profile.max_request_bytes})">
      <return-response>
        <set-status code="413" reason="Payload Too Large" />
      </return-response>
    </when></choose>
    <set-variable name="imageOutputBound" value="@((long){profile.output_reservation_tokens})" />"""