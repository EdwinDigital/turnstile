from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import httpx
from pydantic import SecretStr

from ..domain.model_availability import (
    AVAILABILITY_NODE_PARTITION,
    ModelAvailabilityCatalogCheck,
    ModelAvailabilityConfirmationPlan,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeEnrollment,
    ModelAvailabilityNodeLease,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
    ModelAvailabilityUnavailable,
)
from .apim_model_availability import (
    AVAILABILITY_PROBE_PREFIX,
    FILTER_CATALOG,
    RESOLVE_FIXED_MODEL,
    RESOLVE_MODEL,
    availability_kernel_digest,
    availability_operation_digest,
    catalog_read,
    compile_protocol_fenced_probe,
    is_pre_admission_denial,
    is_protocol_fenced_availability_operation,
    is_retired_availability_operation,
    node_admission_guard,
    validate_availability_parent,
)
from .apim_policy_components import component_digest, expression_tokens, parse_policy
from .ledger import TableStorageLedger

_MAX_SURFACES = 2000
_MAX_HTTP_READS = 2 * _MAX_SURFACES
_MAX_CONCURRENT_READS = 8
_VERIFICATION_DEADLINE_SECONDS = 90
_MANAGEMENT_DEADLINE_SECONDS = 180


def _revision_name(row: Mapping[str, Any]) -> str:
    properties = row.get("properties", row)
    if not isinstance(properties, Mapping) or ("properties" in row and "apiRevision" in row):
        raise ModelAvailabilityUnavailable("Availability revision inventory is ambiguous")
    revision = properties.get("apiRevision")
    if (
        not isinstance(revision, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", revision) is None
    ):
        raise ModelAvailabilityUnavailable("Availability revision identity is missing or invalid")
    return revision


def _verify_diagnostic_pairs(
    inference: Mapping[str, tuple[str, str]],
    diagnostics: Mapping[str, tuple[str, str]],
) -> None:
    expected = {AVAILABILITY_PROBE_PREFIX + name: surface for name, surface in inference.items()}
    if not expected or expected != diagnostics:
        raise ModelAvailabilityUnavailable("Availability diagnostic surface coverage is incomplete")


def _excludes_api(condition: str, api_id: str) -> bool:
    prefix = expression_tokens("@(JArray.Parse(")
    suffix = expression_tokens(").Values<string>().Contains(context.Api.Id.Split(';')[0]))")
    tokens = expression_tokens(condition)
    if (
        len(tokens) != len(prefix) + 1 + len(suffix)
        or tokens[: len(prefix)] != prefix
        or tokens[-len(suffix) :] != suffix
    ):
        return False
    try:
        values = json.loads(json.loads(tokens[len(prefix)]))
    except (ValueError, TypeError):
        return False
    return (
        isinstance(values, list)
        and bool(values)
        and all(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value)
            for value in values
        )
        and api_id not in values
    )


def _no_bypass(root: ET.Element, api_id: str | None = None) -> None:
    if api_id is not None:
        root = deepcopy(root)
        for choose in root.iter("choose"):
            for branch in list(choose):
                if branch.tag == "when" and _excludes_api(branch.get("condition", ""), api_id):
                    choose.remove(branch)
    if root.tag == "policies":
        if any(isinstance(node.tag, str) and node.tag.startswith("cache-") for node in root.iter()):
            raise ModelAvailabilityUnavailable("Availability inheritance or cache is unverified")
        inbound = root.find("inbound")
        if inbound is None:
            raise ModelAvailabilityUnavailable("Inherited inbound policy is missing")
        root = inbound
    prohibited = {
        "cache-lookup",
        "cache-store",
        "cache-lookup-value",
        "cache-store-value",
        "include-fragment",
        "forward-request",
        "llm-token-limit",
        "rate-limit-by-key",
        "rate-limit",
        "quota-by-key",
        "quota",
    }
    for node in root.iter():
        if node.tag in prohibited:
            raise ModelAvailabilityUnavailable("Availability inheritance or cache is unverified")
        if node.tag == "return-response" and not is_pre_admission_denial(root, node):
            raise ModelAvailabilityUnavailable("Inherited policy can bypass availability")
        if node.tag == "send-request" and node.findtext("set-method") != "GET":
            raise ModelAvailabilityUnavailable("Inherited policy performs work before admission")
        if node.tag == "set-variable" and node.get("name", "").startswith("availability"):
            raise ModelAvailabilityUnavailable("Inherited policy shadows availability identity")


def inspect_availability_operation(
    source: str,
    target_id: UUID,
    api_id: str,
    revision: str,
    *,
    catalog: bool,
) -> list[dict[str, Any]]:
    root = parse_policy(source)
    if is_retired_availability_operation(root):
        if catalog:
            raise ModelAvailabilityUnavailable("Catalog cannot be a retired fixed route")
        return []
    inbound = root.find("inbound")
    if inbound is None or len(inbound.findall("base")) != 1:
        raise ModelAvailabilityUnavailable("Operation does not inherit its availability guard")
    base = inbound.find("base")
    assert base is not None
    for name, value in (
        ("availabilityTargetId", str(target_id)),
        ("availabilityApiId", api_id),
        ("availabilityApiRevision", revision),
        ("availabilitySurfaceKind", "catalog" if catalog else "inference"),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if (
            len(nodes) != 1
            or nodes[0] not in list(inbound)
            or nodes[0].get("value") != value
            or list(inbound).index(nodes[0]) > list(inbound).index(base)
        ):
            raise ModelAvailabilityUnavailable("Operation availability identity does not match")
    for name, expected in (
        ("availabilitySurfaceDigest", availability_operation_digest(root)),
        ("availabilityCapabilityDigest", None),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if (
            len(nodes) != 1
            or nodes[0] not in list(inbound)
            or list(inbound).index(nodes[0]) > list(inbound).index(base)
            or re.fullmatch(r"[0-9a-f]{64}", nodes[0].get("value", "")) is None
            or expected is not None
            and nodes[0].get("value") != expected
        ):
            raise ModelAvailabilityUnavailable("Operation capability evidence is inconsistent")
    prefix = ET.Element("inbound")
    for node in list(inbound)[: list(inbound).index(base)]:
        if node.tag != "set-variable" or not node.get("name", "").startswith("availability"):
            prefix.append(node)
    _no_bypass(prefix)
    if any(
        node.tag
        in {
            "cache-lookup",
            "cache-store",
            "cache-lookup-value",
            "cache-store-value",
            "include-fragment",
        }
        for node in root.iter()
    ):
        raise ModelAvailabilityUnavailable("Operation availability cannot use cached state")
    if not catalog:
        fixed_models = root.findall(".//set-variable[@name='availabilityFixedModelAlias']")
        if fixed_models and (
            len(fixed_models) != 1 or fixed_models[0] not in list(inbound)
            or not fixed_models[0].get("value", "").strip()
            or list(inbound).index(fixed_models[0]) > list(inbound).index(base)
        ):
            raise ModelAvailabilityUnavailable("Fixed operation model identity is invalid")
        resolver_expression = RESOLVE_FIXED_MODEL if fixed_models else RESOLVE_MODEL
        resolver = root.findall(".//set-variable[@name='availabilityModelId']")
        if (
            len(resolver) != 1
            or resolver[0] not in list(inbound)
            or expression_tokens(resolver[0].get("value", ""))
            != expression_tokens(resolver_expression)
            or list(inbound).index(resolver[0]) > list(inbound).index(base)
        ):
            raise ModelAvailabilityUnavailable("Operation model identity resolver is unverified")
        routes = root.findall(".//set-variable[@name='availabilityRoutes']")
        if len(routes) != 1 or routes[0] not in list(inbound):
            raise ModelAvailabilityUnavailable("Operation model identity map is missing")
        values = json.loads(routes[0].attrib["value"])
        if not isinstance(values, list) or len(values) > 500:
            raise ModelAvailabilityUnavailable("Operation model identity map exceeds its contract")
        if fixed_models and (
            len(values) != 1 or not isinstance(values[0], dict)
            or values[0].get("alias") != fixed_models[0].get("value")
        ):
            raise ModelAvailabilityUnavailable("Fixed operation mapping is ambiguous")
        aliases: set[str] = set()
        for route in values:
            if not isinstance(route, dict) or set(route) != {"alias", "model_id", "gateways"}:
                raise ModelAvailabilityUnavailable("Operation model identity map is invalid")
            if not isinstance(route["alias"], str) or route["alias"].casefold() in aliases:
                raise ModelAvailabilityUnavailable("Operation aliases are ambiguous")
            aliases.add(route["alias"].casefold())
            if str(UUID(route["model_id"])) != route["model_id"]:
                raise ModelAvailabilityUnavailable("Operation model identity is invalid")
            gateways = route["gateways"]
            if gateways is not None and (
                not isinstance(gateways, list)
                or not gateways
                or any(not isinstance(gateway, str) or not gateway for gateway in gateways)
            ):
                raise ModelAvailabilityUnavailable("Operation serving scope is invalid")
        return values
    node_guard = next(
        (
            node
            for node in inbound
            if node.find(
                "when/set-variable[@name='availabilityNodeKey']",
            )
            is not None
        ),
        None,
    )
    if node_guard is None or component_digest(node_guard) != component_digest(
        node_admission_guard()
    ):
        raise ModelAvailabilityUnavailable("Catalog node admission is unverified")
    results = root.findall(".//set-variable[@name='availabilityCatalogResult']")
    scopes = root.findall(".//set-variable[@name='availabilityCatalogScope']")
    if not results or len(results) != len(scopes):
        raise ModelAvailabilityUnavailable("Catalog is not availability-filtered")
    expected_read = ET.Element("inbound")
    expected_read.extend(catalog_read())
    models = []
    for result, scope in zip(results, scopes, strict=True):
        if expression_tokens(result.get("value", "")) != expression_tokens(FILTER_CATALOG):
            raise ModelAvailabilityUnavailable("Catalog availability filter changed")
        parent = next(parent for parent in root.iter() if result in list(parent))
        index = list(parent).index(result)
        actual_read = ET.Element("inbound")
        actual_read.extend(list(parent)[index - 2 : index])
        if component_digest(actual_read) != component_digest(expected_read):
            raise ModelAvailabilityUnavailable("Catalog availability reads changed")
        values = json.loads(scope.attrib["value"])
        if not isinstance(values, list) or len(values) > 500:
            raise ModelAvailabilityUnavailable("Catalog scope exceeds its contract")
        models.extend(values)
    for response in root.iter("return-response"):
        status = response.find("set-status")
        if (
            status is not None
            and status.get("code") == "200"
            and (
                "availabilityCatalogResult" not in (response.findtext("set-body") or "")
                or response.findtext("set-header[@name='Cache-Control']/value")
                != "no-store, max-age=0"
            )
        ):
            raise ModelAvailabilityUnavailable("Catalog can return stale or unfiltered results")
    return models


class ModelAvailabilityNodeVerifier:
    def __init__(
        self,
        client: httpx.Client,
        key: SecretStr,
        store: TableStorageLedger | None,
        table_endpoint: str,
        table_name: str,
        before_request: Callable[[], None] | None = None,
    ) -> None:
        self._client, self._key, self._store = client, key, store
        self._before_request = before_request
        self._store_digest = hashlib.sha256(
            (table_endpoint.rstrip("/") + "|" + table_name).encode()
        ).hexdigest()

    def attest(
        self, endpoint: str, identity: Mapping[str, str], surfaces: Mapping[str, str]
    ) -> None:
        parsed = httpx.URL(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.host
            or not surfaces
            or len(surfaces) > 20
            or set(identity) != {"target", "revision", "gateway", "instance", "capability"}
        ):
            raise ModelAvailabilityUnavailable(
                "Node probe endpoint or surface inventory is invalid"
            )
        for route, digest in surfaces.items():
            if not route.startswith("/") or any(part in route for part in ("..", "?", "#", "{")):
                raise ModelAvailabilityUnavailable("Node probe route is invalid")
            nonce = uuid4().hex
            request = httpx.Request(
                "HEAD",
                endpoint.rstrip("/") + ";rev=" + quote(identity["revision"], safe="") + route,
                headers={
                    "Ocp-Apim-Subscription-Key": self._key.get_secret_value(),
                    "Cache-Control": "no-cache",
                    "Content-Type": "application/json",
                    "x-turnstile-availability-probe": nonce,
                },
                json={
                    "model": "urn:turnstile:availability-probe:" + nonce,
                    "_turnstile_availability_probe": nonce,
                },
                extensions={"timeout": {"connect": 2, "read": 5, "write": 2, "pool": 2}},
            )
            if self._before_request:
                self._before_request()
            response = self._client.send(request, auth=None, follow_redirects=False)
            try:
                expected = {
                    **identity,
                    "version": "1",
                    "probe": nonce,
                    "kernel": availability_kernel_digest(),
                    "surface": digest,
                    "store": self._store_digest,
                }
                if (
                    response.status_code != 503
                    or any(
                        response.headers.get("x-turnstile-availability-" + name) != value
                        for name, value in expected.items()
                    )
                    or "no-store" not in response.headers.get("Cache-Control", "")
                ):
                    raise ModelAvailabilityUnavailable("Node policy attestation does not match")
            finally:
                response.close()

    def verify(
        self, endpoint: str, lease: ModelAvailabilityNodeLease, surfaces: Mapping[str, str],
        *, valid_until: datetime | None = None,
    ) -> ModelAvailabilityNodeLease:
        if self._store is None:
            raise ModelAvailabilityUnavailable("Node lease store is unavailable")
        self.attest(
            endpoint,
            {
                "target": str(lease.target_id),
                "revision": lease.revision,
                "gateway": lease.native_gateway_id,
                "instance": lease.instance_id,
                "capability": lease.capability_digest,
            },
            surfaces,
        )
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = now + timedelta(seconds=120)
        if valid_until is not None:
            expires_at = min(expires_at, valid_until.replace(microsecond=0))
            if expires_at <= datetime.now(UTC):
                raise ModelAvailabilityUnavailable("Node management evidence expired")
        verified = lease.model_copy(
            update={"issued_at": now, "expires_at": expires_at}
        )
        current = self._store.read_versioned_entity(verified.partition_key, verified.row_key)
        if current:
            observed = ModelAvailabilityNodeLease.from_entity(current[0])
            if observed.issued_at > lease.issued_at or (
                observed.capability_digest != lease.capability_digest
                and observed.expires_at > lease.issued_at
            ):
                raise ModelAvailabilityUnavailable("Node policy lease changed during verification")
        if not self._store.replace_if_match(
            verified.partition_key,
            verified.row_key,
            verified.entity(),
            current[1] if current else None,
        ):
            raise ModelAvailabilityUnavailable("Node policy lease update lost its version")
        readback = self._store.read_versioned_entity(verified.partition_key, verified.row_key)
        if readback is None or ModelAvailabilityNodeLease.from_entity(readback[0]) != verified:
            raise ModelAvailabilityUnavailable("Node policy lease readback changed")
        return verified


class ModelAvailabilityGatewayVerifier:
    def __init__(
        self,
        *,
        target_id: UUID,
        api_id: str,
        gateway_url: str,
        table_endpoint: str,
        table_name: str,
        key: SecretStr | None,
        list_resources: Callable[[str], list[dict[str, Any]]],
        read_policy: Callable[[str], str | None],
        client: httpx.Client,
        state_reader: Callable[[UUID], ModelAvailabilityState | None],
        node_enrollment_reader: Callable[[], Sequence[ModelAvailabilityNodeEnrollment]]
        | None = None,
        node_store: TableStorageLedger | None = None,
        resource_id: str = "",
        state_batch_reader: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]]
        | None = None,
        before_proof: Callable[[], None] | None = None,
    ) -> None:
        self.target_id, self.api_id = target_id, api_id
        self.gateway_url = gateway_url.rstrip("/")
        self.table_endpoint, self.table_name = table_endpoint.rstrip("/"), table_name
        self.key = key
        self.list_resources, self.read_policy = list_resources, read_policy
        self.client, self.state_reader = client, state_reader
        self.node_enrollment_reader, self.node_store = node_enrollment_reader, node_store
        self.resource_id = resource_id
        self.state_batch_reader = state_batch_reader
        self.before_proof = before_proof
        self.confirmation_plan: ModelAvailabilityConfirmationPlan | None = None
        self._catalog_checks: list[ModelAvailabilityCatalogCheck] = []

    def _enrollments(
        self, deployments: Sequence[Mapping[str, Any]], retired_deployments: Sequence[UUID] = (),
    ) -> tuple[ModelAvailabilityNodeEnrollment, ...]:
        excluded = set(retired_deployments)
        retired = {
            UUID(str(item["id"])) for item in deployments
            if item.get("kind") == "self_hosted" and item.get("retired_at") is not None
        }
        if not excluded <= retired:
            raise ModelAvailabilityUnavailable("Retirement evidence does not match inventory")
        required = {
            UUID(str(item["id"])): item for item in deployments
            if item.get("kind") == "self_hosted" and UUID(str(item["id"])) not in excluded
        }
        if not required:
            return ()
        if self.node_enrollment_reader is None or self.node_store is None:
            raise ModelAvailabilityUnavailable("self_hosted_node_coverage_unverified")
        enrollments = tuple(
            sorted(
                (value for value in self.node_enrollment_reader()
                 if value.deployment_id not in excluded),
                key=lambda value: str(value.deployment_id),
            )
        )
        if len(enrollments) != len(required) or {item.deployment_id for item in enrollments} != set(
            required
        ):
            raise ModelAvailabilityUnavailable("self_hosted_node_coverage_unverified")
        identities: set[tuple[str, str]] = set()
        for item in enrollments:
            deployment = required[item.deployment_id]
            if (
                item.target_id != self.target_id
                or item.api_id != self.api_id
                or item.native_gateway_id != deployment.get("native_gateway_id")
                or item.enrolled_at > datetime.now(UTC)
            ):
                raise ModelAvailabilityUnavailable("Node enrollment does not match its target")
            for node in item.nodes:
                identity = (item.native_gateway_id, node.instance_id)
                if identity in identities:
                    raise ModelAvailabilityUnavailable("Node enrollment repeats a runtime identity")
                identities.add(identity)
        return enrollments

    def _retired_deployments(
        self, deployments: Sequence[Mapping[str, Any]],
    ) -> tuple[UUID, ...]:
        retired = [item for item in deployments
                   if item.get("kind") == "self_hosted" and item.get("retired_at") is not None]
        if not retired:
            return ()
        gateways = self.list_resources("/gateways")
        names = [row.get("name") for row in gateways]
        if any(not isinstance(name, str) or not name or "/" in name for name in names):
            raise ModelAvailabilityUnavailable("Native Gateway inventory is invalid")
        if len(names) != len(set(names)):
            raise ModelAvailabilityUnavailable("Native Gateway inventory repeats an identity")
        excluded = []
        for deployment in retired:
            native = str(deployment["native_gateway_id"])
            if native in names:
                associations = self.list_resources("/gateways/" + quote(native, safe="") + "/apis")
                for association in associations:
                    api_name = association.get("name")
                    if not isinstance(api_name, str) or not api_name:
                        raise ModelAvailabilityUnavailable("Native API association is invalid")
                    if api_name.split(";rev=", 1)[0].casefold() == self.api_id.casefold():
                        raise ModelAvailabilityUnavailable("Retired Gateway still serves this API")
            excluded.append(UUID(str(deployment["id"])))
        return tuple(sorted(excluded, key=str))

    def _catalog(
        self,
        endpoint: str,
        revision: str,
        route: str,
        models: Sequence[Mapping[str, Any]],
        routes_by_alias: Mapping[str, Mapping[str, Any]],
        *,
        gateway_id: str,
        instance_id: str,
        capability_digest: str,
        admission_surfaces: Mapping[str, str],
        before_request: Callable[[], None],
        retired_surfaces: Sequence[str] = (),
    ) -> None:
        model_ids = {}
        for model in models:
            binding = routes_by_alias.get(str(model["id"]).casefold())
            if binding is None or binding["model_id"] != model["logical_model_id"]:
                raise ModelAvailabilityUnavailable("Catalog and inference identity differ")
            if binding["gateways"] is None or gateway_id in binding["gateways"]:
                model_ids[model["id"]] = UUID(model["logical_model_id"])
        check = ModelAvailabilityCatalogCheck.model_validate(
            {
                "endpoint_url": endpoint,
                "revision": revision,
                "route": route,
                "gateway_id": gateway_id,
                "instance_id": instance_id,
                "models": model_ids,
                "capability_digest": capability_digest,
                "admission_surfaces": admission_surfaces,
                "retired_surfaces": retired_surfaces,
            }
        )
        self._catalog_checks.append(check)

    def _check_catalog(
        self,
        check: ModelAvailabilityCatalogCheck,
        state_reader: Callable[[UUID], ModelAvailabilityState | None],
        before_request: Callable[[], None],
    ) -> None:
        before = {}
        for model_id in set(check.models.values()):
            current = state_reader(model_id)
            if current is None:
                raise ModelAvailabilityUnavailable("Catalog includes an unknown model identity")
            before[model_id] = current
        if self.key is None:
            raise ModelAvailabilityUnavailable("Availability catalog authentication is unavailable")
        request = httpx.Request(
            "GET",
            str(check.endpoint_url).rstrip("/")
            + ";rev="
            + quote(check.revision, safe="")
            + check.route,
            headers={
                "Ocp-Apim-Subscription-Key": self.key.get_secret_value(),
                "Cache-Control": "no-cache",
            },
            extensions={"timeout": {"connect": 2, "read": 10, "write": 2, "pool": 2}},
        )
        before_request()
        response = self.client.send(request, auth=None, follow_redirects=False)
        try:
            if response.status_code != 200 or len(response.content) > 524288:
                raise ModelAvailabilityUnavailable("Fresh catalog readback did not succeed")
            aliases = sorted(
                alias for alias, model_id in check.models.items() if before[model_id].enabled
            )
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if (
                not isinstance(data, list)
                or any(
                    not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    for item in data
                )
                or sorted(item["id"] for item in data) != aliases
            ):
                raise ModelAvailabilityUnavailable("Fresh catalog availability does not match")
            canonical = "|".join(
                f"{model_id}:{state.revision}:{int(state.enabled)}"
                for model_id, state in sorted(before.items())
            )
            expected = {
                "digest": hashlib.sha256(canonical.encode()).hexdigest(),
                "target": str(self.target_id),
                "revision": check.revision,
                "gateway": check.gateway_id,
                "instance": check.instance_id,
                "version": "1",
                "kernel": availability_kernel_digest(),
                "store": hashlib.sha256(
                    (self.table_endpoint + "|" + self.table_name).encode()
                ).hexdigest(),
            }
            if (
                any(
                    response.headers.get("x-turnstile-availability-" + name) != value
                    for name, value in expected.items()
                )
                or "no-store" not in response.headers.get("Cache-Control", "")
                or response.headers.get("x-turnstile-availability-probe", "")
            ):
                raise ModelAvailabilityUnavailable("Fresh catalog revision evidence does not match")
            if any(state_reader(model_id) != state for model_id, state in before.items()):
                raise ModelAvailabilityUnavailable("Availability changed during verification")
        finally:
            response.close()

    def _check_retired_surface(
        self, check: ModelAvailabilityCatalogCheck, route: str,
        before_request: Callable[[], None],
    ) -> None:
        assert self.key is not None
        nonce = uuid4().hex
        request = httpx.Request(
            "HEAD", str(check.endpoint_url).rstrip("/") + ";rev=" + check.revision + route,
            headers={
                "Ocp-Apim-Subscription-Key": self.key.get_secret_value(),
                "Cache-Control": "no-cache", "x-turnstile-availability-probe": nonce,
            },
            extensions={"timeout": {"connect": 2, "read": 10, "write": 2, "pool": 2}},
        )
        before_request()
        response = self.client.send(request, auth=None, follow_redirects=False)
        try:
            expected = {
                "probe": nonce, "api": self.api_id, "revision": check.revision,
                "gateway": check.gateway_id, "instance": check.instance_id,
            }
            if (
                response.status_code != 403
                or response.headers.get("x-turnstile-model-retired") != "1"
                or "no-store" not in response.headers.get("Cache-Control", "")
                or any(response.headers.get("x-turnstile-retired-" + name) != value
                       for name, value in expected.items())
            ):
                raise ModelAvailabilityUnavailable("Retired route denial could not be verified")
        finally:
            response.close()

    def _retired_checks(
        self, catalogs: Sequence[ModelAvailabilityCatalogCheck],
        before_request: Callable[[], None],
    ) -> list[Callable[[], None]]:
        checks: list[Callable[[], None]] = []
        seen: set[tuple[str, str]] = set()
        for check in catalogs:
            for route in check.retired_surfaces:
                identity = (check.surface_prefix, route)
                if identity not in seen:
                    checks.append(partial(
                        self._check_retired_surface, check, route, before_request,
                    ))
                    seen.add(identity)
        return checks

    def _check_protocol_fenced_surface(
        self, check: ModelAvailabilityCatalogCheck, route: str,
        before_request: Callable[[], None],
    ) -> None:
        assert self.key is not None
        nonce = uuid4().hex
        request = httpx.Request(
            "HEAD", str(check.endpoint_url).rstrip("/") + ";rev=" + check.revision + route,
            headers={
                "Ocp-Apim-Subscription-Key": self.key.get_secret_value(),
                "Cache-Control": "no-cache", "x-turnstile-availability-probe": nonce,
            },
            extensions={"timeout": {"connect": 2, "read": 10, "write": 2, "pool": 2}},
        )
        before_request()
        response = self.client.send(request, auth=None, follow_redirects=False)
        try:
            expected = {
                "probe": nonce, "api": self.api_id, "revision": check.revision,
                "gateway": check.gateway_id, "instance": check.instance_id,
            }
            if (
                response.status_code != 503
                or response.headers.get("x-turnstile-protocol-fenced") != "1"
                or "no-store" not in response.headers.get("Cache-Control", "")
                or any(response.headers.get("x-turnstile-protocol-fence-" + name) != value
                       for name, value in expected.items())
            ):
                raise ModelAvailabilityUnavailable(
                    "Historical protocol fence could not be verified"
                )
        finally:
            response.close()

    def _protocol_fenced_checks(
        self, catalogs: Sequence[ModelAvailabilityCatalogCheck],
        before_request: Callable[[], None],
    ) -> list[Callable[[], None]]:
        checks: list[Callable[[], None]] = []
        seen: set[tuple[str, str]] = set()
        for check in catalogs:
            if not check.protocol_fenced:
                continue
            for route in check.admission_surfaces:
                identity = (check.surface_prefix, route)
                if identity not in seen:
                    checks.append(partial(
                        self._check_protocol_fenced_surface, check, route, before_request,
                    ))
                    seen.add(identity)
        return checks

    def _confirm_catalogs(
        self, plan: ModelAvailabilityConfirmationPlan,
        states: Mapping[UUID, ModelAvailabilityState], before_request: Callable[[], None],
    ) -> None:
        assert self.key is not None and self.node_store is not None
        attestor = ModelAvailabilityNodeVerifier(
            self.client, self.key, self.node_store, self.table_endpoint, self.table_name,
            before_request=before_request,
        )
        checks = self._retired_checks(plan.catalogs, before_request)
        checks.extend(self._protocol_fenced_checks(plan.catalogs, before_request))
        seen: set[str] = set()
        for check in plan.catalogs:
            if check.protocol_fenced:
                continue
            checks.append(partial(self._check_catalog, check, states.get, before_request))
            if check.surface_prefix in seen:
                continue
            seen.add(check.surface_prefix)
            identity = {
                "target": str(self.target_id), "revision": check.revision,
                "gateway": check.gateway_id, "instance": check.instance_id,
                "capability": check.capability_digest,
            }
            for route, digest in check.admission_surfaces.items():
                checks.append(partial(
                    attestor.attest, str(check.endpoint_url), identity, {route: digest},
                ))
        if len(checks) > _MAX_HTTP_READS:
            raise ModelAvailabilityUnavailable("Confirmation read budget exceeded")
        with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_READS, len(checks))) as executor:
            futures = [executor.submit(check) for check in checks]
            try:
                for future in as_completed(futures, timeout=_VERIFICATION_DEADLINE_SECONDS + 10):
                    future.result()
            finally:
                for future in futures:
                    future.cancel()

    def renew(
        self, plan: ModelAvailabilityConfirmationPlan, inventory_digest: str,
        deployments: Sequence[Mapping[str, Any]],
        read_states: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]],
    ) -> ModelAvailabilityCoverage:
        now = datetime.now(UTC)
        context_key = ModelAvailabilityConfirmationPlan.context_key(
            self.resource_id, self.api_id, self.gateway_url, self.table_endpoint, self.table_name,
        )
        if (
            self.key is None or self.node_store is None or plan.row_key != context_key
            or plan.coverage.target_id != self.target_id
            or plan.coverage.inventory_digest != inventory_digest
            or plan.management_verified_at is None
            or not 0 <= (now - plan.management_verified_at).total_seconds() < 300
            or plan.enrollments != self._enrollments(deployments, plan.retired_deployments)
        ):
            raise ModelAvailabilityUnavailable("Fresh complete management evidence is required")
        management_expires = plan.management_verified_at + timedelta(seconds=300)
        model_ids = sorted({model_id for check in plan.catalogs
                            for model_id in check.models.values()}, key=str)
        states = read_states(model_ids)
        if set(states) != set(model_ids):
            raise ModelAvailabilityUnavailable("Renewal source state is incomplete")
        deadline = monotonic() + _VERIFICATION_DEADLINE_SECONDS

        def before_request() -> None:
            if monotonic() >= deadline or datetime.now(UTC) >= management_expires:
                raise ModelAvailabilityUnavailable("Renewal evidence deadline exceeded")

        attestor = ModelAvailabilityNodeVerifier(
            self.client, self.key, self.node_store, self.table_endpoint, self.table_name,
            before_request=before_request,
        )
        unique = {check.surface_prefix: check for check in plan.catalogs
                  if check.gateway_id != "managed" and not check.protocol_fenced}

        def renew_node(check: ModelAvailabilityCatalogCheck) -> ModelAvailabilityNodeLease:
            issued = datetime.now(UTC).replace(microsecond=0)
            lease = ModelAvailabilityNodeLease(
                target_id=self.target_id, api_id=self.api_id, revision=check.revision,
                native_gateway_id=check.gateway_id, instance_id=check.instance_id,
                capability_digest=check.capability_digest, issued_at=issued,
                expires_at=min(issued + timedelta(seconds=120), management_expires),
            )
            return attestor.verify(
                str(check.endpoint_url), lease, check.admission_surfaces,
                valid_until=management_expires,
            )

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            leases = list(executor.map(renew_node, unique.values()))
        self._confirm_catalogs(plan, states, before_request)
        before_request()
        if read_states(model_ids) != states or plan.enrollments != self._enrollments(
            deployments, plan.retired_deployments,
        ):
            raise ModelAvailabilityUnavailable("Renewal inventory or source state changed")
        for lease in leases:
            current = self.node_store.read_versioned_entity(lease.partition_key, lease.row_key)
            if current is None or ModelAvailabilityNodeLease.from_entity(current[0]) != lease:
                raise ModelAvailabilityUnavailable("Renewed node lease changed")
        now = datetime.now(UTC)
        if plan.enrollments:
            known = {(lease.revision, lease.native_gateway_id, lease.instance_id): lease
                     for lease in leases}
            partition = f"{AVAILABILITY_NODE_PARTITION}|{self.target_id}|{self.api_id}"
            for entity in self.node_store.list_partition_entities(partition):
                observed = ModelAvailabilityNodeLease.from_entity(entity)
                if observed.current(now, observed.capability_digest) and known.get(
                    (observed.revision, observed.native_gateway_id, observed.instance_id),
                ) != observed:
                    raise ModelAvailabilityUnavailable("An unverified renewed node lease is active")
        before_request()
        now = datetime.now(UTC)
        expires = min([
            now + timedelta(seconds=120), management_expires,
            *(lease.expires_at for lease in leases),
        ])
        if expires <= now:
            raise ModelAvailabilityUnavailable("Renewed node evidence expired before completion")
        coverage = ModelAvailabilityCoverage.model_validate({
            **plan.coverage.model_dump(), "verified_at": now, "expires_at": expires,
        })
        self.confirmation_plan = ModelAvailabilityConfirmationPlan.model_validate({
            **plan.model_dump(), "coverage": coverage,
        })
        return coverage

    def confirm(
        self,
        plan: ModelAvailabilityConfirmationPlan,
        projected: ModelAvailabilityState,
        inventory_digest: str,
        deployments: Sequence[Mapping[str, Any]],
        read_states: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]],
    ) -> ModelAvailabilityTargetResult:
        context_key = ModelAvailabilityConfirmationPlan.context_key(
            self.resource_id,
            self.api_id,
            self.gateway_url,
            self.table_endpoint,
            self.table_name,
        )
        if (
            self.key is None
            or self.node_store is None
            or plan.row_key != context_key
            or plan.coverage.target_id != self.target_id
            or not plan.coverage.current(datetime.now(UTC), inventory_digest)
            or plan.enrollments != self._enrollments(deployments, plan.retired_deployments)
            or len(plan.catalogs) > _MAX_HTTP_READS
        ):
            raise ModelAvailabilityUnavailable("Fresh matching confirmation coverage is required")
        model_ids = sorted(
            {
                projected.model_id,
                *(model_id for check in plan.catalogs for model_id in check.models.values()),
            },
            key=str,
        )
        states = read_states(model_ids)
        if set(states) != set(model_ids) or states.get(projected.model_id) != projected:
            raise ModelAvailabilityUnavailable("Confirmation source state changed")
        deadline = monotonic() + _VERIFICATION_DEADLINE_SECONDS

        def before_request() -> None:
            if monotonic() >= deadline:
                raise ModelAvailabilityUnavailable("Confirmation request deadline exceeded")

        self._confirm_catalogs(plan, states, before_request)
        now = datetime.now(UTC)
        if (
            not plan.coverage.current(now, inventory_digest)
            or plan.enrollments != self._enrollments(deployments, plan.retired_deployments)
            or read_states(model_ids) != states
        ):
            raise ModelAvailabilityUnavailable("Confirmation evidence changed or expired")
        return ModelAvailabilityTargetResult(
            target_id=self.target_id,
            inventory_digest=inventory_digest,
            policy_digest=plan.coverage.policy_digest,
            status="verified",
            observed_revision=projected.revision,
            observed_enabled=projected.enabled,
            verified_at=now,
        )

    def verify(
        self, inventory_digest: str, deployments: Sequence[Mapping[str, Any]]
    ) -> ModelAvailabilityCoverage:
        self.confirmation_plan = None
        self._catalog_checks = []
        if self.key is None:
            raise ModelAvailabilityUnavailable("Availability catalog authentication is unavailable")
        retired_deployments = self._retired_deployments(deployments)
        enrollments = self._enrollments(deployments, retired_deployments)
        node_leases: list[ModelAvailabilityNodeLease] = []
        node_checks: list[Callable[[], ModelAvailabilityNodeLease]] = []
        managed_checks: list[Callable[[], None]] = []
        observations: list[tuple[str, str]] = []
        reads = 0
        read_lock = Lock()
        deadline = monotonic() + _MANAGEMENT_DEADLINE_SECONDS

        def before_request() -> None:
            nonlocal reads
            with read_lock:
                reads += 1
                exhausted = reads > _MAX_HTTP_READS
            if exhausted or monotonic() >= deadline:
                raise ModelAvailabilityUnavailable("Availability read budget exhausted")

        policy_cache: dict[str, str | None] = {}

        def policy(path: str, required: bool = True) -> str | None:
            if path not in policy_cache:
                before_request()
                policy_cache[path] = self.read_policy(path)
            source = policy_cache[path]
            if source is None and required:
                raise ModelAvailabilityUnavailable("Availability policy coverage is missing")
            if source is not None:
                observations.append(
                    (path, component_digest(parse_policy(source), normalize_text_defaults=False))
                )
            return source

        def inventory(path: str) -> list[dict[str, Any]]:
            before_request()
            return self.list_resources(path)

        service = policy("/policies/policy", required=False)
        if service is not None:
            _no_bypass(parse_policy(service), self.api_id)
        api_path = "/apis/" + quote(self.api_id, safe="")
        for product in inventory(api_path + "/products"):
            name = str(product.get("name") or "")
            if not name or "/" in name:
                raise ModelAvailabilityUnavailable("API product inventory is invalid")
            inherited = policy(
                "/products/" + quote(name, safe="") + "/policies/policy", required=False
            )
            if inherited is not None:
                _no_bypass(parse_policy(inherited), self.api_id)
        revisions = inventory(api_path + "/revisions")
        if not revisions or len(revisions) > 100:
            raise ModelAvailabilityUnavailable(
                "Availability revision inventory is empty or too large"
            )
        revision_names = [_revision_name(row) for row in revisions]
        if len(revision_names) != len(set(revision_names)):
            raise ModelAvailabilityUnavailable(
                "Availability revision inventory repeats an identity"
            )

        def read_parent(revision: str) -> tuple[str, str]:
            path = api_path + ";rev=" + quote(revision, safe="") + "/policies/policy"
            before_request()
            source = self.read_policy(path)
            if source is None:
                raise ModelAvailabilityUnavailable("Availability parent policy is missing")
            validate_availability_parent(parse_policy(source))
            return path, source

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            policy_cache.update(executor.map(read_parent, revision_names))

        def operation_inventory(revision: str) -> tuple[str, list[dict[str, Any]]]:
            values = inventory(api_path + ";rev=" + quote(revision, safe="") + "/operations")
            if len(values) > 40:
                raise ModelAvailabilityUnavailable("Availability operation inventory is too large")
            return revision, values

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            operations_by_revision = dict(executor.map(operation_inventory, revision_names))
        paths = []
        for revision, operations in operations_by_revision.items():
            revision_path = api_path + ";rev=" + quote(revision, safe="")
            for operation in operations:
                operation_name = operation.get("name")
                if (
                    not isinstance(operation_name, str) or not operation_name
                    or "/" in operation_name
                ):
                    raise ModelAvailabilityUnavailable("Availability operation identity is invalid")
                paths.append(
                    revision_path + "/operations/" + quote(operation_name, safe="")
                    + "/policies/policy"
                )
        if len(paths) > _MAX_HTTP_READS or len(paths) != len(set(paths)):
            raise ModelAvailabilityUnavailable("Availability policy inventory exceeds its bound")

        def read_operation_policy(path: str) -> tuple[str, str | None]:
            before_request()
            return path, self.read_policy(path)

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            policy_cache.update(executor.map(read_operation_policy, paths))
        management_verified_at = datetime.now(UTC)
        if self.before_proof is not None:
            self.before_proof()
        deadline = monotonic() + _VERIFICATION_DEADLINE_SECONDS
        surfaces = []
        seen_revisions: set[str] = set()
        for revision_row in revisions:
            revision = _revision_name(revision_row)
            if revision in seen_revisions:
                raise ModelAvailabilityUnavailable(
                    "Availability revision inventory repeats an identity"
                )
            seen_revisions.add(revision)
            revision_path = api_path + ";rev=" + quote(revision, safe="")
            parent_source = policy(revision_path + "/policies/policy")
            assert parent_source is not None
            parent = parse_policy(parent_source)
            validate_availability_parent(parent)
            protocol_fenced = is_protocol_fenced_availability_operation(parent)
            for name, expected in (
                ("availabilityTableEndpoint", self.table_endpoint),
                ("availabilityTableName", self.table_name),
            ):
                node = parent.find(f"inbound/set-variable[@name='{name}']")
                if node is None or node.get("value") != expected:
                    raise ModelAvailabilityUnavailable(
                        "Gateway availability store identity differs"
                    )
            if any(
                json.dumps(str(self.target_id)) not in guard.get("value", "")
                for guard in parent.findall(".//set-variable[@name='availabilityBindingValid']")
            ):
                raise ModelAvailabilityUnavailable("Gateway availability target identity differs")
            catalogs = []
            route_maps = []
            inference: dict[str, tuple[str, str]] = {}
            catalog_surfaces: dict[str, tuple[str, str]] = {}
            retired_operations: dict[str, tuple[str, str]] = {}
            retired_diagnostics: dict[str, tuple[str, str]] = {}
            diagnostics: dict[str, tuple[str, str]] = {}
            operation_ids: set[str] = set()
            capabilities: set[str] = set()
            node_surfaces: dict[str, str] = {}
            for operation in operations_by_revision[revision]:
                name = str(operation.get("name") or "")
                details = operation.get("properties")
                if not name or not isinstance(details, dict) or name in operation_ids:
                    raise ModelAvailabilityUnavailable(
                        "Availability operation inventory is invalid"
                    )
                operation_ids.add(name)
                route, method = (
                    str(details.get("urlTemplate") or ""),
                    str(details.get("method") or ""),
                )
                diagnostic = method == "HEAD" and name.startswith(AVAILABILITY_PROBE_PREFIX)
                catalog = route in {"/models", "/v1/models"} and (method == "GET" or diagnostic)
                source = policy(
                    revision_path + "/operations/" + quote(name, safe="") + "/policies/policy"
                )
                assert source is not None
                root = parse_policy(source)
                if is_retired_availability_operation(root):
                    if method != "POST" and not diagnostic:
                        raise ModelAvailabilityUnavailable("Retired route method is invalid")
                    retired = retired_diagnostics if diagnostic else retired_operations
                    retired[name] = (route, component_digest(root))
                    surfaces.append(f"managed/{revision}/{name}")
                    if len(surfaces) > _MAX_SURFACES:
                        raise ModelAvailabilityUnavailable("Retired surface inventory is too large")
                    continue
                capability = root.find("inbound/set-variable[@name='availabilityCapabilityDigest']")
                surface = root.find("inbound/set-variable[@name='availabilitySurfaceDigest']")
                if capability is None or surface is None:
                    raise ModelAvailabilityUnavailable("Availability policy identity is missing")
                capabilities.add(capability.get("value", ""))
                comparison_digest = component_digest(parse_policy(
                    compile_protocol_fenced_probe(source)
                )) if not diagnostic else component_digest(root)
                if catalog:
                    if diagnostic:
                        diagnostics[name] = (route, comparison_digest)
                    else:
                        values = inspect_availability_operation(
                            source, self.target_id, self.api_id, revision, catalog=True,
                        )
                        catalogs.append((route, values))
                        catalog_surfaces[name] = (route, comparison_digest)
                        node_surfaces[route] = surface.get("value", "")
                else:
                    original = name.removeprefix(AVAILABILITY_PROBE_PREFIX) if diagnostic else name
                    if (method != "POST" and not diagnostic) or (original not in {
                        "chat-completions",
                        "responses",
                        "responses-compact",
                        "anthropic-messages",
                        "anthropic-count-tokens",
                        "images-generations",
                    } and root.find(
                        "inbound/set-variable[@name='availabilityFixedModelAlias']"
                    ) is None):
                        raise ModelAvailabilityUnavailable(
                            "An operation has unverified admission semantics"
                        )
                    (diagnostics if diagnostic else inference)[name] = (
                        route,
                        comparison_digest,
                    )
                    if not diagnostic:
                        if route in node_surfaces:
                            raise ModelAvailabilityUnavailable("Inference paths are ambiguous")
                        node_surfaces[route] = surface.get("value", "")
                    if not diagnostic:
                        route_maps.extend(inspect_availability_operation(
                            source, self.target_id, self.api_id, revision, catalog=False,
                        ))
                surfaces.append(f"managed/{revision}/{name}")
                if len(surfaces) > _MAX_SURFACES:
                    raise ModelAvailabilityUnavailable(
                        "Availability surface inventory exceeds its limit"
                    )
            _verify_diagnostic_pairs({**inference, **catalog_surfaces}, diagnostics)
            if retired_operations or retired_diagnostics:
                _verify_diagnostic_pairs(retired_operations, retired_diagnostics)
            retired_routes = tuple(sorted({value[0] for value in retired_operations.values()}))
            if len(retired_routes) != len(retired_operations) or set(retired_routes).intersection(
                node_surfaces
            ):
                raise ModelAvailabilityUnavailable("Retired operation paths are ambiguous")
            if len(capabilities) != 1 or not catalogs:
                raise ModelAvailabilityUnavailable(
                    "Availability catalog or capability identity is incomplete"
                )
            capability_digest = next(iter(capabilities))
            if protocol_fenced:
                endpoints = [("managed", "managed", self.gateway_url)]
                for enrollment in enrollments:
                    observations.append((str(enrollment.enrollment_id), enrollment.digest()))
                    for enrolled_node in enrollment.nodes:
                        endpoints.append((
                            enrollment.native_gateway_id, enrolled_node.instance_id,
                            str(enrolled_node.endpoint_url),
                        ))
                        surfaces.extend(
                            f"{enrollment.native_gateway_id}/{enrolled_node.instance_id}"
                            f"/{revision}/{name}"
                            for name in operation_ids
                        )
                if len(surfaces) > _MAX_SURFACES:
                    raise ModelAvailabilityUnavailable("Fenced surface inventory exceeds its limit")
                for gateway, instance, endpoint in endpoints:
                    for route, _models in catalogs:
                        self._catalog_checks.append(ModelAvailabilityCatalogCheck.model_validate({
                            "endpoint_url": endpoint, "revision": revision, "route": route,
                            "gateway_id": gateway, "instance_id": instance, "models": {},
                            "capability_digest": capability_digest,
                            "admission_surfaces": node_surfaces, "retired_surfaces": retired_routes,
                            "protocol_fenced": True,
                        }))
                continue
            routes_by_alias: dict[str, dict[str, Any]] = {}
            for mapping in route_maps:
                alias = mapping["alias"].casefold()
                if alias in routes_by_alias and routes_by_alias[alias] != mapping:
                    raise ModelAvailabilityUnavailable(
                        "Protocols disagree about logical model identity"
                    )
                routes_by_alias[alias] = mapping
            for route, models in catalogs:
                self._catalog(
                    self.gateway_url,
                    revision,
                    route,
                    models,
                    routes_by_alias,
                    gateway_id="managed",
                    instance_id="managed",
                    capability_digest=capability_digest,
                    admission_surfaces=node_surfaces,
                    before_request=before_request,
                    retired_surfaces=retired_routes,
                )
            managed_verifier = ModelAvailabilityNodeVerifier(
                self.client, self.key, self.node_store, self.table_endpoint, self.table_name,
                before_request=before_request,
            )
            managed_identity = {
                "target": str(self.target_id), "revision": revision,
                "gateway": "managed", "instance": "managed", "capability": capability_digest,
            }
            managed_checks.extend(
                partial(
                    managed_verifier.attest, self.gateway_url, managed_identity, {route: digest},
                )
                for route, digest in node_surfaces.items()
            )
            for enrollment in enrollments:
                assert self.node_store is not None
                verifier = ModelAvailabilityNodeVerifier(
                    self.client,
                    self.key,
                    self.node_store,
                    self.table_endpoint,
                    self.table_name,
                    before_request=before_request,
                )
                observations.append((str(enrollment.enrollment_id), enrollment.digest()))
                for runtime_node in enrollment.nodes:
                    now = datetime.now(UTC).replace(microsecond=0)
                    lease = ModelAvailabilityNodeLease(
                            target_id=self.target_id,
                            api_id=self.api_id,
                            revision=revision,
                            native_gateway_id=enrollment.native_gateway_id,
                            instance_id=runtime_node.instance_id,
                            capability_digest=capability_digest,
                            issued_at=now,
                            expires_at=now + timedelta(seconds=120),
                    )
                    node_checks.append(partial(
                        verifier.verify, str(runtime_node.endpoint_url), lease, node_surfaces,
                    ))
                    for route, models in catalogs:
                        self._catalog(
                            str(runtime_node.endpoint_url),
                            revision,
                            route,
                            models,
                            routes_by_alias,
                            gateway_id=enrollment.native_gateway_id,
                            instance_id=runtime_node.instance_id,
                            capability_digest=capability_digest,
                            admission_surfaces=node_surfaces,
                            before_request=before_request,
                            retired_surfaces=retired_routes,
                        )
                    surfaces.extend(
                        f"{enrollment.native_gateway_id}/{runtime_node.instance_id}/{revision}/{name}"
                        for name in operation_ids
                    )
                    if len(surfaces) > _MAX_SURFACES:
                        raise ModelAvailabilityUnavailable(
                            "Availability node inventory exceeds its limit"
                        )
        state_ids = sorted({model_id for check in self._catalog_checks
                            for model_id in check.models.values()}, key=str)
        state_snapshot = self.state_batch_reader(state_ids) if self.state_batch_reader else None
        if state_snapshot is not None and set(state_snapshot) != set(state_ids):
            raise ModelAvailabilityUnavailable("Availability source snapshot is incomplete")
        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            node_leases = list(executor.map(lambda check: check(), node_checks))
        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            checks = [executor.submit(
                self._check_catalog, check,
                state_snapshot.get if state_snapshot is not None else self.state_reader,
                before_request,
            ) for check in self._catalog_checks if not check.protocol_fenced]
            checks.extend(executor.submit(check) for check in managed_checks)
            checks.extend(executor.submit(check) for check in self._retired_checks(
                self._catalog_checks, before_request,
            ))
            checks.extend(executor.submit(check) for check in self._protocol_fenced_checks(
                self._catalog_checks, before_request,
            ))
            for future in as_completed(checks):
                future.result()
        if self.state_batch_reader and self.state_batch_reader(state_ids) != state_snapshot:
            raise ModelAvailabilityUnavailable("Availability source changed during verification")
        if retired_deployments != self._retired_deployments(deployments):
            raise ModelAvailabilityUnavailable("Native Gateway retirement evidence changed")
        if enrollments != self._enrollments(deployments, retired_deployments):
            raise ModelAvailabilityUnavailable("Node enrollment changed during verification")
        now = datetime.now(UTC)
        expires = min([now + timedelta(seconds=120), *(lease.expires_at for lease in node_leases)])
        if expires <= now:
            raise ModelAvailabilityUnavailable("Node verification expired before completion")
        if self.node_store is not None and enrollments:
            known = {
                (lease.revision, lease.native_gateway_id, lease.instance_id): lease
                for lease in node_leases
            }
            partition = f"{AVAILABILITY_NODE_PARTITION}|{self.target_id}|{self.api_id}"
            for entity in self.node_store.list_partition_entities(partition):
                observed = ModelAvailabilityNodeLease.from_entity(entity)
                if (
                    observed.current(now, observed.capability_digest)
                    and known.get(
                        (observed.revision, observed.native_gateway_id, observed.instance_id)
                    )
                    != observed
                ):
                    raise ModelAvailabilityUnavailable("An unverified node lease remains active")
            for lease in node_leases:
                current = self.node_store.read_versioned_entity(lease.partition_key, lease.row_key)
                if current is None or ModelAvailabilityNodeLease.from_entity(current[0]) != lease:
                    raise ModelAvailabilityUnavailable(
                        "Verified node lease changed before completion"
                    )
        coverage = ModelAvailabilityCoverage(
            target_id=self.target_id,
            inventory_digest=inventory_digest,
            policy_digest=hashlib.sha256(
                json.dumps(sorted(observations), separators=(",", ":")).encode()
            ).hexdigest(),
            serving_surfaces=tuple(surfaces),
            verified_surfaces=tuple(surfaces),
            verified_at=now,
            expires_at=expires,
        )
        if self.resource_id:
            self.confirmation_plan = ModelAvailabilityConfirmationPlan.model_validate(
                {
                    "resource_id": self.resource_id,
                    "api_id": self.api_id,
                    "gateway_url": self.gateway_url,
                    "table_endpoint": self.table_endpoint,
                    "table_name": self.table_name,
                    "coverage": coverage,
                    "enrollments": enrollments,
                    "catalogs": tuple(self._catalog_checks),
                    "retired_deployments": retired_deployments,
                    "management_verified_at": management_verified_at,
                }
            )
        return coverage
