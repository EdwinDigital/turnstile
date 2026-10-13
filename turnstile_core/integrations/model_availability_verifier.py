from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event, Lock
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
    ModelAvailabilityStagedProgram,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
    ModelAvailabilityUnavailable,
)
from ..domain.storage_routes import ProgramCapabilityProof, document_digest
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
    if not expected or diagnostics != expected:
        raise ModelAvailabilityUnavailable("Availability diagnostic surface coverage is incomplete")


def _excludes_api(condition: str, api_id: str) -> bool:
    prefix = expression_tokens("@(JArray.Parse(")
    suffix = expression_tokens(
        ").Values<string>().Contains(context.Api.Id.Split(';')[0]))",
    )
    tokens = expression_tokens(condition)
    if len(tokens) != len(prefix) + 1 + len(suffix) or (
        tokens[: len(prefix)] != prefix or tokens[-len(suffix) :] != suffix
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
        for choose in list(root.iter("choose")):
            for branch in list(choose):
                if branch.tag == "when" and _excludes_api(branch.get("condition", ""), api_id):
                    choose.remove(branch)
    if root.tag == "policies":
        if any(node.tag.startswith("cache-") for node in root.iter() if isinstance(node.tag, str)):
            raise ModelAvailabilityUnavailable("Availability inheritance or cache is unverified")
        inbound = root.find("inbound")
        if inbound is None:
            raise ModelAvailabilityUnavailable("Inherited inbound policy is missing")
        root = inbound
    for node in root.iter():
        if node.tag in {
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
        }:
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
    for name, expected in (
        ("availabilityTargetId", str(target_id)),
        ("availabilityApiId", api_id),
        ("availabilityApiRevision", revision),
        ("availabilitySurfaceKind", "catalog" if catalog else "inference"),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if len(nodes) != 1 or nodes[0] not in list(inbound) or nodes[0].get("value") != expected:
            raise ModelAvailabilityUnavailable("Operation availability identity does not match")
        if list(inbound).index(nodes[0]) > list(inbound).index(base):
            raise ModelAvailabilityUnavailable("Operation availability identity is too late")
    surface = inbound.find("set-variable[@name='availabilitySurfaceDigest']")
    capability = inbound.find("set-variable[@name='availabilityCapabilityDigest']")
    if (
        surface is None
        or surface.get("value") != availability_operation_digest(root)
        or capability is None
        or re.fullmatch(r"[0-9a-f]{64}", capability.get("value", "")) is None
        or list(inbound).index(capability) > list(inbound).index(base)
        or list(inbound).index(surface) > list(inbound).index(base)
        or len(root.findall(".//set-variable[@name='availabilitySurfaceDigest']")) != 1
        or len(root.findall(".//set-variable[@name='availabilityCapabilityDigest']")) != 1
    ):
        raise ModelAvailabilityUnavailable("Operation capability evidence is inconsistent")
    before_base = ET.Element("inbound")
    for node in list(inbound)[: list(inbound).index(base)]:
        if node.tag == "set-variable" and node.get("name", "").startswith("availability"):
            continue
        before_base.append(node)
    _no_bypass(before_base)
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
        resolver = RESOLVE_FIXED_MODEL if fixed_models else RESOLVE_MODEL
        variables = root.findall(".//set-variable[@name='availabilityModelId']")
        if (
            len(variables) != 1
            or variables[0] not in list(inbound)
            or (
                expression_tokens(variables[0].get("value", "")) != expression_tokens(resolver)
                or list(inbound).index(variables[0]) > list(inbound).index(base)
            )
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
    results = root.findall(".//set-variable[@name='availabilityCatalogResult']")
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
    scopes = root.findall(".//set-variable[@name='availabilityCatalogScope']")
    if not results or len(scopes) != len(results):
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
                or (
                    response.findtext("set-header[@name='Cache-Control']/value")
                    != "no-store, max-age=0"
                )
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
            (table_endpoint.rstrip("/") + "|" + table_name).encode(),
        ).hexdigest()

    def verify(
        self,
        endpoint: str,
        lease: ModelAvailabilityNodeLease,
        surfaces: Mapping[str, str],
        *, valid_until: datetime | None = None,
    ) -> ModelAvailabilityNodeLease:
        if self._store is None:
            raise ModelAvailabilityUnavailable("Node lease store is unavailable")
        self.attest(endpoint, {
            "target": str(lease.target_id), "revision": lease.revision,
            "gateway": lease.native_gateway_id, "instance": lease.instance_id,
            "capability": lease.capability_digest,
        }, surfaces)
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
        if current is not None:
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

    def attest(
        self, endpoint: str, identity: Mapping[str, str], surfaces: Mapping[str, str],
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
            if not route.startswith("/") or any(value in route for value in ("..", "?", "#", "{")):
                raise ModelAvailabilityUnavailable("Node probe route is invalid")
            nonce = uuid4().hex
            request = httpx.Request(
                "HEAD",
                endpoint.rstrip("/") + ";rev=" + quote(identity["revision"], safe="") + route,
                headers={
                    "Ocp-Apim-Subscription-Key": self._key.get_secret_value(),
                    "Cache-Control": "no-cache",
                    "x-turnstile-availability-probe": nonce,
                    "Content-Type": "application/json",
                },
                json={
                    "model": "urn:turnstile:availability-probe:" + nonce,
                    "_turnstile_availability_probe": nonce,
                },
                extensions={"timeout": {"connect": 2, "read": 5, "write": 2, "pool": 2}},
            )
            if self._before_request is not None:
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
        staged_programs: tuple[ModelAvailabilityStagedProgram, ...] = (),
        staged_program_verifier: Callable[
            [ModelAvailabilityStagedProgram], ProgramCapabilityProof
        ] | None = None,
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
        self.staged_programs = staged_programs
        self.staged_program_verifier = staged_program_verifier
        self.confirmation_plan: ModelAvailabilityConfirmationPlan | None = None
        self._catalog_checks: list[ModelAvailabilityCatalogCheck] = []

    @staticmethod
    def _require_staged_node_compatibility(
        staged_programs: tuple[ModelAvailabilityStagedProgram, ...],
        enrollments: tuple[ModelAvailabilityNodeEnrollment, ...],
    ) -> None:
        if enrollments and any(
            not staged.installation.model_additions_enabled
            or staged.installation.legacy_program_sha256 is not None
            or staged.installation.program.scope.gateway_id != "managed"
            or staged.installation.program.instance_id != "managed"
            for staged in staged_programs
        ):
            raise ModelAvailabilityUnavailable("Staged programs require managed-only coverage")

    def _verify_staged_programs(
        self, staged_programs: tuple[ModelAvailabilityStagedProgram, ...],
        before_request: Callable[[], None],
    ) -> tuple[ProgramCapabilityProof, ...]:
        if len(staged_programs) > 4 or len({
            staged.installation.program.api_revision for staged in staged_programs
        }) != len(staged_programs):
            raise ModelAvailabilityUnavailable("Staged program inventory is not bounded and unique")
        proofs = []
        for staged in staged_programs:
            installation = staged.installation
            program = installation.program
            if (
                self.staged_program_verifier is None
                or program.scope.target_id != str(self.target_id)
                or program.scope.api_id != self.api_id or program.scope.gateway_id != "managed"
                or program.instance_id != "managed"
                or installation.gateway_endpoint != self.gateway_url
                or staged.expected_current_revision == program.api_revision
            ):
                raise ModelAvailabilityUnavailable("Staged program approval or verifier is missing")
            for _ in range(2 * len(installation.operation_contracts) + 8):
                before_request()
            proof = self.staged_program_verifier(staged)
            before_request()
            now = int(datetime.now(UTC).timestamp())
            identity_matches = proof.identity == program
            profiles_match = proof.supported_profiles == installation.supported_profiles
            proof_is_current = proof.issued_epoch <= now < proof.expires_epoch
            if not all((identity_matches, profiles_match, proof_is_current)):
                raise ModelAvailabilityUnavailable("Staged program proof changed or expired")
            proofs.append(proof)
        return tuple(proofs)

    def _enrollments(
        self,
        deployments: Sequence[Mapping[str, Any]],
        retired_deployments: Sequence[UUID] = (),
    ) -> tuple[ModelAvailabilityNodeEnrollment, ...]:
        excluded = set(retired_deployments)
        retired = {
            UUID(str(deployment["id"])) for deployment in deployments
            if deployment.get("kind") == "self_hosted" and deployment.get("retired_at") is not None
        }
        if not excluded <= retired:
            raise ModelAvailabilityUnavailable("Retirement evidence does not match inventory")
        required = {
            UUID(str(deployment["id"])): deployment
            for deployment in deployments
            if deployment.get("kind") == "self_hosted"
            and UUID(str(deployment["id"])) not in excluded
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
        now = datetime.now(UTC)
        identities: set[tuple[str, str]] = set()
        for item in enrollments:
            deployment = required[item.deployment_id]
            if (
                item.target_id != self.target_id
                or item.api_id != self.api_id
                or item.native_gateway_id != deployment.get("native_gateway_id")
                or item.enrolled_at > now
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
        retired = [deployment for deployment in deployments
                   if deployment.get("kind") == "self_hosted"
                   and deployment.get("retired_at") is not None]
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
            alias = str(model["id"]).casefold()
            binding = routes_by_alias.get(alias)
            if binding is None or binding["model_id"] != model["logical_model_id"]:
                raise ModelAvailabilityUnavailable("Catalog and inference identity differ")
            if binding["gateways"] is None or gateway_id in binding["gateways"]:
                model_ids[model["id"]] = UUID(model["logical_model_id"])
        check = ModelAvailabilityCatalogCheck.model_validate({
            "endpoint_url": endpoint, "revision": revision, "route": route,
            "gateway_id": gateway_id, "instance_id": instance_id, "models": model_ids,
            "capability_digest": capability_digest, "admission_surfaces": admission_surfaces,
            "retired_surfaces": retired_surfaces,
        })
        self._catalog_checks.append(check)

    def _check_catalog(
        self,
        check: ModelAvailabilityCatalogCheck,
        state_reader: Callable[[UUID], ModelAvailabilityState | None],
        before_request: Callable[[], None],
    ) -> None:
        endpoint, revision, route = str(check.endpoint_url), check.revision, check.route
        gateway_id, instance_id, model_ids = check.gateway_id, check.instance_id, check.models
        before = {}
        for model_id in set(model_ids.values()):
            current = state_reader(model_id)
            if current is None:
                raise ModelAvailabilityUnavailable("Catalog includes an unknown model identity")
            before[model_id] = current
        assert self.key is not None
        request = httpx.Request(
            "GET",
            endpoint.rstrip("/") + ";rev=" + quote(revision, safe="") + route,
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
            expected_aliases = sorted(
                alias for alias, model_id in model_ids.items() if before[model_id].enabled
            )
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if (
                not isinstance(data, list)
                or any(
                    not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    for item in data
                )
                or sorted(item["id"] for item in data) != expected_aliases
            ):
                raise ModelAvailabilityUnavailable("Fresh catalog availability does not match")
            canonical = "|".join(
                f"{model_id}:{current.revision}:{int(current.enabled)}"
                for model_id, current in sorted(before.items())
            )
            expected_headers = {
                "digest": hashlib.sha256(canonical.encode()).hexdigest(),
                "target": str(self.target_id),
                "revision": revision,
                "gateway": gateway_id,
                "instance": instance_id,
                "version": "1",
                "kernel": availability_kernel_digest(),
                "store": hashlib.sha256(
                    (self.table_endpoint + "|" + self.table_name).encode()
                ).hexdigest(),
            }
            if (
                any(
                    response.headers.get("x-turnstile-availability-" + name) != value
                    for name, value in expected_headers.items()
                )
                or "no-store" not in response.headers.get("Cache-Control", "")
                or response.headers.get("x-turnstile-availability-probe", "")
            ):
                raise ModelAvailabilityUnavailable("Fresh catalog revision evidence does not match")
            if any(
                state_reader(model_id) != expected for model_id, expected in before.items()
            ):
                raise ModelAvailabilityUnavailable("Availability changed during verification")
        finally:
            response.close()

    def _check_retired_surface(
        self,
        check: ModelAvailabilityCatalogCheck,
        route: str,
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
        self,
        catalogs: Sequence[ModelAvailabilityCatalogCheck],
        before_request: Callable[[], None],
    ) -> list[Callable[[], None]]:
        checks: list[Callable[[], None]] = []
        seen = set()
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
            headers={"Ocp-Apim-Subscription-Key": self.key.get_secret_value(),
                     "Cache-Control": "no-cache", "x-turnstile-availability-probe": nonce},
            extensions={"timeout": {"connect": 2, "read": 10, "write": 2, "pool": 2}},
        )
        before_request()
        response = self.client.send(request, auth=None, follow_redirects=False)
        try:
            expected = {"probe": nonce, "api": self.api_id, "revision": check.revision,
                        "gateway": check.gateway_id, "instance": check.instance_id}
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
        cancelled = Event()
        failure_lock = Lock()
        first_error: Exception | None = None

        def before_check() -> None:
            if cancelled.is_set():
                raise ModelAvailabilityUnavailable("Confirmation cancelled after failed proof")
            before_request()

        def run_check(check: Callable[[], None]) -> None:
            nonlocal first_error
            if cancelled.is_set():
                return
            try:
                check()
            except Exception as error:
                with failure_lock:
                    if first_error is None:
                        first_error = error
                    cancelled.set()
                raise

        attestor = ModelAvailabilityNodeVerifier(
            self.client, self.key, self.node_store, self.table_endpoint, self.table_name,
            before_request=before_check,
        )
        checks: list[Callable[[], None]] = self._retired_checks(plan.catalogs, before_check)
        checks.extend(self._protocol_fenced_checks(plan.catalogs, before_check))
        seen: set[str] = set()
        for check in plan.catalogs:
            if check.protocol_fenced:
                continue
            checks.append(partial(self._check_catalog, check, states.get, before_check))
            if check.surface_prefix in seen:
                continue
            seen.add(check.surface_prefix)
            identity = {
                "target": str(self.target_id), "revision": check.revision,
                "gateway": check.gateway_id, "instance": check.instance_id,
                "capability": check.capability_digest,
            }
            for route, digest in check.admission_surfaces.items():
                checks.append(partial(attestor.attest, str(check.endpoint_url), identity,
                                      {route: digest}))
        if len(checks) > _MAX_HTTP_READS:
            raise ModelAvailabilityUnavailable("Confirmation read budget exceeded")
        with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_READS, len(checks))) as executor:
            pending = [executor.submit(run_check, check) for check in checks]
            try:
                for future in as_completed(pending, timeout=_VERIFICATION_DEADLINE_SECONDS + 10):
                    future.result()
            except BaseException:
                cancelled.set()
                for future in pending:
                    future.cancel()
                if first_error is not None:
                    raise first_error from None
                raise

    def renew(
        self, plan: ModelAvailabilityConfirmationPlan, inventory_digest: str,
        deployments: Sequence[Mapping[str, Any]],
        read_states: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]],
    ) -> ModelAvailabilityCoverage:
        now = datetime.now(UTC)
        context = ModelAvailabilityConfirmationPlan.context_key(
            self.resource_id, self.api_id, self.gateway_url, self.table_endpoint, self.table_name,
        )
        if (
            self.key is None or self.node_store is None or plan.row_key != context
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

        self._require_staged_node_compatibility(plan.staged_programs, plan.enrollments)
        self._verify_staged_programs(plan.staged_programs, before_request)
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
        staged_proofs = self._verify_staged_programs(plan.staged_programs, before_request)
        now = datetime.now(UTC)
        expirations = [now + timedelta(seconds=120), management_expires]
        expirations.extend(lease.expires_at for lease in leases)
        expirations.extend(datetime.fromtimestamp(proof.expires_epoch, UTC)
                           for proof in staged_proofs)
        expires = min(expirations)
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
        expected_key = ModelAvailabilityConfirmationPlan.context_key(
            self.resource_id, self.api_id, self.gateway_url, self.table_endpoint, self.table_name,
        )
        if (
            self.key is None or plan.row_key != expected_key
            or plan.coverage.target_id != self.target_id
            or not plan.coverage.current(datetime.now(UTC), inventory_digest)
            or plan.enrollments != self._enrollments(deployments, plan.retired_deployments)
            or len(plan.catalogs) > _MAX_HTTP_READS
        ):
            raise ModelAvailabilityUnavailable("Fresh matching confirmation coverage is required")
        model_ids = sorted(
            {projected.model_id, *(model_id for check in plan.catalogs
                                   for model_id in check.models.values())},
            key=str,
        )
        states = read_states(model_ids)
        if set(states) != set(model_ids) or states.get(projected.model_id) != projected:
            raise ModelAvailabilityUnavailable("Confirmation source state changed")

        assert self.node_store is not None
        deadline = monotonic() + _VERIFICATION_DEADLINE_SECONDS

        def before_request() -> None:
            if monotonic() >= deadline:
                raise ModelAvailabilityUnavailable("Confirmation request deadline exceeded")

        self._require_staged_node_compatibility(plan.staged_programs, plan.enrollments)
        self._verify_staged_programs(plan.staged_programs, before_request)
        self._confirm_catalogs(plan, states, before_request)
        self._verify_staged_programs(plan.staged_programs, before_request)
        now = datetime.now(UTC)
        if (
            not plan.coverage.current(now, inventory_digest)
            or plan.enrollments != self._enrollments(deployments, plan.retired_deployments)
            or read_states(model_ids) != states
        ):
            raise ModelAvailabilityUnavailable("Confirmation evidence changed or expired")
        return ModelAvailabilityTargetResult(
            target_id=self.target_id, inventory_digest=inventory_digest,
            policy_digest=plan.coverage.policy_digest, status="verified",
            observed_revision=projected.revision, observed_enabled=projected.enabled,
            verified_at=now,
        )

    def verify(
        self,
        inventory_digest: str,
        deployments: Sequence[Mapping[str, Any]],
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

        def policy(path: str) -> str:
            source = policy_cache[path]
            if source is None:
                raise ModelAvailabilityUnavailable("Availability policy coverage is missing")
            observations.append(
                (path, component_digest(parse_policy(source), normalize_text_defaults=False))
            )
            return source

        service_policy = self.read_policy("/policies/policy")
        if service_policy is not None:
            _no_bypass(parse_policy(service_policy), self.api_id)
            observations.append(
                ("/policies/policy", component_digest(parse_policy(service_policy)))
            )
        api_path = "/apis/" + quote(self.api_id, safe="")
        products = self.list_resources(api_path + "/products")
        for product in products:
            name = str(product.get("name") or "")
            if not name or "/" in name:
                raise ModelAvailabilityUnavailable("API product inventory is invalid")
            inherited = self.read_policy("/products/" + quote(name, safe="") + "/policies/policy")
            if inherited is not None:
                _no_bypass(parse_policy(inherited), self.api_id)
                observations.append(("product:" + name, component_digest(parse_policy(inherited))))
        revisions = self.list_resources(api_path + "/revisions")
        if not revisions or len(revisions) > 100:
            raise ModelAvailabilityUnavailable(
                "Availability revision inventory is empty or too large"
            )
        revision_names = [_revision_name(row) for row in revisions]
        if len(revision_names) != len(set(revision_names)):
            raise ModelAvailabilityUnavailable(
                "Availability revision inventory repeats an identity"
            )
        staged_revisions = {
            staged.installation.program.api_revision for staged in self.staged_programs
        }
        if not staged_revisions <= set(revision_names):
            raise ModelAvailabilityUnavailable("Staged program managed inventory is incomplete")
        self._require_staged_node_compatibility(self.staged_programs, enrollments)
        for revision_row in revisions:
            if _revision_name(revision_row) in staged_revisions and revision_row.get(
                "properties", revision_row,
            ).get("isCurrent") is True:
                raise ModelAvailabilityUnavailable("Staged program cannot be current")
        self._verify_staged_programs(self.staged_programs, before_request)
        legacy_revisions = [name for name in revision_names if name not in staged_revisions]

        def read_parent(revision: str) -> tuple[str, str]:
            path = api_path + ";rev=" + quote(revision, safe="") + "/policies/policy"
            before_request()
            source = self.read_policy(path)
            if source is None:
                raise ModelAvailabilityUnavailable("Availability parent policy is missing")
            validate_availability_parent(parse_policy(source))
            return path, source

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            policy_cache.update(executor.map(read_parent, legacy_revisions))

        def operation_inventory(revision: str) -> tuple[str, list[dict[str, Any]]]:
            before_request()
            values = self.list_resources(
                api_path + ";rev=" + quote(revision, safe="") + "/operations"
            )
            if len(values) > 40:
                raise ModelAvailabilityUnavailable("Availability operation inventory is too large")
            return revision, values

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            operations_by_revision = dict(executor.map(operation_inventory, legacy_revisions))
        paths = []
        for revision, operations in operations_by_revision.items():
            revision_path = api_path + ";rev=" + quote(revision, safe="")
            for operation in operations:
                operation_name = operation.get("name")
                if (
                    not isinstance(operation_name, str)
                    or not operation_name
                    or "/" in operation_name
                ):
                    raise ModelAvailabilityUnavailable("Availability operation identity is invalid")
                paths.append(
                    revision_path + "/operations/" + quote(operation_name, safe="")
                    + "/policies/policy"
                )
        if len(paths) > _MAX_HTTP_READS or len(paths) != len(set(paths)):
            raise ModelAvailabilityUnavailable("Availability policy inventory exceeds its bound")

        def read_policy(path: str) -> tuple[str, str | None]:
            before_request()
            return path, self.read_policy(path)

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_READS) as executor:
            policy_cache.update(executor.map(read_policy, paths))
        management_verified_at = datetime.now(UTC)
        if self.before_proof is not None:
            self.before_proof()
        deadline = monotonic() + _VERIFICATION_DEADLINE_SECONDS
        surfaces = []
        seen_revisions: set[str] = set()
        for revision_row in revisions:
            revision = _revision_name(revision_row)
            if revision in staged_revisions:
                continue
            if revision in seen_revisions:
                raise ModelAvailabilityUnavailable(
                    "Availability revision inventory repeats an identity"
                )
            seen_revisions.add(revision)
            revision_path = api_path + ";rev=" + quote(revision, safe="")
            parent = parse_policy(policy(revision_path + "/policies/policy"))
            validate_availability_parent(parent)
            protocol_fenced = is_protocol_fenced_availability_operation(parent)
            table_endpoint = parent.find("inbound/set-variable[@name='availabilityTableEndpoint']")
            table_name = parent.find("inbound/set-variable[@name='availabilityTableName']")
            if (
                table_endpoint is None
                or table_name is None
                or table_endpoint.get("value") != self.table_endpoint
                or table_name.get("value") != self.table_name
            ):
                raise ModelAvailabilityUnavailable("Gateway availability store identity differs")
            guards = parent.findall(".//set-variable[@name='availabilityBindingValid']")
            if any(
                json.dumps(str(self.target_id)) not in guard.get("value", "") for guard in guards
            ):
                raise ModelAvailabilityUnavailable("Gateway availability target identity differs")
            operations = operations_by_revision[revision]
            catalogs = []
            route_maps = []
            inference_surfaces: dict[str, tuple[str, str]] = {}
            catalog_surfaces: dict[str, tuple[str, str]] = {}
            retired_operations: dict[str, tuple[str, str]] = {}
            retired_diagnostics: dict[str, tuple[str, str]] = {}
            diagnostic_surfaces: dict[str, tuple[str, str]] = {}
            operation_ids: set[str] = set()
            capability_digests: set[str] = set()
            node_surfaces: dict[str, str] = {}
            for operation in operations:
                operation_id = str(operation.get("name") or "")
                details = operation.get("properties")
                if (
                    not operation_id
                    or not isinstance(details, dict)
                    or operation_id in operation_ids
                ):
                    raise ModelAvailabilityUnavailable(
                        "Availability operation inventory is invalid"
                    )
                operation_ids.add(operation_id)
                route = str(details.get("urlTemplate") or "")
                method = str(details.get("method") or "")
                is_diagnostic = method == "HEAD" and operation_id.startswith(
                    AVAILABILITY_PROBE_PREFIX
                )
                is_catalog = route in {"/models", "/v1/models"} and (
                    method == "GET" or is_diagnostic
                )
                source = policy(
                    revision_path
                    + "/operations/"
                    + quote(operation_id, safe="")
                    + "/policies/policy"
                )
                operation_root = parse_policy(source)
                if is_retired_availability_operation(operation_root):
                    if method != "POST" and not is_diagnostic:
                        raise ModelAvailabilityUnavailable("Retired route method is invalid")
                    retired = retired_diagnostics if is_diagnostic else retired_operations
                    retired[operation_id] = (route, component_digest(operation_root))
                    surfaces.append(f"managed/{revision}/{operation_id}")
                    if len(surfaces) > _MAX_SURFACES:
                        raise ModelAvailabilityUnavailable("Retired surface inventory is too large")
                    continue
                capability = operation_root.find(
                    "inbound/set-variable[@name='availabilityCapabilityDigest']"
                )
                surface_digest = operation_root.find(
                    "inbound/set-variable[@name='availabilitySurfaceDigest']"
                )
                if capability is None or surface_digest is None:
                    raise ModelAvailabilityUnavailable("Availability policy identity is missing")
                capability_digests.add(capability.get("value", ""))
                comparison_digest = component_digest(parse_policy(
                    compile_protocol_fenced_probe(source)
                )) if not is_diagnostic else component_digest(operation_root)
                if is_catalog:
                    catalog_models = (
                        [] if is_diagnostic else inspect_availability_operation(
                            source, self.target_id, self.api_id, revision, catalog=True,
                        )
                    )
                    if is_diagnostic:
                        diagnostic_surfaces[operation_id] = (
                            route, comparison_digest,
                        )
                    else:
                        catalogs.append((route, catalog_models))
                        catalog_surfaces[operation_id] = (route, comparison_digest)
                        node_surfaces[route] = surface_digest.get("value", "")
                else:
                    original_id = (
                        operation_id.removeprefix(AVAILABILITY_PROBE_PREFIX)
                        if is_diagnostic
                        else operation_id
                    )
                    if (method != "POST" and not is_diagnostic) or (original_id not in {
                        "chat-completions",
                        "responses",
                        "responses-compact",
                        "anthropic-messages",
                        "anthropic-count-tokens",
                        "images-generations",
                    } and operation_root.find(
                        "inbound/set-variable[@name='availabilityFixedModelAlias']"
                    ) is None):
                        raise ModelAvailabilityUnavailable(
                            "An operation has unverified admission semantics"
                        )
                    selected_surfaces = diagnostic_surfaces if is_diagnostic else inference_surfaces
                    selected_surfaces[operation_id] = (
                        route,
                        comparison_digest,
                    )
                    if not is_diagnostic:
                        if route in node_surfaces:
                            raise ModelAvailabilityUnavailable("Inference paths are ambiguous")
                        node_surfaces[route] = surface_digest.get("value", "")
                    if not is_diagnostic:
                        route_maps.extend(inspect_availability_operation(
                            source,
                            self.target_id,
                            self.api_id,
                            revision,
                            catalog=False,
                        ))
                surfaces.append(f"managed/{revision}/{operation_id}")
                if len(surfaces) > _MAX_SURFACES:
                    raise ModelAvailabilityUnavailable(
                        "Availability surface inventory exceeds its limit"
                    )
            _verify_diagnostic_pairs(
                {**inference_surfaces, **catalog_surfaces}, diagnostic_surfaces,
            )
            if retired_operations or retired_diagnostics:
                _verify_diagnostic_pairs(retired_operations, retired_diagnostics)
            retired_routes = tuple(sorted({value[0] for value in retired_operations.values()}))
            if len(retired_routes) != len(retired_operations) or set(retired_routes).intersection(
                node_surfaces
            ):
                raise ModelAvailabilityUnavailable("Retired operation paths are ambiguous")
            if len(capability_digests) != 1:
                raise ModelAvailabilityUnavailable("Protocols disagree about capability identity")
            if not catalogs:
                raise ModelAvailabilityUnavailable("Availability catalog endpoint is missing")
            if protocol_fenced:
                endpoints = [("managed", "managed", self.gateway_url)]
                for enrollment in enrollments:
                    observations.append((str(enrollment.enrollment_id), enrollment.digest()))
                    for node in enrollment.nodes:
                        endpoints.append((enrollment.native_gateway_id, node.instance_id,
                                          str(node.endpoint_url)))
                        surfaces.extend(
                            f"{enrollment.native_gateway_id}/{node.instance_id}/{revision}/{operation}"
                            for operation in operation_ids
                        )
                if len(surfaces) > _MAX_SURFACES:
                    raise ModelAvailabilityUnavailable("Fenced surface inventory exceeds its limit")
                for gateway, instance, endpoint in endpoints:
                    for route, _ in catalogs:
                        self._catalog_checks.append(ModelAvailabilityCatalogCheck.model_validate({
                            "endpoint_url": endpoint, "revision": revision, "route": route,
                            "gateway_id": gateway, "instance_id": instance, "models": {},
                            "capability_digest": next(iter(capability_digests)),
                            "admission_surfaces": node_surfaces, "retired_surfaces": retired_routes,
                            "protocol_fenced": True,
                        }))
                continue
            routes_by_alias: dict[str, dict[str, Any]] = {}
            for route_map in route_maps:
                alias = route_map["alias"].casefold()
                if alias in routes_by_alias and routes_by_alias[alias] != route_map:
                    raise ModelAvailabilityUnavailable(
                        "Protocols disagree about logical model identity"
                    )
                routes_by_alias[alias] = route_map
            for route, models in catalogs:
                self._catalog(
                    self.gateway_url,
                    revision,
                    route,
                    models,
                    routes_by_alias,
                    gateway_id="managed",
                    instance_id="managed",
                    capability_digest=next(iter(capability_digests)),
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
                "gateway": "managed", "instance": "managed",
                "capability": next(iter(capability_digests)),
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
                for node in enrollment.nodes:
                    now = datetime.now(UTC).replace(microsecond=0)
                    lease = ModelAvailabilityNodeLease(
                            target_id=self.target_id,
                            api_id=self.api_id,
                            revision=revision,
                            native_gateway_id=enrollment.native_gateway_id,
                            instance_id=node.instance_id,
                            capability_digest=next(iter(capability_digests)),
                            issued_at=now,
                            expires_at=now + timedelta(seconds=120),
                    )
                    node_checks.append(partial(
                        verifier.verify, str(node.endpoint_url), lease, node_surfaces,
                    ))
                    for route, models in catalogs:
                        self._catalog(
                            str(node.endpoint_url),
                            revision,
                            route,
                            models,
                            routes_by_alias,
                            gateway_id=enrollment.native_gateway_id,
                            instance_id=node.instance_id,
                            capability_digest=next(iter(capability_digests)),
                            admission_surfaces=node_surfaces,
                            before_request=before_request,
                            retired_surfaces=retired_routes,
                        )
                    surfaces.extend(
                        f"{enrollment.native_gateway_id}/{node.instance_id}/{revision}/{operation_id}"
                        for operation_id in operation_ids
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
            catalog_checks = [executor.submit(
                self._check_catalog, check,
                state_snapshot.get if state_snapshot is not None else self.state_reader,
                before_request,
            ) for check in self._catalog_checks if not check.protocol_fenced]
            catalog_checks.extend(executor.submit(check) for check in managed_checks)
            catalog_checks.extend(executor.submit(check) for check in self._retired_checks(
                self._catalog_checks, before_request,
            ))
            catalog_checks.extend(executor.submit(check) for check in self._protocol_fenced_checks(
                self._catalog_checks, before_request,
            ))
            for future in as_completed(catalog_checks):
                future.result()
        if self.state_batch_reader and self.state_batch_reader(state_ids) != state_snapshot:
            raise ModelAvailabilityUnavailable("Availability source changed during verification")
        if retired_deployments != self._retired_deployments(deployments):
            raise ModelAvailabilityUnavailable("Native Gateway retirement evidence changed")
        if enrollments != self._enrollments(deployments, retired_deployments):
            raise ModelAvailabilityUnavailable("Node enrollment changed during verification")
        staged_proofs = self._verify_staged_programs(self.staged_programs, before_request)
        for staged in self.staged_programs:
            revision = staged.installation.program.api_revision
            surfaces.extend("managed/" + revision + "/" + name
                            for name in staged.installation.operation_contracts)
            staged_digest = document_digest(staged.model_dump(mode="json"))
            observations.append(("staged/" + revision, staged_digest))
        now = datetime.now(UTC)
        expires_at = min(
            [now + timedelta(seconds=120), *(lease.expires_at for lease in node_leases),
             *(datetime.fromtimestamp(proof.expires_epoch, UTC) for proof in staged_proofs)]
        )
        if expires_at <= now:
            raise ModelAvailabilityUnavailable("Node verification expired before completion")
        if self.node_store is not None and enrollments:
            known = {
                (lease.revision, lease.native_gateway_id, lease.instance_id): lease
                for lease in node_leases
            }
            partition = f"{AVAILABILITY_NODE_PARTITION}|{self.target_id}|{self.api_id}"
            observed = self.node_store.list_partition_entities(partition)
            for entity in observed:
                lease = ModelAvailabilityNodeLease.from_entity(entity)
                if (
                    lease.current(now, lease.capability_digest)
                    and known.get(
                        (lease.revision, lease.native_gateway_id, lease.instance_id),
                    )
                    != lease
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
            expires_at=expires_at,
        )
        if self.resource_id:
            self.confirmation_plan = ModelAvailabilityConfirmationPlan.model_validate({
                "resource_id": self.resource_id, "api_id": self.api_id,
                "gateway_url": self.gateway_url, "table_endpoint": self.table_endpoint,
                "table_name": self.table_name, "coverage": coverage,
                "enrollments": enrollments, "catalogs": tuple(self._catalog_checks),
                "retired_deployments": retired_deployments,
                "management_verified_at": management_verified_at,
                "staged_programs": self.staged_programs,
            })
        return coverage
