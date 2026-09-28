from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID
from xml.etree import ElementTree

import httpx
from pydantic import ConfigDict, Field, HttpUrl, model_validator

from turnstile_core.domain.model_availability import ModelAvailabilityPolicyAdoption
from turnstile_core.domain.models import StrictModel
from turnstile_core.integrations.apim_model_availability import (
    AVAILABILITY_PROBE_PREFIX,
    INHERITED_OPERATION_POLICY,
    compile_availability_adoption,
    compile_protocol_fenced_probe,
    compose_availability_parent,
)
from turnstile_core.integrations.apim_policy_components import component_digest, parse_policy
from turnstile_core.integrations.model_availability_verifier import (
    _no_bypass,
    _revision_name,
    inspect_availability_operation,
)


class AvailabilityInstallation(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subscription_id: UUID
    resource_group: str = Field(pattern=r"^[A-Za-z0-9_.()-]{1,90}$")
    service_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,49}$")
    target_id: UUID
    api_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
    table_endpoint: HttpUrl
    table_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")
    revisions: tuple[ModelAvailabilityPolicyAdoption, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_scope(self) -> AvailabilityInstallation:
        endpoint = self.table_endpoint
        if (
            endpoint.scheme != "https"
            or not endpoint.host
            or not endpoint.host.endswith(".table.core.windows.net")
            or endpoint.host.endswith("-secondary.table.core.windows.net")
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {None, "", "/"}
            or endpoint.port != 443
        ):
            raise ValueError("Installation requires the exact primary Table HTTPS origin")
        if len({item.revision for item in self.revisions}) != len(self.revisions) or any(
            item.target_id != self.target_id or item.api_id != self.api_id
            for item in self.revisions
        ):
            raise ValueError("Installation revision scope is inconsistent")
        return self

    @property
    def service_path(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.service_name}"
        )


@dataclass(frozen=True)
class InstallationChange:
    path: str
    etag: str | None
    original_sha256: str | None
    body: dict[str, Any] = field(repr=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "etag": self.etag,
            "original_sha256": self.original_sha256,
            "candidate_sha256": self.digest,
        }


def normalize_legacy_parent(source: str) -> str:
    root = parse_policy(source)
    changed = False
    for parent in root.iter():
        for choice in list(parent):
            if choice.tag != "choose":
                continue
            children = [node for node in choice if isinstance(node.tag, str)]
            if all(node.tag in {"when", "otherwise"} for node in children):
                continue
            if (
                parent.tag != "when"
                or len(children) < 3
                or [node.get("name") for node in children[:2]]
                != ["budgetRequestCannotFit", "budgetDenialReason"]
                or any(node.tag != "set-variable" or len(node) for node in children[:2])
                or any(node.tag not in {"when", "otherwise"} for node in children[2:])
            ):
                raise ValueError("Installation refuses an unknown legacy choice structure")
            position = list(parent).index(choice)
            for offset, variable in enumerate(children[:2]):
                choice.remove(variable)
                parent.insert(position + offset, variable)
            changed = True
    return ElementTree.tostring(root, encoding="unicode") if changed else source


class AvailabilityInstaller:
    def __init__(self, scope: AvailabilityInstallation, client: httpx.Client) -> None:
        self.scope, self.client = scope, client
        self._api = "/apis/" + scope.api_id

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not path.startswith("/") or any(value in path for value in ("..", "?", "#")):
            raise ValueError("Installation path is invalid")
        if method != "GET" and not any(
            path.startswith(self._api + ";rev=" + item.revision + "/")
            for item in self.scope.revisions
        ):
            raise ValueError("Installation write is outside the approved API revisions")
        response = self.client.request(
            method,
            "https://management.azure.com" + self.scope.service_path + path,
            params={"api-version": "2024-05-01", **kwargs.pop("params", {})},
            follow_redirects=False,
            timeout=20,
            **kwargs,
        )
        if response.status_code not in {200, 201, 202, 204, 404}:
            raise RuntimeError(f"Installation {method} failed with HTTP {response.status_code}")
        return response

    def _list(self, path: str) -> list[dict[str, Any]]:
        response = self._request("GET", path)
        if response.status_code != 200:
            raise ValueError("Installation inventory is unavailable")
        body = response.json()
        rows = body.get("value")
        if body.get("nextLink") or not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Installation inventory is incomplete or too large")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("Installation inventory contains an invalid entry")
        return rows

    def _policy(self, path: str) -> tuple[str, str] | None:
        for representation in ("rawxml", "xml"):
            response = self._request("GET", path, params={"format": representation})
            if response.status_code == 404:
                return None
            text = response.text.lstrip("\ufeff \r\n\t")
            if not text.startswith("<"):
                text = response.json()["properties"]["value"]
            etag = response.headers.get("ETag", "")
            if not etag or etag == "*" or "\r" in etag or "\n" in etag:
                raise ValueError("Installation needs a concrete policy ETag")
            try:
                parse_policy(text)
            except ElementTree.ParseError:
                if representation == "rawxml":
                    continue
                raise
            return text, etag
        raise ValueError("Installation policy has no valid XML representation")

    @staticmethod
    def _policy_body(source: str) -> dict[str, Any]:
        return {
            "properties": {
                "format": "xml",
                "value": ElementTree.tostring(parse_policy(source), encoding="unicode"),
            }
        }

    def plan(self) -> list[InstallationChange]:
        actual = [_revision_name(item) for item in self._list(self._api + "/revisions")]
        if len(set(actual)) != len(actual) or set(actual) != {
            item.revision for item in self.scope.revisions
        }:
            raise ValueError("Installation must cover every retained API revision exactly")
        inherited = self._policy("/policies/policy")
        if inherited is not None:
            _no_bypass(parse_policy(inherited[0]), self.scope.api_id)
        for product in self._list(self._api + "/products"):
            name = product.get("name")
            if not isinstance(name, str) or not name or "/" in name:
                raise ValueError("Installation product scope is invalid")
            inherited = self._policy("/products/" + quote(name, safe="") + "/policies/policy")
            if inherited is not None:
                _no_bypass(parse_policy(inherited[0]), self.scope.api_id)
        changes = []
        for adoption in sorted(self.scope.revisions, key=lambda item: item.revision):
            revision_path = self._api + ";rev=" + adoption.revision
            parent_path = revision_path + "/policies/policy"
            observed = self._policy(parent_path)
            if observed is None:
                raise ValueError("Installation parent policy is missing")
            original, etag = observed
            adoption.validate_source("parent", original)
            parent = compose_availability_parent(normalize_legacy_parent(original))
            for placeholder, value in (
                ("__GATEWAY_PROFILE_ID__", str(self.scope.target_id)),
                ("__LEDGER_TABLE_ENDPOINT__", str(self.scope.table_endpoint).rstrip("/")),
                ("__LEDGER_TABLE_NAME__", self.scope.table_name),
            ):
                parent = parent.replace(placeholder, value)
            changes.append(
                InstallationChange(
                    parent_path,
                    etag,
                    hashlib.sha256(original.encode()).hexdigest(),
                    self._policy_body(parent),
                )
            )
            operations = self._list(revision_path + "/operations")
            operation_ids = [item.get("name") for item in operations]
            if len(operation_ids) != len(set(operation_ids)) or set(operation_ids) != set(
                adoption.original_policy_digests
            ) - {"parent"}:
                raise ValueError("Installation operation inventory changed")
            diagnostics = []
            for operation in sorted(operations, key=lambda item: item["name"]):
                operation_id, properties = operation["name"], operation["properties"]
                method, route = properties.get("method"), properties.get("urlTemplate")
                catalog = method == "GET" and route in {"/models", "/v1/models"}
                expected = {
                    "chat-completions": "/chat/completions",
                    "responses": "/responses",
                    "responses-compact": "/responses/compact",
                    "anthropic-messages": "/v1/messages",
                    "anthropic-count-tokens": "/v1/messages/count_tokens",
                    "images-generations": "/images/generations",
                }
                if properties.get("templateParameters") or (
                    not catalog and (
                        method != "POST" or (
                            expected.get(operation_id) != route
                            and operation_id not in adoption.fixed_operations
                        )
                    )
                ):
                    raise ValueError("Installation refuses an unknown operation contract")
                path = revision_path + "/operations/" + quote(operation_id, safe="")
                observed = self._policy(path + "/policies/policy")
                inherits_policy = operation_id in adoption.inherited_operations
                if (observed is None) != inherits_policy:
                    raise ValueError("Installation operation policy presence changed")
                source, operation_etag = (
                    observed if observed is not None else (INHERITED_OPERATION_POLICY, None)
                )
                candidate = compile_availability_adoption(
                    source, adoption, operation_id, catalog=catalog
                )
                inspect_availability_operation(
                    candidate,
                    adoption.target_id,
                    adoption.api_id,
                    adoption.revision,
                    catalog=catalog,
                )
                changes.append(
                    InstallationChange(
                        path + "/policies/policy",
                        operation_etag,
                        None if inherits_policy else hashlib.sha256(source.encode()).hexdigest(),
                        self._policy_body(candidate),
                    )
                )
                if catalog or method == "POST":
                    diagnostic_path = (
                        revision_path + "/operations/" + AVAILABILITY_PROBE_PREFIX + operation_id
                    )
                    if self._request("GET", diagnostic_path).status_code != 404:
                        raise ValueError("Installation diagnostic operation already exists")
                    diagnostics.extend(
                        [
                            InstallationChange(
                                diagnostic_path,
                                None,
                                None,
                                {
                                    "properties": {
                                        "displayName": "Availability " + operation_id,
                                        "method": "HEAD",
                                        "urlTemplate": route,
                                        "templateParameters": [],
                                    }
                                },
                            ),
                            InstallationChange(
                                diagnostic_path + "/policies/policy",
                                None,
                                None,
                                self._policy_body(compile_protocol_fenced_probe(candidate)),
                            ),
                        ]
                    )
            changes.extend(diagnostics)
        return changes

    @staticmethod
    def plan_digest(changes: list[InstallationChange]) -> str:
        return hashlib.sha256(
            json.dumps(
                [item.summary() for item in changes],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def apply(self, changes: list[InstallationChange], expected_digest: str, journal: Path) -> None:
        if self.plan_digest(changes) != expected_digest:
            raise ValueError("Installation plan digest changed")
        descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            for change in changes:
                if change.original_sha256 is not None:
                    current = self._policy(change.path)
                    if (
                        current is None
                        or current[1] != change.etag
                        or hashlib.sha256(current[0].encode()).hexdigest() != change.original_sha256
                    ):
                        raise ValueError("Installation baseline changed before write")
                elif self._request("GET", change.path).status_code != 404:
                    raise ValueError("Installation create target already exists")
                output.write(json.dumps({"event": "attempt", **change.summary()}) + "\n")
                output.flush()
                os.fsync(output.fileno())
                headers = {"If-Match": change.etag} if change.etag else {"If-None-Match": "*"}
                try:
                    self._request("PUT", change.path, json=change.body, headers=headers)
                except httpx.TransportError as error:
                    output.write(json.dumps({
                        "event": "response_unavailable", "path": change.path,
                        "error_type": type(error).__name__,
                    }) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                if change.path.endswith("/policies/policy"):
                    readback = self._policy(change.path)
                    if readback is None or component_digest(
                        parse_policy(readback[0])
                    ) != component_digest(parse_policy(change.body["properties"]["value"])):
                        raise ValueError("Installation policy readback differs")
                else:
                    response = self._request("GET", change.path)
                    properties = response.json().get("properties", {})
                    if any(
                        properties.get(name) != value
                        for name, value in change.body["properties"].items()
                    ):
                        raise ValueError("Installation operation readback differs")
                output.write(json.dumps({"event": "verified", "path": change.path}) + "\n")
                output.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or apply a checksum-pinned availability guard installation.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args()
    scope = AvailabilityInstallation.model_validate_json(args.manifest.read_text())
    if bool(args.expected_plan_sha256) != bool(args.journal):
        raise ValueError("Installation apply requires both expected digest and a new journal path")
    credential = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--subscription",
            str(scope.subscription_id),
            "--output",
            "json",
            "--only-show-errors",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    access_token = json.loads(credential.stdout)["accessToken"]
    with httpx.Client(headers={"Authorization": "Bearer " + access_token}) as client:
        installer = AvailabilityInstaller(scope, client)
        changes = installer.plan()
        if args.expected_plan_sha256:
            installer.apply(changes, args.expected_plan_sha256, args.journal)
        print(
            json.dumps(
                {
                    "plan_sha256": installer.plan_digest(changes),
                    "changes": [item.summary() for item in changes],
                    "applied": bool(args.expected_plan_sha256),
                }
            )
        )


if __name__ == "__main__":
    main()
