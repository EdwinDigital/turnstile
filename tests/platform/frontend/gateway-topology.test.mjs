import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import {
  apimGatewayEntries, gatewayEvidence, gatewayPublicationChoices, resolveGatewaySelection,
} from "../../../frontend/src/lib/gateway-topology.ts"
import * as gatewayTopology from "../../../frontend/src/lib/gateway-topology.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const { QueryClient } = frontendRequire("@tanstack/react-query")
async function compile(path) {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({ input: entry, external: path => path !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const apiCode = await compile("../../../frontend/src/data-sources/apim/api.ts")
const queryCode = await compile("../../../frontend/src/data-sources/apim/queries.ts")
const componentCode = await compile("../../../frontend/src/components/model-management/gateway-targets.tsx")

function componentHarness(exportName, props, { operation = null, mutationPending = false } = {}) {
  const slots = []
  let cursor = 0
  const effects = []
  const syncCalls = []
  const queryClient = { setQueryData() {}, invalidateQueries() { return Promise.resolve() } }
  const imports = {
    react: {
      useId: () => "unit-gateway",
      useEffect: callback => effects.push(callback),
      useState(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
        return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
      },
      useRef(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = { current: initial }
        return slots[index]
      },
    },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": {
      useQueryClient: () => queryClient,
      useQuery: () => ({ data: operation, error: null, refetch() {} }),
      useMutation: options => ({ isPending: mutationPending, error: null, mutate: () => {
        syncCalls.push(options)
        options.onSuccess?.({ operation: { id: "unit-operation" } })
      } }),
    },
    "../../data-sources/apim/api": { dataSource: {} },
    "../../data-sources/apim/queries": {
      finopsKeys: { apimTargets: ["targets"], gatewayReleaseOperation: id => ["operation", id] },
      finopsQueries: { gatewayReleaseOperation: id => ({ queryKey: ["operation", id] }) },
    },
    "../../lib/gateway-topology": gatewayTopology,
  }
  const exports = {}
  runInNewContext(componentCode, { exports, Set, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const render = () => { cursor = 0; return exports[exportName](props) }
  return { props, syncCalls, effects, nodes, render, all: () => nodes(render()), find: predicate => nodes(render()).find(predicate) }
}

class ApiError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status }
}
function apiHarness(request, writeJson = request) {
  const exports = {}
  runInNewContext(apiCode, { exports, require: name => name.includes("api/client")
    ? { request, writeJson, ApiError } : {} })
  return exports.dataSource
}
function queryHarness() {
  const exports = {}
  const imports = {
    "@tanstack/react-query": { queryOptions: options => options },
    "../../api/cache-policies": { cachePolicies: {} },
    "./api": { dataSource: {} },
  }
  runInNewContext(queryCode, { exports, require: name => imports[name] ?? {} })
  return exports
}

const profiles = [{ id: "first", name: "First", implementation: "apim", enabled: true, is_default: true, base_url: "https://first.example.test" }]
function topology(changes = {}) {
  return {
    target: { gateway_profile_id: "first", api_id: "unit-api", service_name: "unit-service", drift_state: "in_sync", capabilities: { self_hosted_gateway: "supported" } },
    deployments: [{ deployment: {
      id: "managed", native_gateway_id: "managed", kind: "managed", display_name: "Managed",
      retired_at: null, declared_state: "present", reachability_state: "reachable",
      endpoint_url: "https://first.example.test", last_probe_at: "2026-09-23T00:00:00Z",
      release_gate: "required", ...changes,
    }, inventory: { associated_api_ids: ["UNIT-API"] }, published_models: [] }],
  }
}

for (const [changes, label] of [
  [{ retired_at: "2026-09-23T00:00:00Z" }, "Retired"],
  [{ declared_state: "absent" }, "Not found"],
  [{ endpoint_url: null }, "Not configured"],
  [{ declared_state: "unknown" }, "Not synchronized"],
  [{ last_probe_at: null }, "Not verified"],
  [{ reachability_state: "unreachable" }, "Unreachable"],
  [{}, "Verified"],
]) test(`Gateway evidence: ${label}`, () => {
  const target = topology(changes)
  assert.equal(gatewayEvidence({ ...target.deployments[0], target }).label, label)
})

test("declared self-hosted Gateway without API membership is not verified", () => {
  const target = topology({ kind: "self_hosted" })
  const entry = { ...target.deployments[0], inventory: { associated_api_ids: ["other-api"] }, target }
  assert.equal(gatewayEvidence(entry).label, "Not associated")
  assert.equal(apimGatewayEntries(topology({ retired_at: "2026-09-23T00:00:00Z" })).length, 0)
})

test("legacy profiles remain selectable only in compatibility mode", () => {
  assert.equal(gatewayPublicationChoices(profiles, null, false)[0].targetId, "first")
  assert.deepEqual(gatewayPublicationChoices(profiles, null, true), [])
})

test("required sibling readiness gates selected optional deployments", () => {
  const target = topology({ reachability_state: "unknown" })
  target.deployments.push(topology({ id: "optional", kind: "self_hosted", release_gate: "optional" }).deployments[0])
  const choices = gatewayPublicationChoices(profiles, { items: [target] }, true)
  assert.equal(choices.length, 2)
  assert.ok(choices.every(choice => !choice.selectable && choice.reason === "Required Gateway is not ready"))
})

test("an explicit invalid selection never jumps to another APIM or deployment", () => {
  const choices = gatewayPublicationChoices(profiles, { items: [topology()] }, true)
  assert.equal(resolveGatewaySelection(profiles, choices, { targetId: "missing", deploymentId: null }).choice, undefined)
  assert.equal(resolveGatewaySelection(profiles, choices, { targetId: "first", deploymentId: "removed" }).choice, undefined)
  assert.equal(resolveGatewaySelection(profiles, choices, { targetId: null, deploymentId: null }).choice.deploymentId, "managed")
})

test("only a missing topology API enables legacy fallback", async () => {
  assert.equal(await apiHarness(async () => { throw new ApiError(404) }).apimTargets(), null)
  for (const status of [401, 403, 500, 503]) {
    await assert.rejects(apiHarness(async () => { throw new ApiError(status) }).apimTargets(), error => error.status === status)
  }
})

test("registration and sync clients use the exact public routes", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return { operation: { id: "queued" } } })
  const payload = { name: "Unit", tenant_id: "unit-tenant", api_id: "unit-api" }
  await api.registerApimTarget(payload)
  await api.syncApimTarget("first/second")
  assert.equal(calls[0][0], "/api/v1/model-management/apim-targets")
  assert.equal(calls[0][1], payload)
  assert.equal(calls[1][0], "/api/v1/model-management/apim-targets/first%2Fsecond/inventory-sync")
  assert.equal(calls[1][1].method, "POST")
})

test("a late topology read cannot replace a confirmed target write", async () => {
  const { finopsKeys, storeApimTarget } = queryHarness()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const previous = { items: [topology()], total: 1, inventory_sync_available: true }
  client.setQueryData(finopsKeys.apimTargets, previous)
  let release
  const pending = client.fetchQuery({ queryKey: finopsKeys.apimTargets, queryFn: () => new Promise(resolve => { release = resolve }) }).catch(() => {})
  const saved = topology()
  saved.target.service_name = "confirmed-service"
  await storeApimTarget(client, saved)
  release(previous)
  await pending
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].target.service_name, "confirmed-service")
  assert.equal(client.getQueryData(finopsKeys.apimTargets).total, 1)
  client.clear()
})

test("topology component has scoped expansion, evidence labels and Owner-only sync", () => {
  const target = { ...topology(), published_model_count: 0, self_hosted_gateway_count: 0 }
  const harness = componentHarness("GatewayTargetGroup", { target, profile: profiles[0], canManage: false, syncAvailable: true })
  assert.equal(harness.all().filter(node => node.type === "article").length, 1)
  assert.equal(harness.find(node => node.props?.["aria-label"] === "Synchronize inventory"), undefined)
  harness.find(node => node.props?.["aria-expanded"] === true).props.onClick()
  assert.equal(harness.all().filter(node => node.type === "article").length, 0)
  harness.props.canManage = true
  const sync = harness.find(node => node.props?.["aria-label"] === "Synchronize inventory")
  assert.equal(sync.props.disabled, false)
  sync.props.onClick()
  assert.equal(harness.syncCalls.length, 1)
  assert.equal(harness.find(node => node.props?.["aria-label"] === "Synchronize inventory").props.disabled, true)
})

test("registration blocks duplicate submit and close until the same promise settles", async () => {
  let finish
  const saved = []
  let closes = 0
  const harness = componentHarness("GatewayRegistrationDialog", {
    busy: false, error: null, onClose() { closes++ },
    onSave(value) { saved.push(value); return new Promise(resolve => { finish = resolve }) },
  })
  assert.equal(harness.find(node => node.type === "DialogHeader").props.className, "registry-editor-header")
  assert.equal(harness.find(node => node.type === "DialogFooter").props.className, "registry-editor-footer")
  assert.equal(harness.all().filter(node => node.props?.className === "registry-field-label").length, 10)
  for (const input of harness.all().filter(node => node.type === "Input")) input.props.onChange({ target: { value: ` ${input.props.name} ` } })
  const submit = harness.find(node => node.type === "form").props.onSubmit
  const pending = submit({ preventDefault() {} })
  await submit({ preventDefault() {} })
  harness.render().props.onOpenChange(false)
  assert.equal(saved.length, 1)
  assert.equal(saved[0].name, "name")
  assert.equal(closes, 0)
  finish()
  await pending
  harness.render().props.onOpenChange(false)
  assert.equal(closes, 1)
})

test("registration failure retains fields and disables every input while busy", async () => {
  const harness = componentHarness("GatewayRegistrationDialog", {
    busy: false, error: null, onClose() {}, onSave: async () => { throw new Error("dependency unavailable") },
  })
  for (const input of harness.all().filter(node => node.type === "Input")) input.props.onChange({ target: { value: input.props.name } })
  await harness.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  harness.props.error = "Registration validation failed"
  assert.equal(harness.find(node => node.props?.name === "name").props.value, "name")
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, harness.props.error)
  harness.props.busy = true
  assert.ok(harness.all().filter(node => node.type === "Input").every(node => node.props.disabled))
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("Gateway details and sync actions remain visible under narrow table rules", () => {
  const css = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  let display
  css.walkRules(".gateway-target-group .model-row-actions > button:not(.toggle)", rule => {
    rule.walkDecls("display", declaration => { display = declaration.value })
  })
  assert.equal(display, "grid")
})