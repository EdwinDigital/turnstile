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
const onboardingCode = await compile("../../../frontend/src/components/model-management/apim-onboarding-dialog.tsx")

function componentHarness(exportName, props, { operation = null, mutationPending = false, code = componentCode, api = {} } = {}) {
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
      useMutation: options => {
        const index = cursor++
        if (!(index in slots)) slots[index] = { pending: false, error: null }
        const state = slots[index]
        return { isPending: mutationPending || state.pending, error: state.error,
          reset() { state.error = null },
          mutate: () => { syncCalls.push(options); options.onSuccess?.({ operation: { id: "unit-operation" } }) },
          async mutateAsync(value) {
            state.pending = true
            try {
              const result = await options.mutationFn(value)
              await options.onSuccess?.(result, value)
              return result
            } catch (error) { state.error = error; throw error }
            finally { state.pending = false; await options.onSettled?.() }
          },
        }
      },
    },
    "../../data-sources/apim/api": { dataSource: api },
    "../../data-sources/apim/queries": {
      finopsKeys: { apimTargets: ["targets"], gatewayReleaseOperation: id => ["operation", id] },
      finopsQueries: {
        gatewayReleaseOperation: id => ({ queryKey: ["operation", id] }),
        selfHostedGatewayRemoval: id => ({ queryKey: ["removal", id] }),
      },
      storeGatewayDeployment: async () => {},
      storeGatewayDeploymentName: async () => {},
    },
    "../../lib/gateway-topology": gatewayTopology,
  }
  const exports = {}
  runInNewContext(code, { exports, Set, URL, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
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

test("initialization clients use exact URLs and preserve authorization errors", async () => {
  assert.equal(await apiHarness(async () => { throw new ApiError(404) }).apimOnboardingOperations(), null)
  for (const status of [401, 403, 500, 503]) {
    await assert.rejects(apiHarness(async () => { throw new ApiError(status) }).apimOnboardingOperations(), error => error.status === status)
  }
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  const request = { tenant_id: "unit-tenant", resource_id: "unit-resource" }
  const start = { request, confirmation_sha256: "a".repeat(64) }
  await api.previewApimOnboarding(request)
  await api.startApimOnboarding(start)
  await api.retryApimOnboarding("first/second")
  assert.equal(calls[0][0], "/api/v1/model-management/apim-onboarding/preview")
  assert.equal(calls[0][1], request)
  assert.equal(calls[1][0], "/api/v1/model-management/apim-onboarding")
  assert.equal(calls[1][1], start)
  assert.equal(calls[2][0], "/api/v1/model-management/apim-onboarding/first%2Fsecond/retry")
  assert.equal(calls[2][1].method, "POST")
})

test("initialization polls only active tasks and replaces stale attempts after accepted retry", async () => {
  const { finopsKeys, finopsQueries, storeApimOnboarding } = queryHarness()
  const options = finopsQueries.apimOnboarding()
  for (const status of ["succeeded", "failed", "restored"]) {
    assert.equal(options.refetchInterval({ state: { data: { items: [{ status }] } } }), false)
  }
  assert.equal(options.refetchInterval({ state: { data: { items: [{ status: "queued" }] } } }), 2000)
  assert.equal(options.retry, false)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const other = { id: "other", gateway_profile_id: "other-target", status: "failed" }
  const previous = { available: true, items: [{ id: "old", gateway_profile_id: "first", status: "failed" }, other] }
  client.setQueryData(finopsKeys.apimOnboarding, previous)
  let release
  const pending = client.fetchQuery({ queryKey: finopsKeys.apimOnboarding, queryFn: () => new Promise(resolve => { release = resolve }) }).catch(() => {})
  const accepted = { id: "new", gateway_profile_id: "first", status: "queued" }
  await storeApimOnboarding(client, accepted)
  release(previous)
  await pending
  const current = client.getQueryData(finopsKeys.apimOnboarding)
  assert.equal(current.available, true)
  assert.deepEqual(Array.from(current.items, item => item.id), ["new", "other"])
  assert.equal(client.getQueryData(finopsKeys.gatewayReleaseOperation("new")), accepted)
  client.clear()
})

function initializationPlan(ready = true) {
  return {
    request: { tenant_id: "00000000-0000-0000-0000-000000000012", resource_id: "/subscriptions/00000000-0000-0000-0000-000000000034/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit-service", name: "Unit APIM" },
    service_name: "unit-service", service_tier: "Developer", location: "unit-region",
    managed_gateway_origin: "https://unit-service.azure-api.net/", api_path: "turnstile/unit", api_id: "unit-api",
    dashboard_subscription_id: "unit-dashboard", probe_subscription_id: "unit-probe",
    initial_monthly_token_limit: 100000, initial_tokens_per_minute: 100000,
    steps: ["validate_target", "create_api"], blockers: ready ? [] : ["budget_execution_not_ready"],
    ready, confirmation_sha256: "a".repeat(64),
  }
}
function onboardingHarness(api, extra = {}) {
  const harness = componentHarness("ApimOnboardingDialog", {
    available: true, onClose() {}, onAccepted() {}, onSettled() {}, ...extra,
  }, { code: onboardingCode, api })
  for (const input of harness.all().filter(node => node.type === "Input")) {
    input.props.onChange({ target: { value: ` ${initializationPlan().request[input.props.name]} ` } })
  }
  return harness
}
const submitForm = harness => harness.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })

test("initialization uses the confirmed server plan and invalidates it on Back", async () => {
  const plan = initializationPlan()
  const calls = []
  const accepted = []
  const harness = onboardingHarness({
    previewApimOnboarding: async request => { calls.push(["preview", request]); return plan },
    startApimOnboarding: async request => { calls.push(["start", request]); return { operation: { id: "unit-operation" } } },
  }, { onAccepted: operation => accepted.push(operation) })
  await submitForm(harness)
  assert.equal(calls[0][1].name, "Unit APIM")
  assert.equal(calls[0][1].tenant_id, plan.request.tenant_id)
  assert.equal(harness.all().filter(node => node.type === "Input").length, 0)
  await submitForm(harness)
  assert.equal(calls[1][1].request, plan.request)
  assert.equal(calls[1][1].confirmation_sha256, plan.confirmation_sha256)
  assert.equal(accepted[0].id, "unit-operation")
  harness.find(node => node.type === "Button" && node.props.children?.includes?.("Back")).props.onClick()
  assert.equal(harness.find(node => node.props?.name === "name").props.value.trim(), "Unit APIM")
  await submitForm(harness)
  assert.equal(calls[2][0], "preview")
})

test("blocked initialization plans can recheck but never start", async () => {
  let checks = 0
  const harness = onboardingHarness({
    previewApimOnboarding: async () => { checks++; return initializationPlan(false) },
    startApimOnboarding: async () => { throw new Error("blocked plan must not start") },
  })
  await submitForm(harness)
  assert.ok(harness.find(node => node.props?.role === "alert"))
  await submitForm(harness)
  assert.equal(checks, 2)
})

test("initialization prevents duplicate submission and dismissal until preview settles", async () => {
  let finish
  let calls = 0
  let closes = 0
  const harness = onboardingHarness({ previewApimOnboarding: () => { calls++; return new Promise(resolve => { finish = resolve }) } }, { onClose() { closes++ } })
  const pending = submitForm(harness)
  await submitForm(harness)
  harness.render().props.onOpenChange(false)
  assert.equal(calls, 1)
  assert.equal(closes, 0)
  assert.ok(harness.all().filter(node => node.type === "Input").every(node => node.props.disabled))
  finish(initializationPlan())
  await pending
  harness.render().props.onOpenChange(false)
  assert.equal(closes, 1)
})

test("initialization error preserves the confirmed plan and disables unavailable execution", async () => {
  const harness = onboardingHarness({
    previewApimOnboarding: async () => initializationPlan(),
    startApimOnboarding: async () => { throw new Error("Preflight unavailable") },
  })
  await submitForm(harness)
  await submitForm(harness)
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Preflight unavailable")
  assert.ok(harness.find(node => node.props?.["aria-label"] === "Initialization plan"))
  harness.props.available = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("initialization progress retains failure, exact retry identity and completed steps", () => {
  const retries = []
  const operation = { id: "failed-task", semantic_preview: initializationPlan(), status: "failed", worker_available: true,
    checkpoint: { completed_steps: ["validate_target", "create_api"], retry_of: "previous-task" }, error_message: "Permission preflight failed" }
  const harness = componentHarness("ApimOnboardingRow", { operation, available: true, retryingId: null, onRetry: id => retries.push(id) }, { code: onboardingCode })
  assert.equal(harness.all().filter(node => node.props?.["data-complete"] === true).length, 2)
  harness.find(node => node.props?.["aria-label"] === "Retry initialization").props.onClick()
  assert.deepEqual(retries, ["failed-task"])
  harness.find(node => node.props?.["aria-label"] === "Initialization progress").props.onClick()
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Permission preflight failed")
  harness.props.retryingId = "another-task"
  assert.equal(harness.find(node => node.props?.["aria-label"] === "Retry initialization").props.disabled, true)
})

test("Self-hosted clients keep exact identity, minimal name payload and mutation methods", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  const payload = { native_gateway_id: "edge", display_name: "Edge", endpoint_url: "https://edge.example.test" }
  await api.addSelfHostedGateway("target/path", payload)
  await api.renameSelfHostedGateway("edge/path", "Renamed")
  await api.validateSelfHostedGateway("edge/path")
  await api.selfHostedGatewayRemovalPreview("edge/path")
  await api.removeSelfHostedGateway("edge/path")
  const change = { ...payload, apim_target_id: "destination" }
  await api.changeSelfHostedGatewayAssociation("edge/path", change)
  const root = "/api/v1/model-management/self-hosted-gateways/edge%2Fpath"
  assert.equal(calls[0][0], "/api/v1/model-management/apim-targets/target%2Fpath/self-hosted-gateways")
  assert.equal(calls[0][1], payload)
  assert.deepEqual(Object.keys(calls[1][1]), ["display_name"])
  assert.equal(calls[1][1].display_name, "Renamed")
  assert.equal(calls[1][2], "PATCH")
  assert.deepEqual(calls.slice(1).map(call => call[0]), [root + "/name", root + "/validations", root + "/removal-preview", root, root + "/apim-association"])
  assert.equal(calls[2][1].method, "POST")
  assert.equal(calls[4][1].method, "DELETE")
  assert.equal(calls[5][1], change)
  assert.equal(calls[5][2], "PUT")
})

test("confirmed Self-hosted names and retirements survive stale topology responses", async () => {
  const { finopsKeys, storeGatewayDeployment, storeGatewayDeploymentName } = queryHarness()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const previous = { items: [topology({ id: "edge", apim_target_id: "first", kind: "self_hosted", display_name: "Old" })] }
  client.setQueryData(finopsKeys.apimTargets, previous)
  let release
  const pending = client.fetchQuery({ queryKey: finopsKeys.apimTargets, queryFn: () => new Promise(resolve => { release = resolve }) }).catch(() => {})
  await storeGatewayDeploymentName(client, "edge", " Confirmed ")
  release(previous)
  await pending
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].deployments[0].deployment.display_name, "Confirmed")
  await storeGatewayDeployment(client, null, "edge")
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].deployments.length, 0)
  assert.equal(client.getQueryData(finopsKeys.apimTargets).items[0].self_hosted_gateway_count, 0)
  client.clear()
})

test("confirmed reassociation removes the old entry and adds only its exact destination", async () => {
  const { finopsKeys, storeGatewayDeployment, finopsQueries } = queryHarness()
  const client = new QueryClient()
  const first = topology({ id: "old-edge", apim_target_id: "first", kind: "self_hosted" })
  const second = { ...topology(), target: { ...topology().target, gateway_profile_id: "second" }, deployments: [] }
  client.setQueryData(finopsKeys.apimTargets, { items: [first, second] })
  const saved = { ...first.deployments[0], deployment: { ...first.deployments[0].deployment, id: "new-edge", apim_target_id: "second" } }
  await storeGatewayDeployment(client, saved, "old-edge")
  const items = client.getQueryData(finopsKeys.apimTargets).items
  assert.equal(items[0].deployments.length, 0)
  assert.equal(items[1].deployments[0].deployment.id, "new-edge")
  assert.equal(items[1].self_hosted_gateway_count, 1)
  assert.equal(finopsQueries.selfHostedGatewayRemoval(null).enabled, false)
  assert.equal(finopsQueries.selfHostedGatewayRemoval("edge").staleTime, 0)
  client.clear()
})

function selfHostedEntry() {
  const target = topology({ id: "edge", apim_target_id: "first", kind: "self_hosted", native_gateway_id: "edge", hosting_mode: "docker", display_name: "Edge", location: "Unit" })
  return { ...target.deployments[0], target }
}

test("Self-hosted editor validates HTTPS, preserves metadata and blocks duplicate submission", async () => {
  let finish
  const saves = []
  let closes = 0
  const target = topology()
  const harness = componentHarness("SelfHostedGatewayEditor", {
    targets: [target], initialTargetId: "first", available: true, busy: false, error: null,
    onClose() { closes++ }, onSave: (...args) => { saves.push(args); return new Promise(resolve => { finish = resolve }) },
  })
  for (const [name, value] of Object.entries({ display_name: " Edge ", native_gateway_id: " edge ", endpoint_url: "https://user:secret@edge.example.test", location: " Unit " })) {
    harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.props?.name === "endpoint_url").props.onChange({ target: { value: "https://edge.example.test/unit/llm" } })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, false)
  const pending = submitForm(harness)
  await submitForm(harness)
  harness.render().props.onOpenChange(false)
  assert.equal(saves.length, 1)
  assert.equal(saves[0][0], "first")
  assert.equal(saves[0][1].display_name, "Edge")
  assert.equal(saves[0][1].native_gateway_id, "edge")
  assert.equal(saves[0][1].release_gate, "required")
  assert.equal(closes, 0)
  finish()
  await pending
  harness.render().props.onOpenChange(false)
  assert.equal(closes, 1)
})

test("Self-hosted reassociation requires a fresh matching clear preview and another APIM", () => {
  const entry = selfHostedEntry()
  const other = { ...topology(), target: { ...topology().target, gateway_profile_id: "second", service_name: "Second" } }
  const harness = componentHarness("SelfHostedGatewayEditor", {
    targets: [entry.target, other], initialTargetId: "first", entry, available: true, busy: false, error: null,
    preview: { deployment_id: "edge", removal_allowed: true, blockers: [], models: [] }, loading: false,
    onClose() {}, onSave: async () => {},
  })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.type === "Select" && node.props.value === "first").props.onValueChange("second")
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, false)
  assert.ok(harness.find(node => node.props?.name === "native_gateway_id").props.readOnly)
  harness.props.preview = { ...harness.props.preview, deployment_id: "other-edge" }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.props.preview = { deployment_id: "edge", removal_allowed: false, blockers: ["Publication is pending"], models: [] }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  assert.ok(harness.find(node => node.props?.role === "alert"))
})

test("Gateway name dialog is name-only and retains draft after failure", async () => {
  const values = []
  const harness = componentHarness("GatewayDeploymentNameDialog", {
    entry: selfHostedEntry(), available: true, busy: false, error: null,
    onClose() {}, onSave: async name => { values.push(name); throw new Error("Unavailable") },
  })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.type === "Input").props.onChange({ target: { value: " Renamed " } })
  await submitForm(harness)
  assert.deepEqual(values, ["Renamed"])
  assert.equal(harness.find(node => node.type === "Input").props.value, " Renamed ")
  harness.props.available = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("Gateway retirement refuses loading, blocked and foreign previews and guards double confirmation", async () => {
  let finish
  let calls = 0
  const harness = componentHarness("GatewayRetirementDialog", {
    entry: selfHostedEntry(), preview: null, loading: true, busy: false, error: null, available: true,
    onClose() {}, onConfirm: () => { calls++; return new Promise(resolve => { finish = resolve }) },
  })
  const action = () => harness.find(node => node.type === "AlertDialogAction")
  assert.equal(harness.find(node => node.type === "AlertDialogFooter").props.className, "model-delete-footer")
  assert.ok(harness.find(node => node.type === "AlertDialogCancel"))
  assert.equal(action().props.disabled, true)
  harness.props.loading = false
  harness.props.preview = { deployment_id: "other", removal_allowed: true, models: [], blockers: [] }
  assert.equal(action().props.disabled, true)
  harness.props.preview.deployment_id = "edge"
  assert.equal(action().props.disabled, false)
  harness.props.preview.removal_allowed = false
  assert.equal(action().props.disabled, true)
  harness.props.preview.removal_allowed = true
  const pending = action().props.onClick()
  await action().props.onClick()
  assert.equal(calls, 1)
  finish()
  await pending
})

test("Gateway lifecycle menus require Owner and capability; Managed entries never get lifecycle actions", () => {
  const target = { ...topology(), published_model_count: 0, self_hosted_gateway_count: 1 }
  target.deployments.push(selfHostedEntry())
  const harness = componentHarness("GatewayTargetGroup", {
    target, profile: profiles[0], canManage: false, syncAvailable: true, lifecycleAvailable: true,
  })
  assert.equal(harness.all().filter(node => node.props?.["aria-label"] === "Gateway actions").length, 0)
  harness.props.canManage = true
  assert.equal(harness.all().filter(node => node.props?.["aria-label"] === "Gateway actions").length, 1)
  assert.equal(harness.all().filter(node => node.type === "DropdownMenuItem" && node.props.variant === "destructive").length, 1)
  harness.props.lifecycleAvailable = false
  assert.equal(harness.all().filter(node => node.props?.["aria-label"] === "Gateway actions").length, 0)
})

test("adding a Self-hosted Gateway uses the selected APIM and schedules one inventory sync", async () => {
  const target = { ...topology(), published_model_count: 0, self_hosted_gateway_count: 0 }
  const calls = []
  const harness = componentHarness("GatewayTargetGroup", {
    target, profile: profiles[0], canManage: true, syncAvailable: true, lifecycleAvailable: true,
  }, { api: { addSelfHostedGateway: async (...args) => { calls.push(args); return selfHostedEntry() } } })
  harness.find(node => node.type === "DropdownMenuItem" && node.props.children?.includes?.("Add Self-hosted Gateway")).props.onClick()
  const editor = harness.find(node => node.type?.name === "SelfHostedGatewayEditor")
  assert.ok(editor)
  const value = { native_gateway_id: "edge", display_name: "Edge", endpoint_url: "https://edge.example.test" }
  await editor.props.onSave("first", value)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], "first")
  assert.equal(calls[0][1], value)
  assert.equal(harness.find(node => node.type?.name === "SelfHostedGatewayEditor"), undefined)
  assert.equal(harness.syncCalls.length, 1)
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