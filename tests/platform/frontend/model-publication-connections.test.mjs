import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"
import * as modelAvailability from "../../../frontend/src/lib/model-availability.ts"

import {
  isDatabricksWorkspaceUrl,
  preferredPublicationConnection,
  publicationConnectionAuth,
  publicationConnectionEndpoint,
  publicationConnectionNeedsCredential,
  publicationConnections,
  publicationConnectionTarget,
} from "../../../frontend/src/components/model-management/model-publication-connections.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const queryLibrary = frontendRequire("@tanstack/react-query")
const postcss = frontendRequire("postcss")
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const queriesEntry = fileURLToPath(new URL("../../../frontend/src/data-sources/apim/queries.ts", import.meta.url))
const queriesBundle = await rolldown({ input: queriesEntry, external: id => id !== queriesEntry, treeshake: false })
let queriesCode
try { queriesCode = (await queriesBundle.generate({ format: "cjs" })).output[0].code }
finally { await queriesBundle.close() }

const apiEntry = fileURLToPath(new URL("../../../frontend/src/data-sources/apim/api.ts", import.meta.url))
const apiBundle = await rolldown({ input: apiEntry, external: id => id !== apiEntry, treeshake: false })
let apiCode
try { apiCode = (await apiBundle.generate({ format: "cjs" })).output[0].code }
finally { await apiBundle.close() }

const pageSource = readFileSync(new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url), "utf8")
const dialogSource = pageSource.slice(pageSource.indexOf("export function GatewayNameDialog("), pageSource.indexOf("\nfunction GatewayDeleteDialog("))
assert.ok(dialogSource.startsWith("export function GatewayNameDialog("))
const dialogBundle = await rolldown({
  input: "name-dialog.tsx", transform: { jsx: { runtime: "automatic" } },
  external: ["react", "react/jsx-runtime", "primitives"],
  plugins: [{ name: "production-name-dialog", resolveId: id => id === "name-dialog.tsx" ? id : null,
    load: id => id === "name-dialog.tsx" ? `import { useState, useId } from "react";
      import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter, Input, Button, Save } from "primitives";
      ${dialogSource}` : null }],
})
let dialogCode
try { dialogCode = (await dialogBundle.generate({ format: "cjs" })).output[0].code }
finally { await dialogBundle.close() }

function gatewayNameHarness(overrides = {}) {
  let draft = "Original name"
  const saved = []
  const closed = []
  const exports = {}
  const imports = {
    react: { useId: () => "gateway-name", useState: () => [draft, value => { draft = value }] },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    primitives: new Proxy({}, { get: (_target, key) => key }),
  }
  runInNewContext(dialogCode, { exports, require: name => imports[name] })
  const props = { gateway: { id: "unit", name: draft }, available: true, busy: false, error: null,
    onSave: value => saved.push(value), onClose: () => closed.push(true), ...overrides }
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const find = predicate => nodes(exports.GatewayNameDialog(props)).find(predicate)
  return { props, saved, closed, find,
    input(value) { find(node => node.type === "Input").props.onChange({ target: { value } }) },
    submit() { find(node => node.type === "form").props.onSubmit({ preventDefault() {} }) },
  }
}

test("infrastructure action menus inherit the shared surface without a second border", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const rule = sheet.nodes.find(node => node.type === "rule" && node.selector === ".infrastructure-row-menu")
  assert.ok(rule)
  assert.deepEqual(Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value])), {
    "min-width": "188px",
  })
  const shared = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
    .nodes.find(node => node.type === "rule" && node.selector === '[data-slot="dropdown-menu-content"]')
  const reference = sheet.nodes.find(node => node.type === "rule" && node.selectors.includes(".model-filter-menu"))
  assert.ok(shared)
  assert.ok(reference)
  const declarations = rule => Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  for (const property of ["outline", "box-shadow", "border-radius", "background", "color", "padding"]) {
    assert.equal(declarations(shared)[property], declarations(reference)[property])
  }
})

test("selects and filter dropdowns share one compact theme contract", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/finops.css", import.meta.url), "utf8"))
  const rule = sheet.nodes.find(node => node.type === "rule" && node.selectors.includes('[data-slot="select-trigger"]'))
  assert.ok(rule)
  for (const selector of [".model-filter-trigger", ".finops-filter-menu-trigger", ".people-model-select-trigger"]) {
    assert.ok(rule.selectors.includes(selector))
  }
  const declarations = Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  const expected = {
    height: "28px", "font-size": "12px", "font-weight": "500", border: "1px solid var(--border)",
    "border-radius": "7px", padding: "0 10px", background: "var(--background)", color: "var(--muted-foreground)",
  }
  assert.deepEqual(Object.fromEntries(Object.keys(expected).map(name => [name, declarations[name]])), expected)
  const focus = sheet.nodes.find(node => node.type === "rule" && node.selector.startsWith(':is([data-slot="select-trigger"]') && node.selector.endsWith(":focus-visible"))
  assert.ok(focus.selector.includes(".model-filter-trigger"))
  const popup = sheet.nodes.find(node => node.type === "rule" && node.selector === '[data-slot="select-content"]')
  assert.equal(popup.nodes.find(node => node.prop === "border-radius").value, "var(--radius)")
  assert.ok(sheet.nodes.some(node => node.type === "rule" && node.selector === '[data-slot="select-trigger"][aria-invalid="true"]'))
})

test("select page layouts do not override shared filter colors or density", () => {
  const sheet = postcss.parse(readFileSync(new URL("../../../frontend/src/styles/base.css", import.meta.url), "utf8"))
  const layoutSelectors = [".settings-section .settings-select-trigger", ".dimension-picker .dimension-select-trigger", ".settings-select-trigger"]
  const forbidden = new Set(["background", "color", "border-color", "height", "min-height", "font-size", "padding"])
  sheet.walkRules(rule => {
    if (!rule.selectors.some(selector => layoutSelectors.includes(selector))) return
    for (const declaration of rule.nodes.filter(node => node.type === "decl")) {
      assert.equal(forbidden.has(declaration.prop), false, `${rule.selector}: ${declaration.prop}`)
    }
  })
})

test("name dialog submits only a trimmed changed name and keeps failed drafts", () => {
  const harness = gatewayNameHarness()
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  for (const value of ["   ", "x".repeat(121), " Original name "]) {
    harness.input(value)
    harness.submit()
    assert.equal(harness.saved.length, 0)
  }
  harness.input("  Finance  ")
  harness.props.error = "Request failed"
  assert.equal(harness.find(node => node.type === "Input").props.value, "  Finance  ")
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Request failed")
  harness.submit()
  assert.deepEqual(harness.saved, ["Finance"])
})

test("name dialog prevents writes when unsupported or pending and blocks pending close", () => {
  for (const overrides of [{ busy: true }, { available: false }]) {
    const harness = gatewayNameHarness(overrides)
    harness.input("Finance")
    harness.submit()
    assert.equal(harness.saved.length, 0)
    assert.equal(harness.find(node => node.type === "Input").props.disabled, true)
    assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  }
  const harness = gatewayNameHarness({ busy: true })
  harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  assert.equal(harness.closed.length, 0)
  harness.props.busy = false
  harness.find(node => node.type === "Dialog").props.onOpenChange(false)
  assert.equal(harness.closed.length, 1)
})

for (const minimal of [false, true]) test(`name API supports minimal and full replies (minimal=${minimal})`, async () => {
  const exports = {}
  const requests = []
  const imports = {
    "../../api/client": { request: async (path, options) => {
      requests.push({ path, options })
      return minimal ? undefined : registry({ gateway_name_edit_supported: true })
    } },
    "../../locales/index": {},
    "../../components/model-management/openai-compatible": {},
  }
  runInNewContext(apiCode, { exports, require: name => imports[name] })
  const result = await exports.dataSource.renameGateway("unit/gateway", "Finance")
  assert.equal(requests.length, 1)
  assert.equal(requests[0].path, "/api/v1/model-management/gateways/unit%2Fgateway/name")
  assert.equal(requests[0].options.method, "PATCH")
  assert.equal(requests[0].options.headers.Prefer, "return=minimal")
  assert.deepEqual(JSON.parse(requests[0].options.body), { name: "Finance" })
  if (minimal) assert.equal(result, undefined)
  else assert.equal(result.gateway_name_edit_supported, true)
})

function registryCacheHarness(readRegistry) {
  const exports = {}
  const imports = {
    "@tanstack/react-query": queryLibrary,
    "../../api/cache-policies": { cachePolicies: { aggregate: {}, reference: {} } },
    "../../lib/timezone": { resolveTimezone: () => "UTC" },
    "../../lib/model-availability": modelAvailability,
    "./api": { dataSource: { registry: readRegistry } },
  }
  runInNewContext(queriesCode, { exports, require: name => {
    assert.ok(name in imports, `Unexpected queries import: ${name}`)
    return imports[name]
  } })
  return exports
}

for (const partial of [false, true]) test(`committed registry survives an older response (partial=${partial})`, async () => {
  let releaseRead
  let markStarted
  const started = new Promise(resolve => { markStarted = resolve })
  const original = registry()
  const harness = registryCacheHarness(async () => {
    markStarted()
    return new Promise(resolve => { releaseRead = resolve })
  })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    const pending = client.fetchQuery(harness.finopsQueries.registry()).catch(error => error)
    await started
    await harness.storeRegistry(client, partial
      ? current => ({ ...current, runtimes: current.runtimes.map(runtime => ({ ...runtime, health_status: "available" })) })
      : registry({ models: [{ id: "saved-model" }] }))
    releaseRead(original)
    await pending
    const actual = client.getQueryData(harness.finopsKeys.registry)
    if (partial) {
      assert.equal(actual.runtimes[0].health_status, "available")
      assert.deepEqual(actual.gateways, original.gateways)
    } else assert.equal(actual.models[0].id, "saved-model")
  } finally { client.clear() }
})

test("failed background refresh retains the committed registry", async () => {
  const harness = registryCacheHarness(async () => { throw new Error("registry unavailable") })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const saved = registry({ models: [{ id: "saved-model" }] })
  try {
    await harness.storeRegistry(client, saved)
    await assert.rejects(client.fetchQuery(harness.finopsQueries.registry()), /registry unavailable/)
    assert.deepEqual(client.getQueryData(harness.finopsKeys.registry), saved)
  } finally { client.clear() }
})

test("registry writers await cancellation and use the shared cache boundary", async () => {
  let releaseCancel
  const calls = []
  const client = {
    cancelQueries() { calls.push("cancel"); return new Promise(resolve => { releaseCancel = resolve }) },
    setQueryData() { calls.push("write") },
  }
  const saving = registryCacheHarness(() => {}).storeRegistry(client, registry())
  assert.deepEqual(calls, ["cancel"])
  releaseCancel()
  await saving
  assert.deepEqual(calls, ["cancel", "write"])
  const page = readFileSync(new URL("../../../frontend/src/pages/model-management-page.tsx", import.meta.url), "utf8")
  assert.doesNotMatch(page, /client\.setQueryData(?:<ModelRegistry>)?\(finopsKeys\.registry/)
  assert.equal((page.match(/await storeRegistry\(client,/g) ?? []).length, 7)
})

test("minimal name acknowledgement updates only the selected gateway and its runtime labels", async () => {
  const harness = registryCacheHarness(async () => { throw new Error("registry unavailable") })
  const client = new queryLibrary.QueryClient({ defaultOptions: { queries: { retry: false } } })
  const original = registry({ gateways: [gateway("gateway-a", { name: "Original" }), gateway("gateway-b", { name: "Other" })] })
  try {
    client.setQueryData(harness.finopsKeys.registry, original)
    await harness.storeGatewayName(client, "gateway-a", "  Finance  ")
    const saved = client.getQueryData(harness.finopsKeys.registry)
    assert.equal(saved.gateways[0].name, "Finance")
    assert.deepEqual(saved.gateways[1], original.gateways[1])
    assert.ok(saved.runtimes.filter(runtime => runtime.gateway_profile_id === "gateway-a").every(runtime => runtime.gateway_name === "Finance"))
    assert.deepEqual(saved.runtimes.filter(runtime => runtime.gateway_profile_id !== "gateway-a"), original.runtimes.filter(runtime => runtime.gateway_profile_id !== "gateway-a"))
    await assert.rejects(client.fetchQuery(harness.finopsQueries.registry()), /registry unavailable/)
    assert.equal(client.getQueryData(harness.finopsKeys.registry).gateways[0].name, "Finance")
  } finally { client.clear() }
})

function gateway(id, overrides = {}) {
  return { id, implementation: "apim", enabled: true, ...overrides }
}

function provider(id, overrides = {}) {
  return { id, enabled: true, ...overrides }
}

function connection(id, gatewayId, overrides = {}) {
  return {
    id, gateway_profile_id: gatewayId, provider_id: "provider-a", enabled: true,
    is_default: false, brand_key: "generic", config: { api_format: "openai_chat" },
    ...overrides,
  }
}

function registry(overrides = {}) {
  return {
    databricks_connections_supported: true,
    gateways: [gateway("gateway-a"), gateway("gateway-b"), gateway("empty")],
    providers: [provider("provider-a"), provider("provider-b")],
    runtimes: [
      connection("a-first", "gateway-a"),
      connection("a-default", "gateway-a", { is_default: true }),
      connection("a-other-provider", "gateway-a", { provider_id: "provider-b" }),
      connection("b-default", "gateway-b", { is_default: true }),
    ],
    models: [],
    ...overrides,
  }
}

test("connections belong to the selected gateway across enabled providers", () => {
  assert.deepEqual(publicationConnections(registry(), "gateway-a").map((item) => item.id),
    ["a-first", "a-default", "a-other-provider"])
  assert.deepEqual(publicationConnections(registry(), "gateway-b").map((item) => item.id),
    ["b-default"])
})

test("an explicit eligible selection takes precedence over the default and first connection", () => {
  const connections = publicationConnections(registry(), "gateway-a")
  assert.equal(preferredPublicationConnection(connections)?.id, "a-default")
  assert.equal(preferredPublicationConnection(connections, "a-other-provider")?.id, "a-other-provider")
  assert.equal(preferredPublicationConnection(connections.filter((item) => !item.is_default))?.id,
    "a-first")
})

test("switching gateways never retains an out-of-scope connection", () => {
  const connections = publicationConnections(registry(), "gateway-b")
  assert.equal(preferredPublicationConnection(connections, "a-default")?.id, "b-default")
  assert.throws(() => publicationConnectionTarget(registry(), "gateway-b", "a-default"),
    /connection_unavailable/)
})

test("an empty gateway stays empty without borrowing or creating a connection", () => {
  const connections = publicationConnections(registry(), "empty")
  assert.deepEqual(connections, [])
  assert.equal(preferredPublicationConnection(connections, "a-default"), undefined)
  assert.throws(() => publicationConnectionTarget(registry(), "empty", "new-runtime"),
    /connection_unavailable/)
})

test("missing, disabled and non-APIM gateways fail closed", () => {
  for (const overrides of [{ enabled: false }, { implementation: "direct" }]) {
    const data = registry({ gateways: [gateway("gateway-a", overrides)] })
    assert.deepEqual(publicationConnections(data, "gateway-a"), [])
    assert.throws(() => publicationConnectionTarget(data, "gateway-a", "a-default"),
      /connection_unavailable/)
  }
  assert.deepEqual(publicationConnections(registry(), "unknown"), [])
})

test("disabled or missing providers and disabled runtimes are not publishable", () => {
  const data = registry({
    providers: [provider("provider-a"), provider("provider-b", { enabled: false })],
    runtimes: [
      connection("enabled", "gateway-a"),
      connection("disabled", "gateway-a", { enabled: false }),
      connection("disabled-provider", "gateway-a", { provider_id: "provider-b" }),
      connection("missing-provider", "gateway-a", { provider_id: "unknown" }),
    ],
  })
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id), ["enabled"])
  for (const id of ["disabled", "disabled-provider", "missing-provider"]) {
    assert.throws(() => publicationConnectionTarget(data, "gateway-a", id), /connection_unavailable/)
  }
})

test("registered zero-model Foundry connections require a managed nonempty Project", () => {
  const config = {
    control_plane_managed: true,
    project_endpoint: "https://example.services.ai.azure.com/api/projects/sample",
    auth_strategy: "named_value_api_key", credential_provisioned: false,
  }
  const data = registry({ runtimes: [
    connection("registered", "gateway-a", { brand_key: "microsoft_foundry", config }),
    connection("unmanaged", "gateway-a", { brand_key: "microsoft_foundry",
      config: { ...config, control_plane_managed: false } }),
    connection("blank-project", "gateway-a", { brand_key: "microsoft_foundry",
      config: { ...config, project_endpoint: " " } }),
    connection("unsupported", "gateway-a", { config: {} }),
  ] })
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id), ["registered"])
})

test("existing Bedrock, Databricks and Anthropic runtimes use the same selection path", () => {
  const data = registry({ runtimes: [
    connection("bedrock", "gateway-a", { brand_key: "amazon_bedrock", config: {} }),
    connection("databricks", "gateway-a", { brand_key: "azure_databricks", config: {
      control_plane_managed: true, api_format: "anthropic_messages",
      workspace_url: "https://adb-unit.1.azuredatabricks.net",
    } }),
    connection("messages", "gateway-a", { config: { api_format: "anthropic_messages" } }),
  ] })
  assert.deepEqual(publicationConnections(data, "gateway-a").map((item) => item.id),
    ["bedrock", "databricks", "messages"])
})

test("runtime authentication overrides provider defaults without exposing configuration", () => {
  assert.equal(publicationConnectionAuth(connection("key", "gateway-a", {
    config: { auth_strategy: "named_value_api_key" },
  }), provider("provider-a", { auth_type: "azure_ad" })), "api_key")
  assert.equal(publicationConnectionAuth(connection("identity", "gateway-a", {
    config: { auth_strategy: "managed_identity" },
  }), provider("provider-a", { auth_type: "api_key" })), "managed_identity")
  assert.equal(publicationConnectionAuth(connection("legacy", "gateway-a")), "connection")
})

test("Databricks publication requires managed Workspace and declared backend capabilities", () => {
  const config = {
    control_plane_managed: true, api_format: "anthropic_messages",
    workspace_url: "https://adb-unit.1.azuredatabricks.net",
    auth_strategy: "managed_identity",
  }
  const data = registry({ runtimes: [
    connection("managed", "gateway-a", { brand_key: "azure_databricks", config }),
    connection("legacy", "gateway-a", { brand_key: "azure_databricks", config: {} }),
  ] })
  assert.deepEqual(publicationConnections(data, "gateway-a").map(item => item.id), ["managed"])
  assert.deepEqual(publicationConnections({ ...data, databricks_connections_supported: false }, "gateway-a"), [])
  for (const url of ["http://adb-unit.1.azuredatabricks.net", "https://example.test", `${config.workspace_url}/path`, `${config.workspace_url}?token=unit`]) {
    assert.equal(isDatabricksWorkspaceUrl(url), false)
  }
})

test("OAuth connection credentials use their own field and remain capability gated", () => {
  const data = registry({ databricks_oauth_supported: true, runtimes: [
    connection("oauth", "gateway-a", { brand_key: "azure_databricks", config: {
      control_plane_managed: true, api_format: "anthropic_messages",
      workspace_url: "https://adb-unit.1.azuredatabricks.net",
      auth_strategy: "oauth_client_credentials", credential_provisioned: false,
    } }),
  ] })
  assert.equal(publicationConnectionAuth(data.runtimes[0]), "oauth_m2m")
  assert.deepEqual(publicationConnections({ ...data, databricks_oauth_supported: false }, "gateway-a"), [])
  assert.throws(() => publicationConnectionTarget(data, "gateway-a", "oauth"), /credential_required/)
  assert.deepEqual(publicationConnectionTarget(data, "gateway-a", "oauth", " unit-secret ").runtime,
    { existing_id: "oauth", oauth_client_secret: "unit-secret" })
  data.runtimes[0].config.credential_provisioned = true
  assert.deepEqual(publicationConnectionTarget(data, "gateway-a", "oauth", "unused").runtime,
    { existing_id: "oauth" })
})

test("endpoint summaries omit userinfo, query and fragment and require HTTPS", () => {
  const runtime = connection("redacted", "gateway-a", { config: {
    base_url: "https://sample-user:sample-value@api.example.test/v1/?credential=sample#fragment",
  } })
  assert.equal(publicationConnectionEndpoint(runtime), "https://api.example.test/v1")
  assert.equal(publicationConnectionEndpoint(connection("fallback", "gateway-a", { config: {
    project_endpoint: "invalid", base_url: "http://invalid.test", backend_url: "https://api.example.test/",
  } })), "https://api.example.test")
  assert.equal(publicationConnectionEndpoint(connection("missing", "gateway-a")), null)
})

test("submission carries existing IDs only and cannot carry new endpoint configuration", () => {
  const target = publicationConnectionTarget(registry(), "gateway-a", "a-other-provider")
  assert.deepEqual(target, {
    gateway_profile_id: "gateway-a",
    provider: { existing_id: "provider-b" },
    runtime: { existing_id: "a-other-provider" },
  })
})

test("only a connection explicitly awaiting its first credential receives a key", () => {
  for (const authStrategy of ["named_value_api_key", "named_value_bearer"]) {
    const data = registry({ runtimes: [connection("pending", "gateway-a", { config: {
      api_format: "openai_chat", credential_provisioned: false, auth_strategy: authStrategy,
    } })] })
    assert.equal(publicationConnectionNeedsCredential(data.runtimes[0]), true)
    assert.throws(() => publicationConnectionTarget(data, "gateway-a", "pending", " "),
      /credential_required/)
    assert.deepEqual(publicationConnectionTarget(data, "gateway-a", "pending", " sample-only ").runtime,
      { existing_id: "pending", api_key: "sample-only" })
    data.runtimes[0].config.credential_provisioned = true
    assert.deepEqual(publicationConnectionTarget(data, "gateway-a", "pending", "unused").runtime,
      { existing_id: "pending" })
  }
  assert.equal(publicationConnectionNeedsCredential(connection("identity", "gateway-a", {
    config: { credential_provisioned: false, auth_strategy: "managed_identity" },
  })), false)
  assert.deepEqual(publicationConnectionTarget(registry(), "gateway-b", "b-default", "unused").runtime,
    { existing_id: "b-default" })
})