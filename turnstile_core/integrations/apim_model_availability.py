from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any
from xml.etree import ElementTree as ET

from ..domain.control_plane import GatewayPublication
from ..domain.model_availability import (
    ModelAvailabilityPolicyAdoption,
    ModelAvailabilityPublication,
)
from .apim_control_plane_contract import PolicyCompilationError
from .apim_policy_components import (
    _matches_component as _matches_policy_component,
)
from .apim_policy_components import (
    component_digest,
    parse_policy,
    serialize_policy,
)

AVAILABILITY_PROBE_PREFIX = "availability-probe-"

RESOLVE_MODEL = r"""@{
  try {
    var body = context.Request.Body.As<JObject>(preserveContent: true);
    if (body["model"]?.Type != JTokenType.String) { return ""; }
    var alias = ((string)body["model"]).Trim();
    var routes = JArray.Parse((string)context.Variables["availabilityRoutes"]);
    var matches = routes.OfType<JObject>().Where(route =>
      string.Equals((string)route["alias"], alias, StringComparison.OrdinalIgnoreCase)
      && (route["gateways"].Type == JTokenType.Null || ((JArray)route["gateways"]).Any(
        gateway => string.Equals((string)gateway, context.Deployment.Gateway.Id,
          StringComparison.OrdinalIgnoreCase)))).ToArray();
    if (matches.Length != 1) { return ""; }
    Guid modelId;
    var value = (string)matches[0]["model_id"];
    return Guid.TryParseExact(value, "D", out modelId) && modelId.ToString() == value ? value : "";
  } catch { return ""; }
}"""

BINDING_VALID = r"""@{
  Guid modelId;
  var value = context.Variables.GetValueOrDefault<string>("availabilityModelId", "");
  return Guid.TryParseExact(value, "D", out modelId) && modelId.ToString() == value
    && context.Variables.GetValueOrDefault<string>("availabilityTargetId", "")
      == "__GATEWAY_PROFILE_ID__"
    && context.Variables.GetValueOrDefault<string>("availabilityApiId", "")
      == context.Api.Id.Split(';')[0]
    && context.Variables.GetValueOrDefault<string>("availabilityApiRevision", "")
      == context.Api.Revision;
}"""

STATE_URL = r"""@{
  return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='"
    + Uri.EscapeDataString("model-availability|v1") + "',RowKey='"
    + Uri.EscapeDataString((string)context.Variables["availabilityModelId"]) + "')";
}"""

READ_STATE = r"""@{
  try {
    var reply = (IResponse)context.Variables["availabilityRead"];
    if (reply == null || reply.StatusCode != 200) { throw new Exception(); }
    var etag = reply.Headers.GetValueOrDefault("ETag", "");
    if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.IndexOf('\r') >= 0
      || etag.IndexOf('\n') >= 0) { throw new Exception(); }
    var text = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(text) > 4096) { throw new Exception(); }
    var entity = JObject.Parse(text);
    var modelId = (string)context.Variables["availabilityModelId"];
    if (entity["PartitionKey"]?.Type != JTokenType.String
      || (string)entity["PartitionKey"] != "model-availability|v1"
      || entity["RowKey"]?.Type != JTokenType.String || (string)entity["RowKey"] != modelId
      || entity["ModelId"]?.Type != JTokenType.String || (string)entity["ModelId"] != modelId
      || entity["SchemaVersion"]?.Type != JTokenType.Integer || (long)entity["SchemaVersion"] != 1
      || entity["Revision"]?.Type != JTokenType.Integer || (long)entity["Revision"] < 1
      || (long)entity["Revision"] > 2147483647 || entity["Enabled"]?.Type != JTokenType.Boolean
      || entity["Lifecycle"]?.Type != JTokenType.String) { throw new Exception(); }
    var lifecycle = (string)entity["Lifecycle"];
    if (lifecycle != "active" && lifecycle != "staged" && lifecycle != "deleted") {
      throw new Exception();
    }
    if (lifecycle == "deleted" && (bool)entity["Enabled"]) { throw new Exception(); }
    return new JObject(new JProperty("outcome", (bool)entity["Enabled"] ? "enabled" : "disabled"),
      new JProperty("revision", (int)entity["Revision"])).ToString(Newtonsoft.Json.Formatting.None);
  } catch { return "{\"outcome\":\"unavailable\"}"; }
}"""

PROBE_NONCE = r"""@{
  try {
    var nonce = context.Request.Headers.GetValueOrDefault("x-turnstile-availability-probe", "");
    Guid parsed;
    if (nonce.Length != 32 || !Guid.TryParseExact(nonce, "N", out parsed)
      || parsed.ToString("N") != nonce) { return ""; }
    var kind = context.Variables.GetValueOrDefault<string>("availabilitySurfaceKind", "");
    if (context.Request.Method == "GET" && kind == "catalog") { return nonce; }
    if (context.Request.Method != "HEAD" || kind != "inference"
      || !context.Operation.Id.StartsWith("availability-probe-", StringComparison.Ordinal)) {
      return "";
    }
    return nonce;
  } catch { return ""; }
}"""

NODE_ROW_KEY = r"""@{
  var identity = new JArray(context.Api.Revision, context.Deployment.Gateway.Id,
    context.Deployment.Gateway.InstanceId).ToString(Newtonsoft.Json.Formatting.None);
  using (var algorithm = System.Security.Cryptography.SHA256.Create()) {
    return BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(identity)))
      .Replace("-", "").ToLowerInvariant();
  }
}"""

NODE_URL = r"""@{
  var partition = "model-availability-nodes|v1|"
    + (string)context.Variables["availabilityTargetId"] + "|" + context.Api.Id.Split(';')[0];
  return ((string)context.Variables["availabilityTableEndpoint"]).TrimEnd('/') + "/"
    + (string)context.Variables["availabilityTableName"] + "(PartitionKey='"
    + Uri.EscapeDataString(partition) + "',RowKey='"
    + Uri.EscapeDataString((string)context.Variables["availabilityNodeKey"]) + "')";
}"""

READ_NODE = r"""@{
  try {
    var reply = (IResponse)context.Variables["availabilityNodeRead"];
    if (reply == null || reply.StatusCode != 200) { return false; }
    var etag = reply.Headers.GetValueOrDefault("ETag", "");
    if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.IndexOf('\r') >= 0
      || etag.IndexOf('\n') >= 0) { return false; }
    var text = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(text) > 4096) { return false; }
    var entity = JObject.Parse(text);
    var expected = new Dictionary<string, string> {
      { "PartitionKey", "model-availability-nodes|v1|"
        + (string)context.Variables["availabilityTargetId"] + "|" + context.Api.Id.Split(';')[0] },
      { "RowKey", (string)context.Variables["availabilityNodeKey"] },
      { "TargetId", (string)context.Variables["availabilityTargetId"] },
      { "ApiId", context.Api.Id.Split(';')[0] }, { "ApiRevision", context.Api.Revision },
      { "NativeGatewayId", context.Deployment.Gateway.Id },
      { "InstanceId", context.Deployment.Gateway.InstanceId },
      { "CapabilityDigest", (string)context.Variables["availabilityCapabilityDigest"] }
    };
    foreach (var field in expected) {
      if (entity[field.Key]?.Type != JTokenType.String
        || (string)entity[field.Key] != field.Value) { return false; }
    }
    if (entity["SchemaVersion"]?.Type != JTokenType.Integer
      || (long)entity["SchemaVersion"] != 1 || entity["IssuedEpoch"]?.Type != JTokenType.String
      || entity["ExpiresEpoch"]?.Type != JTokenType.String) { return false; }
    long issued, expires;
    var issueText = (string)entity["IssuedEpoch"];
    var expiryText = (string)entity["ExpiresEpoch"];
    if (issueText.Length > 12 || expiryText.Length > 12
      || !long.TryParse(issueText, out issued) || !long.TryParse(expiryText, out expires)
      || JsonConvert.SerializeObject(issued) != issueText
      || JsonConvert.SerializeObject(expires) != expiryText) { return false; }
    var now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc))
      .TotalSeconds;
    return issued <= now && now < expires && expires > issued && expires - issued <= 120;
  } catch { return false; }
}"""

STORE_IDENTITY = r"""@{
  var identity = ((string)context.Variables["availabilityTableEndpoint"]).TrimEnd('/')
    + "|" + (string)context.Variables["availabilityTableName"];
  using (var algorithm = System.Security.Cryptography.SHA256.Create()) {
    return BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(identity)))
      .Replace("-", "").ToLowerInvariant();
  }
}"""

CATALOG_URL = r"""@{
  var page = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
  var url = (string)context.Variables["availabilityTableEndpoint"] + "/"
    + (string)context.Variables["availabilityTableName"] + "()?$filter="
    + Uri.EscapeDataString("PartitionKey eq 'model-availability|v1'")
    + "&$top=1000&$select=PartitionKey,RowKey,SchemaVersion,ModelId,Revision,Enabled,Lifecycle";
  if (page["next_partition"]?.Type == JTokenType.String) {
    url += "&NextPartitionKey=" + Uri.EscapeDataString((string)page["next_partition"]);
  }
  if (page["next_row"]?.Type == JTokenType.String) {
    url += "&NextRowKey=" + Uri.EscapeDataString((string)page["next_row"]);
  }
  return url;
}"""

CATALOG_PAGE = r"""@{
  try {
    var previous = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
    if ((bool?)previous["ok"] != true || (int)previous["pages"] >= 4) { throw new Exception(); }
    var reply = (IResponse)context.Variables["availabilityCatalogRead"];
    if (reply == null || reply.StatusCode != 200) { throw new Exception(); }
    var text = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(text) > 524288) { throw new Exception(); }
    var values = JObject.Parse(text)["value"] as JArray;
    if (values == null || values.Count > 1000) { throw new Exception(); }
    var states = (JObject)previous["states"];
    foreach (var token in values) {
      var entity = token as JObject;
      Guid modelId;
      if (entity == null || entity["PartitionKey"]?.Type != JTokenType.String
        || (string)entity["PartitionKey"] != "model-availability|v1"
        || entity["RowKey"]?.Type != JTokenType.String
        || entity["ModelId"]?.Type != JTokenType.String
        || (string)entity["RowKey"] != (string)entity["ModelId"]
        || !Guid.TryParseExact((string)entity["ModelId"], "D", out modelId)
        || modelId.ToString() != (string)entity["ModelId"]
        || entity["SchemaVersion"]?.Type != JTokenType.Integer || (long)entity["SchemaVersion"] != 1
        || entity["Revision"]?.Type != JTokenType.Integer || (long)entity["Revision"] < 1
        || (long)entity["Revision"] > 2147483647 || entity["Enabled"]?.Type != JTokenType.Boolean
        || entity["Lifecycle"]?.Type != JTokenType.String) { throw new Exception(); }
      var lifecycle = (string)entity["Lifecycle"];
      if (lifecycle != "active" && lifecycle != "staged" && lifecycle != "deleted") {
        throw new Exception();
      }
      if (lifecycle == "deleted" && (bool)entity["Enabled"]) { throw new Exception(); }
      var key = modelId.ToString();
      if (states.Property(key) != null) { throw new Exception(); }
      states[key] = new JObject(new JProperty("revision", (int)entity["Revision"]),
        new JProperty("enabled", (bool)entity["Enabled"]));
    }
    var nextPartition = reply.Headers.GetValueOrDefault("x-ms-continuation-NextPartitionKey", "");
    var nextRow = reply.Headers.GetValueOrDefault("x-ms-continuation-NextRowKey", "");
    if (nextPartition.Length > 1024 || nextRow.Length > 1024
      || string.IsNullOrEmpty(nextPartition) && !string.IsNullOrEmpty(nextRow)) {
      throw new Exception();
    }
        return new JObject(new JProperty("ok", true),
            new JProperty("pages", (int)previous["pages"] + 1),
      new JProperty("states", states),
      new JProperty("next_partition", string.IsNullOrEmpty(nextPartition) ? null : nextPartition),
      new JProperty("next_row", string.IsNullOrEmpty(nextRow) ? null : nextRow)
    ).ToString(Newtonsoft.Json.Formatting.None);
  } catch { return "{\"ok\":false}"; }
}"""

FILTER_CATALOG = r"""@{
  try {
    var page = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
    if ((bool?)page["ok"] != true || page["next_partition"]?.Type != JTokenType.Null
      || page["next_row"]?.Type != JTokenType.Null || (int)page["pages"] < 1) {
      throw new Exception();
    }
    var states = (JObject)page["states"];
    var models = JArray.Parse((string)context.Variables["availabilityCatalogScope"]);
    if (models.Count > 500) { throw new Exception(); }
    var data = new JArray();
    var aliases = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    var revisions = new Dictionary<string, string>(StringComparer.Ordinal);
    foreach (JObject model in models) {
      var id = (string)model["logical_model_id"];
      var observed = states[id] as JObject;
      if (observed == null || !aliases.Add((string)model["id"])) { throw new Exception(); }
      var enabled = (bool)observed["enabled"];
      revisions[id] = id + ":" + JsonConvert.SerializeObject((int)observed["revision"])
        + ":" + (enabled ? "1" : "0");
      if (enabled) {
        var entry = (JObject)model.DeepClone();
        entry.Remove("logical_model_id");
        data.Add(entry);
      }
    }
    string digest;
    using (var algorithm = System.Security.Cryptography.SHA256.Create()) {
      digest = BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(
        string.Join("|", revisions.OrderBy(item => item.Key, StringComparer.Ordinal)
          .Select(item => item.Value))))).Replace("-", "").ToLowerInvariant();
    }
    var body = new JObject(new JProperty("data", data), new JProperty("has_more", false),
      new JProperty("first_id", data.Count == 0 ? JValue.CreateNull() : data[0]["id"]),
      new JProperty("last_id", data.Count == 0 ? JValue.CreateNull() : data[data.Count - 1]["id"]));
    return new JObject(new JProperty("ok", true), new JProperty("body", body),
      new JProperty("digest", digest)).ToString(Newtonsoft.Json.Formatting.None);
  } catch { return "{\"ok\":false}"; }
}"""


def _variable(parent: ET.Element, name: str, value: str) -> ET.Element:
    return ET.SubElement(parent, "set-variable", {"name": name, "value": value})


def _header(parent: ET.Element, name: str, value: str) -> None:
    node = ET.Element("set-header", {"name": name, "exists-action": "override"})
    body = parent.find("set-body") if parent.tag == "return-response" else None
    if body is None:
        parent.append(node)
    else:
        parent.insert(list(parent).index(body), node)
    ET.SubElement(node, "value").text = value


def _storage_read(parent: ET.Element, name: str, url: str) -> None:
    request = ET.SubElement(
        parent,
        "send-request",
        {
            "mode": "new",
            "response-variable-name": name,
            "timeout": "2",
            "ignore-error": "true",
        },
    )
    ET.SubElement(request, "set-url").text = url
    ET.SubElement(request, "set-method").text = "GET"
    for header, value in (
        ("x-ms-version", "2020-12-06"),
        ("Accept", "application/json;odata=nometadata"),
        ("Cache-Control", "no-cache"),
    ):
        _header(request, header, value)
    ET.SubElement(
        request,
        "authentication-managed-identity",
        {
            "resource": "https://storage.azure.com/",
        },
    )


def availability_denial(*, disabled: bool = False) -> ET.Element:
    response = ET.Element("return-response")
    code = "model_disabled" if disabled else "model_availability_unavailable"
    ET.SubElement(
        response,
        "set-status",
        {
            "code": "403" if disabled else "503",
            "reason": "Forbidden" if disabled else "Service Unavailable",
        },
    )
    _header(response, "Content-Type", "application/json")
    _header(response, "Cache-Control", "no-store")
    _header(
        response,
        "x-request-id",
        '@(context.Variables.GetValueOrDefault<string>("callerRequestId", '
        'context.RequestId.ToString()))',
    )
    _header(response, "x-correlation-id", "@(context.RequestId.ToString())")
    if not disabled:
        _header(response, "Retry-After", "1")
    ET.SubElement(response, "set-body").text = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "permission_error" if disabled else "api_error",
                "code": code,
                "param": "model",
                "message": "The requested model is disabled"
                if disabled
                else "Model availability could not be verified",
            },
        },
        separators=(",", ":"),
    )
    return response


def node_admission_guard() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                "@(!context.Deployment.Gateway.IsManaged && string.IsNullOrEmpty("
                'context.Variables.GetValueOrDefault<string>("availabilityProbeNonce", "")))'
            )
        },
    )
    _variable(branch, "availabilityNodeKey", NODE_ROW_KEY)
    _storage_read(branch, "availabilityNodeRead", NODE_URL)
    _variable(branch, "availabilityNodeAllowed", READ_NODE)
    rejected = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@(!(bool)context.Variables["availabilityNodeAllowed"])',
        },
    )
    rejected.append(availability_denial())
    return root


def availability_probe_guard() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                '@(context.Variables.GetValueOrDefault<string>("availabilitySurfaceKind", "")'
                ' == "inference" && !string.IsNullOrEmpty('
                'context.Variables.GetValueOrDefault<string>("availabilityProbeNonce", "")))'
            )
        },
    )
    response = availability_denial()
    for header, value in (
        ("version", "1"),
        ("probe", '@((string)context.Variables["availabilityProbeNonce"])'),
        ("kernel", '@((string)context.Variables["availabilityKernelDigest"])'),
        ("capability", '@((string)context.Variables["availabilityCapabilityDigest"])'),
        ("surface", '@((string)context.Variables["availabilitySurfaceDigest"])'),
        ("target", '@((string)context.Variables["availabilityTargetId"])'),
        ("revision", "@(context.Api.Revision)"),
        ("gateway", "@(context.Deployment.Gateway.Id)"),
        ("instance", "@(context.Deployment.Gateway.InstanceId)"),
        ("store", STORE_IDENTITY),
    ):
        _header(response, "x-turnstile-availability-" + header, value)
    branch.append(response)
    invalid = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                '@(context.Request.Method == "HEAD" || ('
                'context.Variables.GetValueOrDefault<string>("availabilitySurfaceKind", "")'
                ' == "inference" && context.Request.Method != "POST"))'
            )
        },
    )
    denied = availability_denial()
    status = denied.find("set-status")
    assert status is not None
    status.set("code", "405")
    status.set("reason", "Method Not Allowed")
    _header(denied, "Allow", "POST")
    invalid.append(denied)
    return root


def admission_guard() -> ET.Element:
    root = ET.Element("choose")
    operations = (
        "chat-completions",
        "responses",
        "responses-compact",
        "anthropic-messages",
        "anthropic-count-tokens",
        "images-generations",
    )
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": "@("
            + " || ".join(f'context.Operation.Id == "{name}"' for name in operations)
            + ")"
        },
    )
    _variable(branch, "availabilityBindingValid", BINDING_VALID)
    invalid = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@(!(bool)context.Variables["availabilityBindingValid"])',
        },
    )
    invalid.append(availability_denial())
    _storage_read(branch, "availabilityRead", STATE_URL)
    _variable(branch, "availabilityDecision", READ_STATE)
    outcomes = ET.SubElement(branch, "choose")
    for outcome in ("disabled", "unavailable"):
        rejected = ET.SubElement(
            outcomes,
            "when",
            {
                "condition": (
                    '@((string)JObject.Parse((string)context.Variables["availabilityDecision"])["outcome"]'
                    f' == "{outcome}")'
                )
            },
        )
        trace = ET.SubElement(
            rejected,
            "trace",
            {
                "source": "turnstile-model-availability",
                "severity": "information",
            },
        )
        ET.SubElement(trace, "message").text = (
            "model_disabled" if outcome == "disabled" else "model_availability_unavailable"
        )
        ET.SubElement(trace, "metadata", {"name": "provider_dispatched", "value": "false"})
        ET.SubElement(
            trace,
            "metadata",
            {
                "name": "correlation_id",
                "value": "@(context.RequestId.ToString())",
            },
        )
        rejected.append(availability_denial(disabled=outcome == "disabled"))
    branch.append(node_admission_guard())
    return root


def availability_kernel_digest() -> str:
    parts = (
        RESOLVE_MODEL,
        BINDING_VALID,
        READ_STATE,
        PROBE_NONCE,
        NODE_ROW_KEY,
        NODE_URL,
        READ_NODE,
        STORE_IDENTITY,
        CATALOG_URL,
        CATALOG_PAGE,
        FILTER_CATALOG,
        component_digest(admission_guard()),
        component_digest(availability_probe_guard()),
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def availability_capability_digest(
    snapshot: ModelAvailabilityPublication | ModelAvailabilityPolicyAdoption,
) -> str:
    content = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((availability_kernel_digest() + content).encode()).hexdigest()


def availability_operation_digest(root: ET.Element) -> str:
    source = deepcopy(root)
    inbound = source.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Availability operation inbound is missing")
    for marker in inbound.findall("set-variable[@name='availabilitySurfaceDigest']"):
        inbound.remove(marker)
    return component_digest(source)


def _admission_locations(root: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Availability requires a parent inbound policy")
    locations = [
        (branch, node)
        for parent in inbound
        if parent.tag == "choose"
        for branch in parent
        for node in branch
        if node.tag == "choose"
        and (condition := node.find("when")) is not None
        and "selectedModelKnown" in condition.get("condition", "")
    ]
    if len(locations) != 2:
        raise PolicyCompilationError("Availability requires both authenticated admission paths")
    for parent, anchor in locations:
        if any(
            child.tag in {"llm-token-limit", "rate-limit-by-key", "send-request"}
            for node in list(parent)[: list(parent).index(anchor)]
            for child in node.iter()
        ):
            raise PolicyCompilationError("Availability must precede counting and reservations")
    return locations


def is_pre_admission_denial(root: ET.Element, response: ET.Element) -> bool:
    status = response.find("set-status")
    code = status.get("code", "") if status is not None else ""
    if code.isdigit():
        return 400 <= int(code) <= 599
    from .apim_budget_execution import _deny

    parent = next((parent for parent in root.iter() if response in list(parent)), None)
    if parent is None or parent.tag != "when":
        return False
    error = parent.find("set-variable[@name='executionError']")
    if error is None:
        return False
    reference = ET.Element("inbound")
    _deny(reference, parent.get("condition", ""), error.get("value", ""))
    expected = reference.find("choose/when")
    return expected is not None and _matches_policy_component(parent, expected, {})


def validate_availability_parent(root: ET.Element) -> None:
    bindings: dict[str, str] = {}
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Availability requires a parent inbound policy")
    for name, expected in (
        ("availabilityProtocolVersion", "@(1)"),
        ("availabilityKernelDigest", availability_kernel_digest()),
        ("availabilityProbeNonce", PROBE_NONCE),
        ("availabilityTableEndpoint", "__LEDGER_TABLE_ENDPOINT__"),
        ("availabilityTableName", "__LEDGER_TABLE_NAME__"),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if (
            len(nodes) != 1
            or nodes[0] not in list(inbound)
            or not _matches_policy_component(
                nodes[0],
                ET.Element(
                    "set-variable",
                    {"name": name, "value": expected},
                ),
                bindings,
            )
        ):
            raise PolicyCompilationError("Availability protocol integrity changed")
    locations = _admission_locations(root)
    parents = {child: parent for parent in root.iter() for child in parent}
    branches = {parents[parent] for parent, _anchor in locations}
    if len(branches) != 1:
        raise PolicyCompilationError("Availability admission paths are not exhaustive")
    authentication = next(iter(branches))
    if authentication not in list(inbound) or [
        node.tag for node in authentication if isinstance(node.tag, str)
    ] != ["when", "otherwise"]:
        raise PolicyCompilationError("Availability admission paths are not exhaustive")
    for node in (
        child
        for item in list(inbound)[: list(inbound).index(authentication)]
        for child in item.iter()
    ):
        if node.tag in {
            "llm-token-limit",
            "rate-limit",
            "rate-limit-by-key",
            "quota",
            "quota-by-key",
            "send-request",
            "forward-request",
            "include-fragment",
        }:
            raise PolicyCompilationError("Work cannot precede model availability admission")
        if node.tag == "return-response" and not is_pre_admission_denial(root, node):
            raise PolicyCompilationError("A response can bypass model availability")
    names = {
        "availabilityProtocolVersion",
        "availabilityTableEndpoint",
        "availabilityTableName",
        "availabilityBindingValid",
        "availabilityDecision",
        "availabilityKernelDigest",
        "availabilityProbeNonce",
        "availabilityNodeKey",
        "availabilityNodeAllowed",
    }
    if any(
        node.get("name", "").startswith("availability") and node.get("name") not in names
        for node in root.iter("set-variable")
    ):
        raise PolicyCompilationError("Parent cannot shadow operation availability identity")
    if any(
        len(root.findall(f".//set-variable[@name='{name}']")) != 2
        for name in ("availabilityBindingValid", "availabilityDecision")
    ):
        raise PolicyCompilationError("Availability guards are incomplete or duplicated")
    if any(isinstance(node.tag, str) and node.tag.startswith("cache-") for node in root.iter()):
        raise PolicyCompilationError("Availability requires uncached admission and discovery")
    for parent, anchor in locations:
        index = list(parent).index(anchor)
        if (
            index < 1
            or index + 1 >= len(parent)
            or not _matches_policy_component(
                parent[index - 1], availability_probe_guard(), bindings
            )
            or not _matches_policy_component(parent[index + 1], admission_guard(), bindings)
        ):
            raise PolicyCompilationError("Availability guard integrity or placement changed")


def compose_availability_parent(source: str) -> str:
    root = parse_policy(source)
    if root.find(".//set-variable[@name='availabilityProtocolVersion']") is not None:
        validate_availability_parent(root)
        return source
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Availability requires a parent inbound policy")
    for parent, anchor in _admission_locations(root):
        parent.insert(list(parent).index(anchor), availability_probe_guard())
        parent.insert(list(parent).index(anchor) + 1, admission_guard())
    for index, (name, value) in enumerate(
        (
            ("availabilityProtocolVersion", "@(1)"),
            ("availabilityTableEndpoint", "__LEDGER_TABLE_ENDPOINT__"),
            ("availabilityTableName", "__LEDGER_TABLE_NAME__"),
            ("availabilityKernelDigest", availability_kernel_digest()),
            ("availabilityProbeNonce", PROBE_NONCE),
        )
    ):
        inbound.insert(index, ET.Element("set-variable", {"name": name, "value": value}))
    validate_availability_parent(root)
    return serialize_policy(root)


def without_availability_parent(root: ET.Element) -> ET.Element:
    validate_availability_parent(root)
    result = deepcopy(root)
    for parent, anchor in _admission_locations(result):
        parent.remove(parent[list(parent).index(anchor) + 1])
        parent.remove(parent[list(parent).index(anchor) - 1])
    inbound = result.find("inbound")
    assert inbound is not None
    for name in (
        "availabilityProtocolVersion",
        "availabilityTableEndpoint",
        "availabilityTableName",
        "availabilityKernelDigest",
        "availabilityProbeNonce",
    ):
        node = inbound.find(f"set-variable[@name='{name}']")
        assert node is not None
        inbound.remove(node)
    return result


def catalog_read() -> list[ET.Element]:
    root = ET.Element("inbound")
    _variable(root, "availabilityCatalogPage", '{"ok":true,"pages":0,"states":{}}')
    retry = ET.SubElement(
        root,
        "retry",
        {
            "count": "3",
            "interval": "0",
            "first-fast-retry": "true",
            "condition": (
                '@((bool?)JObject.Parse((string)context.Variables["availabilityCatalogPage"])["ok"]'
                ' == true && JObject.Parse((string)context.Variables["availabilityCatalogPage"])'
                '["next_partition"]?.Type == JTokenType.String)'
            ),
        },
    )
    _storage_read(retry, "availabilityCatalogRead", CATALOG_URL)
    _variable(retry, "availabilityCatalogPage", CATALOG_PAGE)
    return list(root)


def compile_availability_operation(
    source: str,
    publication: GatewayPublication,
    snapshot: ModelAvailabilityPublication,
    *,
    catalog: bool = False,
) -> str:
    snapshot.validate_publication(publication)
    bindings = {
        binding.model.model_key.casefold(): binding for binding in publication.desired_spec.bindings
    }
    routes = []
    for alias, model_id in snapshot.models.items():
        binding = bindings.get(alias.casefold())
        routes.append(
            {
                "alias": alias,
                "model_id": str(model_id),
                "gateways": [
                    deployment.native_gateway_id for deployment in binding.serving_deployments
                ]
                if binding and binding.deployment_scope.mode.value == "selected"
                else None,
            }
        )
    return _compile_availability_operation(source, snapshot, routes, catalog=catalog)


def compile_availability_adoption(
    source: str,
    adoption: ModelAvailabilityPolicyAdoption,
    operation_id: str,
    *,
    catalog: bool = False,
) -> str:
    adoption.validate_source(operation_id, source)
    routes = [
        {"alias": alias, "model_id": str(model_id), "gateways": adoption.serving_gateways[alias]}
        for alias, model_id in adoption.models.items()
    ]
    return _compile_availability_operation(source, adoption, routes, catalog=catalog)


def _compile_availability_operation(
    source: str,
    snapshot: ModelAvailabilityPublication | ModelAvailabilityPolicyAdoption,
    routes: Sequence[dict[str, Any]],
    *,
    catalog: bool,
) -> str:
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None or len(inbound.findall("base")) != 1:
        raise PolicyCompilationError("Availability requires exactly one operation base policy")
    base = inbound.find("base")
    assert base is not None
    nodes = ET.Element("inbound")
    for name, value in (
        ("availabilityTargetId", str(snapshot.target_id)),
        ("availabilityApiId", snapshot.api_id),
        ("availabilityApiRevision", snapshot.revision),
        ("availabilitySurfaceKind", "catalog" if catalog else "inference"),
        ("availabilityCapabilityDigest", availability_capability_digest(snapshot)),
    ):
        _variable(nodes, name, value)
    if not catalog:
        _variable(nodes, "availabilityRoutes", json.dumps(routes, separators=(",", ":")))
        _variable(nodes, "availabilityModelId", RESOLVE_MODEL)
    for index, node in enumerate(nodes):
        inbound.insert(index, node)
    ready = ET.Element("choose")
    missing = ET.SubElement(
        ready,
        "when",
        {
            "condition": (
                '@(context.Variables.GetValueOrDefault<int>("availabilityProtocolVersion", 0) != 1)'
            )
        },
    )
    missing.append(availability_denial())
    index = list(inbound).index(base)
    inbound.insert(index + 1, ready)
    inbound.insert(
        index + 2,
        ET.Element(
            "set-header",
            {
                "name": "x-turnstile-availability-probe",
                "exists-action": "delete",
            },
        ),
    )
    if catalog:
        inbound.insert(index + 3, node_admission_guard())
        for response in list(root.iter("return-response")):
            status, body = response.find("set-status"), response.find("set-body")
            if status is None or status.get("code") != "200" or body is None:
                continue
            payload = json.loads(body.text or "{}")
            models = payload.get("data")
            if not isinstance(models, list):
                raise PolicyCompilationError("Published catalog response is invalid")
            for model in models:
                if model["id"] not in snapshot.models:
                    raise PolicyCompilationError("Published catalog identity is missing")
                model["logical_model_id"] = str(snapshot.models[model["id"]])
            parent = next(parent for parent in root.iter() if response in list(parent))
            insertion = list(parent).index(response)
            generated = ET.Element("inbound")
            _variable(
                generated, "availabilityCatalogScope", json.dumps(models, separators=(",", ":"))
            )
            generated.extend(catalog_read())
            _variable(generated, "availabilityCatalogResult", FILTER_CATALOG)
            failed = ET.SubElement(
                ET.SubElement(generated, "choose"),
                "when",
                {
                    "condition": (
                        '@((bool?)JObject.Parse((string)context.Variables["availabilityCatalogResult"])'
                        '["ok"] != true)'
                    )
                },
            )
            failed.append(availability_denial())
            for offset, node in enumerate(generated):
                parent.insert(insertion + offset, node)
            body.text = (
                '@(JObject.Parse((string)context.Variables["availabilityCatalogResult"])["body"]'
                ".ToString(Newtonsoft.Json.Formatting.None))"
            )
            for header, value in (
                ("Cache-Control", "no-store, max-age=0"),
                ("Pragma", "no-cache"),
                ("x-turnstile-availability-version", "1"),
            ):
                _header(response, header, value)
            for header, variable in (
                ("probe", "availabilityProbeNonce"),
                ("kernel", "availabilityKernelDigest"),
                ("capability", "availabilityCapabilityDigest"),
                ("surface", "availabilitySurfaceDigest"),
            ):
                _header(
                    response,
                    "x-turnstile-availability-" + header,
                    f'@((string)context.Variables["{variable}"])',
                )
            for header, value in (
                ("store", STORE_IDENTITY),
                (
                    "digest",
                    '@((string)JObject.Parse((string)context.Variables["availabilityCatalogResult"])["digest"])',
                ),
                ("target", str(snapshot.target_id)),
                ("revision", snapshot.revision),
                ("gateway", "@(context.Deployment.Gateway.Id)"),
                ("instance", "@(context.Deployment.Gateway.InstanceId)"),
            ):
                _header(response, "x-turnstile-availability-" + header, value)
    inbound.insert(
        0,
        ET.Element(
            "set-variable",
            {
                "name": "availabilitySurfaceDigest",
                "value": availability_operation_digest(root),
            },
        ),
    )
    return serialize_policy(root)
