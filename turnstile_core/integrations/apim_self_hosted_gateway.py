from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import HttpUrl, SecretStr

from ..domain.control_plane import (
    GATEWAY_BOOTSTRAP_ACTIONS as GATEWAY_BOOTSTRAP_ACTIONS,
)
from ..domain.control_plane import (
    GATEWAY_PROVISION_ACTIONS as GATEWAY_PROVISION_ACTIONS,
)
from ..domain.gateway_targets import (
    SELF_HOSTED_GATEWAY_IMAGE_PATTERN,
    SelfHostedGatewayBootstrap,
    SelfHostedGatewayBootstrapRequest,
    SelfHostedGatewayProvisionSpec,
)
from .apim_control_plane_contract import (
    ApimClientSettings,
    PolicyCompilationError,
    PublisherPermissionError,
    RetryablePublicationError,
    missing_management_actions,
)


class AzureSelfHostedGatewayManagementMixin:
    _settings: ApimClientSettings
    _resource_id_base: str

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
        api_version: str | None = None,
    ) -> httpx.Response:
        raise NotImplementedError

    def _provisioned_gateway_path(self, spec: SelfHostedGatewayProvisionSpec) -> str:
        if (
            self._resource_id_base.casefold() != spec.target.arm_resource_id.casefold()
            or self._settings.apim_api_id != spec.target.api_id
        ):
            raise PolicyCompilationError("Gateway creation target changed")
        return "/gateways/" + quote(spec.native_gateway_id, safe="")

    @staticmethod
    def _gateway_ownership_marker(spec: SelfHostedGatewayProvisionSpec) -> str:
        return f"Turnstile gateway {spec.target.gateway_profile_id}/{spec.request_id}"

    def _verify_provisioned_gateway(
        self,
        spec: SelfHostedGatewayProvisionSpec,
        response: httpx.Response,
    ) -> None:
        if response.status_code in {202, 404}:
            raise RetryablePublicationError("Gateway creation readback is pending")
        if response.status_code != 200:
            raise PolicyCompilationError("Gateway creation readback was not completed")
        value = response.json()
        properties = value.get("properties") if isinstance(value, Mapping) else None
        if (
            not isinstance(properties, Mapping)
            or str(value.get("id", "")).casefold()
            != (self._resource_id_base + self._provisioned_gateway_path(spec)).casefold()
            or properties.get("description") != self._gateway_ownership_marker(spec)
        ):
            raise PolicyCompilationError("Existing Gateway is not owned by this creation request")
        state = properties.get("provisioningState")
        if state is not None and str(state).casefold() not in {"succeeded", "created"}:
            if str(state).casefold() in {"failed", "canceled"}:
                raise PolicyCompilationError("APIM Gateway provisioning failed")
            raise RetryablePublicationError("APIM Gateway provisioning is pending")

    def _require_gateway_permissions(self, required: tuple[str, ...], path: str = "") -> None:
        response = self._request(
            "GET",
            path + "/providers/Microsoft.Authorization/permissions",
            api_version="2022-04-01",
        )
        if response.status_code != 200:
            raise PublisherPermissionError()
        try:
            missing = missing_management_actions(response.json(), required)
        except ValueError:
            raise PublisherPermissionError() from None
        if missing:
            raise PublisherPermissionError(missing_actions=missing)

    @staticmethod
    def _require_api_association(response: httpx.Response) -> None:
        if response.status_code in {200, 204}:
            return
        if response.status_code in {202, 404}:
            raise RetryablePublicationError("Gateway API association readback is pending")
        raise PolicyCompilationError("Gateway API association readback does not match")

    def ensure_self_hosted_gateway(
        self,
        spec: SelfHostedGatewayProvisionSpec,
        before_write: Callable[[], None],
    ) -> None:
        path = self._provisioned_gateway_path(spec)
        self._require_gateway_permissions(GATEWAY_PROVISION_ACTIONS)
        response = self._request("GET", "")
        service = response.json()
        if not isinstance(service, Mapping) or not isinstance(service.get("sku"), Mapping):
            raise PolicyCompilationError("APIM service readback is invalid")
        if (
            response.status_code != 200
            or str(service.get("id", "")).casefold() != self._resource_id_base.casefold()
            or service.get("sku", {}).get("name", "").casefold() not in {"developer", "premium"}
        ):
            raise PolicyCompilationError("The APIM target does not support Self-hosted Gateway")
        response = self._request("GET", "/apis/" + quote(spec.target.api_id, safe=""))
        api = response.json()
        if not isinstance(api, Mapping) or not isinstance(api.get("properties"), Mapping):
            raise PolicyCompilationError("APIM API readback is invalid")
        if (
            response.status_code != 200
            or str(api.get("id", "")).casefold()
            != (self._resource_id_base + "/apis/" + spec.target.api_id).casefold()
            or str(api.get("properties", {}).get("path", "")).strip("/")
            != spec.target.api_path.strip("/")
        ):
            raise PolicyCompilationError("Gateway creation API identity does not match")
        existing = self._request("GET", path, allow_not_found=True)
        if existing.status_code == 404:
            before_write()
            self._request(
                "PUT",
                path,
                extra_headers={"If-None-Match": "*"},
                json_body={
                    "properties": {
                        "description": self._gateway_ownership_marker(spec),
                        "locationData": {
                            "name": spec.configuration.location or spec.configuration.display_name
                        },
                    },
                },
            )
            existing = self._request("GET", path, allow_not_found=True)
        self._verify_provisioned_gateway(spec, existing)
        association_path = path + "/apis/" + quote(spec.target.api_id, safe="")
        associated = self._request("HEAD", association_path, allow_not_found=True)
        if associated.status_code == 404:
            before_write()
            self._request(
                "PUT",
                association_path,
                extra_headers={"If-None-Match": "*"},
                json_body={"properties": {}},
            )
            associated = self._request("HEAD", association_path, allow_not_found=True)
        self._require_api_association(associated)

    def self_hosted_gateway_bootstrap(
        self,
        spec: SelfHostedGatewayProvisionSpec,
        request: SelfHostedGatewayBootstrapRequest,
        image: str,
    ) -> SelfHostedGatewayBootstrap:
        if not re.fullmatch(SELF_HOSTED_GATEWAY_IMAGE_PATTERN, image):
            raise ValueError("Gateway image must be an explicitly versioned official image")
        request = SelfHostedGatewayBootstrapRequest.model_validate(request.model_dump())
        path = self._provisioned_gateway_path(spec)
        self._require_gateway_permissions(GATEWAY_BOOTSTRAP_ACTIONS, path)
        self._verify_provisioned_gateway(spec, self._request("GET", path, allow_not_found=True))
        self._require_api_association(
            self._request(
                "HEAD",
                path + "/apis/" + quote(spec.target.api_id, safe=""),
                allow_not_found=True,
            )
        )
        expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(
            hours=request.expires_in_hours,
            seconds=-60,
        )
        response = self._request(
            "POST",
            path + "/generateToken",
            json_body={
                "keyType": "primary",
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        if response.status_code != 200:
            raise PolicyCompilationError("Gateway token issuance did not complete")
        try:
            payload = response.json()
        except ValueError:
            raise PolicyCompilationError("Gateway token response is invalid") from None
        value = payload.get("value") if isinstance(payload, Mapping) else None
        if (
            not isinstance(value, str)
            or not value.startswith(spec.native_gateway_id + "&")
            or len(value) > 4096
            or any(not 33 <= ord(character) < 127 for character in value)
        ):
            raise PolicyCompilationError("Gateway token response is invalid")
        endpoint = f"https://{spec.target.service_name}.configuration.azure-api.net"
        filename = spec.native_gateway_id + ".env"
        command = shlex.join(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                spec.native_gateway_id,
                "--restart",
                "unless-stopped",
                "--publish",
                "127.0.0.1:8080:8080",
                "--publish",
                "127.0.0.1:8081:8081",
                "--env-file",
                "./" + filename,
                image,
            ]
        )
        environment = (
            f"config.service.endpoint={endpoint}\n"
            f"config.service.auth=GatewayKey {value}\ntelemetry.logs.std=json\n"
        )
        if request.trusted_reverse_proxy:
            environment += "net.server.http.forwarded.proto.enabled=true\n"
        return SelfHostedGatewayBootstrap(
            deployment_id=spec.deployment_id,
            native_gateway_id=spec.native_gateway_id,
            configuration_endpoint=HttpUrl(endpoint),
            expires_at=expiry,
            image=image,
            environment_file_name=filename,
            environment_file=SecretStr(environment),
            docker_command=shlex.join(["chmod", "600", "./" + filename]) + " &&\n" + command,
            renewal_command=" &&\n".join(
                [
                    shlex.join(["chmod", "600", "./" + filename]),
                    shlex.join(["docker", "stop", spec.native_gateway_id]),
                    shlex.join(["docker", "rm", spec.native_gateway_id]),
                    command,
                ]
            ),
        )
