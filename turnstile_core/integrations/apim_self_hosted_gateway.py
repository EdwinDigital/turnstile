from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import HttpUrl

from ..config import Settings
from ..domain.gateway_targets import (
    SelfHostedGatewayBootstrap,
    SelfHostedGatewayProvisionSpec,
)
from .apim_control_plane_contract import PolicyCompilationError


class AzureSelfHostedGatewayManagementMixin:
    _settings: Settings
    _resource_id_base: str

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None, allow_not_found: bool = False,
        api_version: str | None = None,
    ) -> httpx.Response:
        raise NotImplementedError

    def _provisioned_gateway_path(self, spec: SelfHostedGatewayProvisionSpec) -> str:
        if (self._resource_id_base.casefold() != spec.target.arm_resource_id.casefold()
                or self._settings.apim_api_id != spec.target.api_id):
            raise PolicyCompilationError("Gateway creation target changed")
        return f"/gateways/{quote(spec.native_gateway_id, safe='')}"

    @staticmethod
    def _gateway_ownership_marker(spec: SelfHostedGatewayProvisionSpec) -> str:
        return f"Turnstile gateway {spec.target.gateway_profile_id}/{spec.request_id}"

    def _verify_provisioned_gateway(
        self, spec: SelfHostedGatewayProvisionSpec, value: dict[str, Any],
    ) -> None:
        expected_id = self._resource_id_base + self._provisioned_gateway_path(spec)
        if (str(value.get("id", "")).casefold() != expected_id.casefold()
                or value.get("properties", {}).get("description")
                != self._gateway_ownership_marker(spec)):
            raise PolicyCompilationError("Existing Gateway is not owned by this creation request")

    def _require_gateway_creation_permissions(self) -> None:
        response = self._request(
            "GET", "/providers/Microsoft.Authorization/permissions", api_version="2022-04-01",
        ).json()
        permissions = response.get("value", [])
        if not isinstance(permissions, list) or response.get("nextLink"):
            raise PolicyCompilationError("Gateway creation requires a complete permission readback")
        for permission in permissions:
            if not isinstance(permission, dict):
                raise PolicyCompilationError("Gateway permission readback is invalid")
            for field in ("actions", "notActions"):
                patterns = permission.get(field, [])
                valid_patterns = isinstance(patterns, list) and all(
                    isinstance(item, str) for item in patterns
                )
                if not valid_patterns:
                    raise PolicyCompilationError("Gateway permission readback is invalid")
        actions = {
            "Microsoft.ApiManagement/service/read",
            "Microsoft.ApiManagement/service/gateways/read",
            "Microsoft.ApiManagement/service/gateways/write",
            "Microsoft.ApiManagement/service/gateways/apis/read",
            "Microsoft.ApiManagement/service/gateways/apis/write",
            "Microsoft.ApiManagement/service/apis/read",
        }
        for action in actions:
            granted = any(
                any(fnmatchcase(action.casefold(), pattern.casefold())
                    for pattern in permission.get("actions", []))
                and not any(fnmatchcase(action.casefold(), pattern.casefold())
                            for pattern in permission.get("notActions", []))
                for permission in permissions
            )
            if not granted:
                raise PolicyCompilationError(f"Gateway creation permission is missing: {action}")

    def ensure_self_hosted_gateway(
        self, spec: SelfHostedGatewayProvisionSpec, before_write: Callable[[], None],
    ) -> None:
        path = self._provisioned_gateway_path(spec)
        self._require_gateway_creation_permissions()
        service = self._request("GET", "").json()
        if (str(service.get("id", "")).casefold() != self._resource_id_base.casefold()
                or service.get("sku", {}).get("name") not in {"Developer", "Premium"}):
            raise PolicyCompilationError("The APIM target does not support Self-hosted Gateway")
        api = self._request("GET", f"/apis/{quote(spec.target.api_id, safe='')}").json()
        if str(api.get("id", "")).casefold() != (
            self._resource_id_base + "/apis/" + spec.target.api_id
        ).casefold():
            raise PolicyCompilationError("Gateway creation API identity does not match")
        existing = self._request("GET", path, allow_not_found=True)
        if existing.status_code == 404:
            before_write()
            self._request("PUT", path, extra_headers={"If-None-Match": "*"}, json_body={
                "properties": {
                    "description": self._gateway_ownership_marker(spec),
                    "locationData": {"name": spec.configuration.location
                                     or spec.configuration.display_name},
                },
            })
            existing = self._request("GET", path)
        self._verify_provisioned_gateway(spec, existing.json())
        association = path + "/apis/" + quote(spec.target.api_id, safe="")
        associated = self._request("HEAD", association, allow_not_found=True)
        if associated.status_code == 404:
            before_write()
            self._request("PUT", association, json_body={
                "properties": {"provisioningState": "created"},
            })
            associated = self._request("HEAD", association)
        if associated.status_code not in {200, 204}:
            raise PolicyCompilationError("Gateway API association readback does not match")

    def self_hosted_gateway_bootstrap(
        self, spec: SelfHostedGatewayProvisionSpec, expires_in_hours: int, image: str,
    ) -> SelfHostedGatewayBootstrap:
        if not 1 <= expires_in_hours <= 720:
            raise ValueError("Gateway token validity must be between 1 and 720 hours")
        if not re.fullmatch(
            r"mcr\.microsoft\.com/azure-api-management/gateway"
            r"(?::[0-9]+\.[0-9]+\.[0-9]+|@sha256:[a-f0-9]{64})", image,
        ):
            raise ValueError("Gateway image must be an explicitly versioned official image")
        path = self._provisioned_gateway_path(spec)
        self._verify_provisioned_gateway(spec, self._request("GET", path).json())
        association_path = path + "/apis/" + quote(spec.target.api_id, safe="")
        association = self._request("HEAD", association_path)
        if association.status_code not in {200, 204}:
            raise PolicyCompilationError("Gateway API association readback does not match")
        expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(
            hours=expires_in_hours, seconds=-60,
        )
        value = self._request("POST", path + "/generateToken", json_body={
            "keyType": "primary", "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }).json().get("value")
        if (not isinstance(value, str) or not value.startswith(spec.native_gateway_id + "&")
                or len(value) > 4096 or any(character in value for character in "\r\n\x00")):
            raise PolicyCompilationError("Gateway token response is invalid")
        endpoint = f"https://{spec.target.service_name}.configuration.azure-api.net"
        filename = spec.native_gateway_id + ".env"
        command = shlex.join([
            "docker", "run", "--detach", "--name", spec.native_gateway_id,
            "--restart", "unless-stopped", "--publish", "8080:8080",
            "--publish", "8081:8081", "--env-file", "./" + filename, image,
        ])
        return SelfHostedGatewayBootstrap(
            deployment_id=spec.deployment_id, native_gateway_id=spec.native_gateway_id,
            configuration_endpoint=HttpUrl(endpoint), expires_at=expiry, image=image,
            environment_file_name=filename,
            environment_file=(
                f"config.service.endpoint={endpoint}\nconfig.service.auth=GatewayKey {value}\n"
                "telemetry.logs.std=json\n"
            ),
            docker_command=shlex.join(["chmod", "600", "./" + filename]) + " &&\n" + command,
            renewal_command=" &&\n".join([
                shlex.join(["chmod", "600", "./" + filename]),
                shlex.join(["docker", "stop", spec.native_gateway_id]),
                shlex.join(["docker", "rm", spec.native_gateway_id]), command,
            ]),
        )