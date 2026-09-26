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
import * as gatewayForms from "../../../frontend/src/lib/gateway-forms.ts"
import * as modelFilters from "../../../frontend/src/lib/model-inventory-filters.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const { QueryClient } = frontendRequire("@tanstack/react-query")
async function compile(path, expose = []) {
  const entry = fileURLToPath(new URL(path, import.meta.url))
  const bundle = await rolldown({ input: entry, external: path => path !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false,
    plugins: expose.length ? [{ name: "test-component-exports", load(path) { if (path === entry) return `${readFileSync(entry, "utf8")}\nexport { ${expose.join(", ")} }` } }] : [],
  })
  try { return (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
}
const apiCode = await compile("../../../frontend/src/data-sources/apim/api.ts")
const queryCode = await compile("../../../frontend/src/data-sources/apim/queries.ts")
const onboardingCode = await compile("../../../frontend/src/components/model-management/apim-onboarding-dialog.tsx")
const creationCode = await compile("../../../frontend/src/components/model-management/self-hosted-gateway-dialog.tsx")
const workspaceCode = await compile("../../../frontend/src/components/model-management/apim-gateway-workspace.tsx", ["ApimTable", "GatewayTable", "GatewayChildRow", "InfrastructureActions", "ApimEditor", "GatewayEditor", "GatewayAssociationEditor", "InfrastructureNameEditor", "RemovalDialog", "InfrastructureTableLoading"])
const pageCode = await compile("../../../frontend/src/pages/model-management-page.tsx", ["ModelWorkspace", "modelManagementTabFromUrl"])

test("new Gateway form generates no native identity and requires endpoint control confirmation", () => {
  const form = { display_name: "  New Docker  ", endpoint_url: "https://edge.test/unit/llm", location: " ", endpoint_control_confirmed: false }
  assert.equal(gatewayForms.gatewayCreateFormValid(form), false)
  form.endpoint_control_confirmed = true
  assert.deepEqual(gatewayForms.gatewayCreateFormPayload(form), {
    display_name: "New Docker", endpoint_url: form.endpoint_url, location: null,
    hosting_mode: "docker", release_gate: "required", endpoint_control_confirmed: true,
  })
  assert.equal(gatewayForms.gatewayCreateFormValid({ ...form, endpoint_url: "http://edge.test" }), false)
  assert.equal(gatewayForms.gatewayCreateFormValid({ ...form, display_name: " " }), false)
  assert.equal("native_gateway_id" in gatewayForms.gatewayCreateFormPayload({ ...form, native_gateway_id: "injected" }), false)
})

test("Gateway creation uses an idempotency header and bootstrap is never cacheable", async () => {
  const requests = []
  const api = apiHarness(async (...args) => { requests.push(args); return {} })
  const payload = { display_name: "New Docker", endpoint_url: "https://edge.test", endpoint_control_confirmed: true }
  await api.createSelfHostedGateway("target", payload, "request-uuid")
  assert.equal(requests[0][0], "/api/v1/model-management/apim-targets/target/self-hosted-gateway-provisions")
  assert.equal(requests[0][1].headers["Idempotency-Key"], "request-uuid")
  assert.deepEqual(JSON.parse(requests[0][1].body), payload)
  await api.selfHostedGatewayBootstrap("deployment", 24)
  assert.equal(requests[1][1].cache, "no-store")
  assert.deepEqual(JSON.parse(requests[1][1].body), { expires_in_hours: 24 })
})

function componentHarness(exportName, props, { operation = null, mutationPending = false, code = workspaceCode, api = {}, role = "owner", globals = {} } = {}) {
  const slots = []
  let cursor = 0
  const effects = []
  const syncCalls = []
  const pendingMutations = []
  const mutationVariables = []
  const queries = []
  const invalidations = []
  const queryClient = { setQueryData() {}, getQueriesData() { return [] }, invalidateQueries(options) { invalidations.push(options.queryKey); return Promise.resolve() } }
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
      useQuery: options => { queries.push(options); return { data: operation, error: null, refetch() {} } },
      useMutation: options => {
        const index = cursor++
        if (!(index in slots)) slots[index] = { pending: false, error: null }
        const state = slots[index]
        const mutateAsync = async value => {
            state.pending = true
            state.variables = value
            mutationVariables.push(value)
            try {
              const result = await options.mutationFn(value, { client: queryClient })
              await options.onSuccess?.(result, value)
              return result
            } catch (error) { state.error = error; throw error }
            finally { state.pending = false; await options.onSettled?.() }
        }
        return { isPending: mutationPending || state.pending, error: state.error, variables: state.variables,
          reset() { state.error = null }, mutateAsync,
          mutate: value => {
            syncCalls.push(options)
            const pending = mutateAsync(value)
            pendingMutations.push(pending)
            void pending.catch(() => {})
          },
        }
      },
    },
    "../../data-sources/apim/api": { dataSource: api },
    "../../data-sources/apim/queries": {
      finopsKeys: { registry: ["registry"], apimTargets: ["targets"], apimOnboarding: ["onboarding"], selfHostedGatewayProvisions: ["provisions"], gatewayReleaseOperation: id => ["operation", id] },
      finopsQueries: {
        gatewayReleaseOperation: id => ({ queryKey: ["operation", id] }),
        apimOnboarding: () => ({ queryKey: ["onboarding"] }),
        selfHostedGatewayProvisions: () => ({ queryKey: ["provisions"] }),
        selfHostedGatewayRemoval: id => ({ queryKey: ["removal", id] }),
      },
      storeGatewayDeployment: async () => {},
      storeGatewayDeploymentName: async () => {},
      storeApimTarget: async () => {},
    },
    "../../lib/gateway-topology": gatewayTopology,
    "../../lib/gateway-forms": gatewayForms,
    "../lib/model-inventory-filters": modelFilters,
    "../hooks/use-mobile": { useIsMobile: () => false },
    "../../providers/auth-provider": { useAuth: () => ({ user: { role } }) },
    "../../locales": { useLocale: () => ({ locale: "en-US" }) },
    "../../providers/timezone-provider": { useTimezone: () => ({ timezone: "UTC" }) },
    "../components/brand-logos": new Proxy({ providerBrandFromIdentity: () => "generic", gatewayBrandFromIdentity: () => "generic" }, { get: (target, name) => target[name] ?? name }),
  }
  const onboardingExports = {}
  runInNewContext(onboardingCode, { exports: onboardingExports,
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }),
  })
  imports["./apim-onboarding-dialog"] = onboardingExports
  const creationExports = {}
  runInNewContext(creationCode, { exports: creationExports, crypto: { randomUUID: () => "unit-request-id" }, AbortController, URL, Blob,
    require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }),
  })
  imports["./self-hosted-gateway-dialog"] = creationExports
  const exports = {}
  runInNewContext(code, { exports, Set, URL, Blob, AbortController, Error, crypto: { randomUUID: () => "unit-request-id" }, window: { location: { href: "http://localhost/?tab=apim" } }, ...globals,
    require: name => imports[name] ?? (name.endsWith("/gateway-topology") ? gatewayTopology : new Proxy({}, { get: (_target, key) => key })),
  })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const render = () => { cursor = 0; queries.length = 0; return exports[exportName](props) }
  const settle = async () => {
    let settled = 0
    while (settled < pendingMutations.length) {
      const current = pendingMutations.slice(settled)
      settled += current.length
      await Promise.allSettled(current)
    }
  }
  return { props, syncCalls, effects, mutationVariables, queries, invalidations, nodes, render, settle, all: () => nodes(render()), find: predicate => nodes(render()).find(predicate) }
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

test("private design has four ordered tabs and a separate self-hosted inventory", () => {
  assert.deepEqual(gatewayTopology.MODEL_MANAGEMENT_TABS, ["models", "connections", "gateways", "apim"])
  const target = topology()
  target.deployments.push(topology({ id: "edge", native_gateway_id: "edge", kind: "self_hosted", endpoint_url: null, declared_state: "unknown" }).deployments[0])
  target.deployments.push(topology({ id: "retired", kind: "self_hosted", retired_at: "2026-09-23T00:00:00Z" }).deployments[0])
  assert.deepEqual(gatewayTopology.gatewayEntries({ items: [target] }).map(entry => entry.deployment.id), ["edge"])
  assert.deepEqual(gatewayTopology.gatewayEntries(null), [])
})

test("registry preserves strict backend topology and self-hosted capability flags", async () => {
  for (const enabled of [true, false, undefined, "true"]) {
    const registry = await apiHarness(async () => ({ providers: [], gateways: [], runtimes: [], models: [],
      apim_target_topology_available: enabled, self_hosted_gateway_enabled: enabled,
    })).registry()
    assert.equal(registry.apim_target_topology_available, enabled === true)
    assert.equal(registry.self_hosted_gateway_enabled, enabled === true)
  }
})

for (const mode of ["gateways", "apim"]) test(`source workspace renders four real tabs in ${mode} view`, () => {
  const changes = []
  const target = topology()
  target.deployments.push(topology({ id: "edge", kind: "self_hosted" }).deployments[0])
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode, registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true },
    topology: { items: [target], total: 1, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange: value => changes.push(value), onToggleSidebar() {},
  }, { code: workspaceCode })
  const tabs = harness.all().filter(node => node.props?.["data-tab"])
  assert.deepEqual(tabs.map(node => node.props["data-tab"]), ["models", "connections", "gateways", "apim"])
  assert.equal(tabs.at(-1).props.children[0], "APIM 实例")
  tabs.at(-1).props.onClick()
  assert.deepEqual(changes, ["apim"])
  const table = harness.find(node => node.type?.name === (mode === "apim" ? "ApimTable" : "GatewayTable"))
  assert.ok(table)
  if (mode === "gateways") assert.deepEqual(table.props.entries.map(entry => entry.deployment.id), ["edge"])
  assert.equal(harness.find(node => node.type === "ManagementCredentialDialog"), undefined)
})

test("the page routes apim URLs and preserves source three-tab compatibility for old backends", () => {
  assert.equal(componentHarness("modelManagementTabFromUrl", {}, { code: pageCode }).render(), "apim")
  const harness = componentHarness("ModelWorkspace", {
    registry: { models: [], providers: [], runtimes: [], gateways: [] }, topology: { items: [], total: 0 },
    topologyAvailable: true, tab: "models", publications: [], loadingRuntimeHealthIds: new Set(),
  }, { code: pageCode })
  assert.deepEqual(harness.all().filter(node => node.props?.["data-tab"]).map(node => node.props["data-tab"]), ["models", "connections", "gateways", "apim"])
  harness.props.topologyAvailable = false
  assert.deepEqual(harness.all().filter(node => node.props?.["data-tab"]).map(node => node.props["data-tab"]), ["models", "connections", "gateways"])
})

test("model workspace applies filters and search to models, pending rows, empty states and totals", () => {
  const registry = {
    gateways: profiles,
    providers: [{ id: "provider", name: "Foundry", brand_key: "microsoft_foundry", enabled: true }],
    runtimes: [{ id: "connection", name: "Connection", provider_id: "provider", gateway_profile_id: "first", brand_key: "microsoft_foundry", runtime_kind: "foundry", enabled: true, health_status: "unknown", config: {} }],
    models: [
      { id: "alpha", model_key: "gpt-alpha", display_name: "Alpha", runtime_id: "connection", provider_id: "provider", family_key: "openai", capabilities: ["chat"], enabled: true },
      { id: "beta", model_key: "claude-beta", display_name: "Beta", runtime_id: "connection", provider_id: "provider", family_key: "claude", capabilities: ["chat"], enabled: false },
    ],
  }
  const harness = componentHarness("ModelWorkspace", {
    registry, topology: { items: [], total: 0 }, topologyAvailable: true, tab: "models",
    publications: [{ id: "pending", model_key: "deepseek-pending", display_name: "DeepSeek pending", publication_kind: "model_add", status: "queued", updated_at: "2026-01-01T00:00:00Z" }],
    loadingRuntimeHealthIds: new Set(),
  }, { code: pageCode })
  const visible = () => harness.all().filter(node => ["ModelRow", "PublicationModelRow"].includes(node.type?.name))
    .map(node => node.props.model?.model_key ?? node.props.publication.model_key)
  const apply = value => harness.find(node => node.type === "ModelInventoryFilterMenu").props.onChange(value)
  const search = value => harness.find(node => node.type === "ExpandableSearch").props.onChange(value)
  assert.deepEqual(visible(), ["gpt-alpha", "claude-beta", "deepseek-pending"])
  apply({ ...modelFilters.emptyModelFilters(), family: ["deepseek"] })
  assert.deepEqual(visible(), ["deepseek-pending"])
  assert.equal(harness.find(node => node.props?.className === "smh-empty"), undefined)
  assert.equal(harness.find(node => node.type === "footer").props.children[1], 1)
  apply({ ...modelFilters.emptyModelFilters(), status: ["enabled"], capability: ["chat"] })
  assert.deepEqual(visible(), ["gpt-alpha"])
  search(" beta ")
  assert.deepEqual(visible(), [])
  assert.ok(harness.find(node => node.props?.className === "smh-empty"))
  assert.equal(harness.find(node => node.type === "footer").props.children[1], 0)
  apply(modelFilters.emptyModelFilters())
  assert.deepEqual(visible(), ["claude-beta"])
  search("")
  assert.deepEqual(visible(), ["gpt-alpha", "claude-beta", "deepseek-pending"])
})

test("source initialization queries honor capability presence and active-only 1.5-second polling", () => {
  const props = {
    mode: "apim", registry: { models: [], runtimes: [], gateways: [] },
    topology: { items: [], total: 0, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }
  const harness = componentHarness("ApimGatewayWorkspace", props)
  const query = () => { harness.render(); return harness.queries.find(options => options.queryKey[0] === "onboarding") }
  assert.equal(query().enabled, false)
  props.topology.onboarding_available = false
  assert.equal(query().enabled, true)
  assert.equal(query().refetchInterval({ state: { data: { items: [{ status: "queued" }] } } }), 1500)
  assert.equal(query().refetchInterval({ state: { data: { items: [{ status: "failed" }, { status: "succeeded" }] } } }), false)
  props.mode = "gateways"
  assert.equal(query().enabled, false)
  props.mode = "apim"
  const member = componentHarness("ApimGatewayWorkspace", props, { role: "member" })
  member.render()
  assert.equal(member.queries.find(options => options.queryKey[0] === "onboarding").enabled, false)
})

test("source APIM collapse keeps its children and search preserves the total tab count", () => {
  const target = topology()
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode: "apim", registry: { models: [], runtimes: [], gateways: profiles },
    topology: { items: [target], total: 1, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  })
  const table = () => harness.find(node => node.type?.name === "ApimTable")
  table().props.onToggleTarget("first")
  const collapsed = componentHarness("ApimTable", table().props)
  assert.equal(collapsed.find(node => node.props?.id === "apim-gateways-first").props.hidden, true)
  assert.equal(collapsed.all().filter(node => node.type?.name === "GatewayChildRow").length, 1)
  table().props.onToggleTarget("first")
  assert.equal(componentHarness("ApimTable", table().props).find(node => node.props?.id === "apim-gateways-first").props.hidden, false)
  harness.find(node => node.type === "ExpandableSearch").props.onChange("does-not-exist")
  assert.equal(table().props.targets.length, 0)
  assert.equal(table().props.filtered, true)
  assert.equal(harness.find(node => node.props?.["data-tab"] === "apim").props.children[1].props.children, 1)
})

for (const syncAvailable of [false, true]) test(`source attachment refreshes topology and respects inventory sync availability ${syncAvailable}`, async () => {
  const calls = []
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode: "apim", registry: { models: [], runtimes: [], gateways: profiles, self_hosted_gateway_enabled: true },
    topology: { items: [topology()], total: 1, resource_lifecycle_available: true, inventory_sync_available: syncAvailable },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }, { api: {
    addSelfHostedGateway: async (targetId, value) => calls.push(["attach", targetId, value]),
    syncApimTarget: async targetId => { calls.push(["sync", targetId]); return { operation: { id: "sync-id", status: "queued" } } },
  } })
  const table = () => harness.find(node => node.type?.name === "ApimTable")
  table().props.onToggleTarget("first")
  table().props.onAttach("first")
  const create = harness.find(node => node.type?.name === "SelfHostedGatewayCreateDialog")
  assert.ok(create)
  assert.equal(harness.find(node => node.type?.name === "GatewayEditor"), undefined)
  create.props.onAdopt("first")
  const editor = harness.find(node => node.type?.name === "GatewayEditor")
  editor.props.onSubmit("first", { native_gateway_id: "edge" })
  await harness.settle()
  assert.deepEqual(calls.map(call => call[0]), syncAvailable ? ["attach", "sync"] : ["attach"])
  assert.ok(calls.every(call => call[1] === "first"))
  assert.equal(table().props.collapsedTargetIds.has("first"), false)
  assert.equal(harness.find(node => node.type?.name === "GatewayEditor"), undefined)
  assert.deepEqual(harness.invalidations, [["registry"], ["targets"]])
})

test("source failed connection validation refreshes persisted evidence", async () => {
  const harness = componentHarness("ApimGatewayWorkspace", {
    mode: "gateways", registry: { models: [], runtimes: [], gateways: profiles },
    topology: { items: [topology({ id: "edge", kind: "self_hosted" })], total: 1, resource_lifecycle_available: true },
    loading: false, error: null, onTabChange() {}, onToggleSidebar() {},
  }, { api: { validateSelfHostedGateway: async () => { throw new Error("Probe failed") } } })
  harness.find(node => node.type?.name === "GatewayTable").props.onValidate("edge")
  await harness.settle()
  assert.deepEqual(harness.invalidations, [["registry"], ["targets"]])
  assert.ok(harness.find(node => node.type?.name === "InfrastructureErrorNotice"))
})

test("source association preserves Gateway identity and blocks published or unknown model dependencies", async () => {
  const target = topology({ id: "edge", kind: "self_hosted", hosting_mode: "kubernetes" })
  const destination = topology()
  destination.target.gateway_profile_id = "second"
  destination.configuration_endpoint = "https://second.configuration.example.test"
  const saved = []
  const props = {
    entry: { ...target.deployments[0], target }, targets: [target, destination],
    canChangeAssociation: true, lifecycleAvailable: true, busy: false, error: null,
    onClose() {}, onSubmit: value => saved.push(value),
  }
  const harness = componentHarness("GatewayAssociationEditor", props)
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.type?.name === "TargetSelect").props.onChange("second")
  harness.find(node => node.props?.type === "url").props.onChange({ target: { value: " https://second.example.test/api " } })
  await submitForm(harness)
  assert.equal(saved[0].apim_target_id, "second")
  assert.equal(saved[0].native_gateway_id, target.deployments[0].deployment.native_gateway_id)
  assert.equal(saved[0].display_name, "Managed")
  assert.equal(saved[0].endpoint_url, "https://second.example.test/api")
  assert.equal(saved[0].hosting_mode, "kubernetes")
  for (const entry of [
    { ...props.entry, published_models: [{ id: "model", display_name: "Model", model_key: "model-key" }] },
    { ...props.entry, published_models_available: false },
  ]) {
    const blocked = componentHarness("GatewayAssociationEditor", { ...props, entry })
    assert.equal(blocked.find(node => node.props?.type === "submit"), undefined)
    assert.equal(blocked.find(node => node.type?.name === "TargetSelect").props.disabled, true)
    await submitForm(blocked)
  }
  assert.equal(saved.length, 1)
})

test("source loading and errors cannot appear as an empty successful inventory", () => {
  const props = {
    mode: "apim", registry: { models: [], runtimes: [], gateways: [] }, topology: null,
    loading: true, error: null, onTabChange() {}, onToggleSidebar() {},
  }
  const harness = componentHarness("ApimGatewayWorkspace", props)
  assert.ok(harness.find(node => node.type?.name === "InfrastructureTableLoading"))
  assert.equal(harness.find(node => node.type === "footer"), undefined)
  props.loading = false
  props.error = new Error("Inventory unavailable")
  assert.ok(harness.find(node => node.props?.role === "alert"))
  assert.equal(harness.find(node => node.type?.name === "ApimTable"), undefined)
  assert.equal(harness.find(node => node.type === "footer"), undefined)
  props.error = null
  props.topology = { items: [], total: 0 }
  assert.ok(harness.find(node => node.type?.name === "ApimTable"))
  assert.ok(harness.find(node => node.type === "footer"))
})

test("source Gateway attachment validates endpoints and keeps the selected APIM without a release-gate field", async () => {
  const saved = []
  const harness = componentHarness("GatewayEditor", {
    targets: [topology()], initialTargetId: "first", available: true, busy: false, error: null,
    onClose() {}, onSubmit: (...values) => saved.push(values),
  }, { code: workspaceCode })
  for (const [name, value] of Object.entries({ display_name: " Edge ", native_gateway_id: " edge ", endpoint_url: "https://edge.example.test/api?" })) {
    harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.props?.name === "endpoint_url").props.onChange({ target: { value: "https://edge.example.test/api" } })
  assert.equal(harness.find(node => node.type?.name === "TargetSelect").props.disabled, true)
  assert.equal(harness.find(node => node.props?.name === "release_gate"), undefined)
  await submitForm(harness)
  assert.equal(saved[0][0], "first")
  assert.equal(saved[0][1].display_name, "Edge")
  assert.equal(saved[0][1].hosting_mode, "kubernetes")
  assert.equal(saved[0][1].release_gate, "required")
  harness.props.available = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("source APIM adoption form validates identity without requesting a management credential", async () => {
  const saved = []
  const harness = componentHarness("ApimEditor", { busy: false, error: null, onClose() {}, onSubmit: value => saved.push(value) }, { code: workspaceCode })
  const subscription = "00000000-0000-0000-0000-000000000002"
  const values = { name: " Unit ", tenant_id: "00000000-0000-0000-0000-000000000003", subscription_id: subscription,
    resource_group: "unit", service_name: "unit-service", api_id: "unit-api", gateway_endpoint: "https://unit.example.test/api",
    management_endpoint: `https://management.azure.com/subscriptions/${subscription}/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit-service`,
  }
  for (const [name, value] of Object.entries(values)) harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, false)
  assert.equal(harness.find(node => node.props?.type === "password"), undefined)
  await submitForm(harness)
  assert.equal(saved[0].name, "Unit")
  assert.equal(saved[0].management_auth_profile_ref, "system_managed_identity")
  harness.find(node => node.props?.name === "service_name").props.onChange({ target: { value: "other" } })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("source name editor submits only a trimmed name and honors backend support", async () => {
  const saved = []
  const harness = componentHarness("InfrastructureNameEditor", {
    kind: "gateway", identity: "edge", name: "Edge", supported: true, busy: false, error: null, onClose() {}, onSubmit: value => saved.push(value),
  }, { code: workspaceCode })
  assert.equal(harness.all().filter(node => node.type === "Input").length, 1)
  harness.find(node => node.type === "Input").props.onChange({ target: { value: " Renamed " } })
  await submitForm(harness)
  assert.deepEqual(saved, ["Renamed"])
  harness.props.supported = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("source removal confirmation blocks loading or dependencies and retains the shared footer", () => {
  const harness = componentHarness("RemovalDialog", { target: { kind: "apim", id: "first", name: "First" }, preview: null,
    loading: true, busy: false, error: null, onClose() {}, onConfirm() {},
  }, { code: workspaceCode })
  const action = () => harness.find(node => node.type === "AlertDialogAction")
  assert.equal(action().props.disabled, true)
  assert.equal(harness.find(node => node.type === "AlertDialogFooter").props.className, "model-delete-footer")
  harness.props.loading = false
  harness.props.preview = { removal_allowed: false, blockers: ["Remove model connections first"], models: [] }
  assert.equal(action().props.disabled, true)
  harness.props.preview.removal_allowed = true
  assert.equal(action().props.disabled, false)
})

test("source Gateway actions are Owner-only and Managed children have no lifecycle controls", () => {
  const props = { name: "Edge", checkLabel: "验证连接", removeLabel: "移除", busy: false, checking: false, checkDisabled: false, removeDisabled: false,
    onCheck() {}, onEdit() {}, onBinding() {}, onRemove() {},
  }
  const member = componentHarness("InfrastructureActions", props, { code: workspaceCode, role: "member" })
  assert.equal(member.all().filter(node => node.type === "DropdownMenuItem").length, 0)
  const owner = componentHarness("InfrastructureActions", props, { code: workspaceCode })
  assert.equal(owner.render().props.className, "model-row-actions infrastructure-row-actions gateway-name-actions")
  assert.equal(owner.all().filter(node => node.type === "DropdownMenuItem").length, 4)
  const target = topology()
  const child = componentHarness("GatewayChildRow", { entry: { ...target.deployments[0], target }, busy: false, lifecycleAvailable: true }, { code: workspaceCode })
  assert.equal(child.find(node => node.type?.name === "InfrastructureActions"), undefined)
  assert.ok(child.find(node => node.type === "ManagedGatewayLogo"))
})

test("source Gateway validation dispatches directly from the existing Owner action", async () => {
  const calls = []
  const target = topology()
  const harness = componentHarness("ApimGatewayWorkspace", { mode: "gateways", registry: { gateways: profiles, runtimes: [], models: [] },
    topology: { items: [target], total: 1, resource_lifecycle_available: true }, loading: false, error: null,
    onTabChange() {}, onToggleSidebar() {},
  }, { code: workspaceCode, api: { async validateSelfHostedGateway(...values) { calls.push(values) } } })
  harness.find(node => node.type?.name === "GatewayTable").props.onValidate("edge")
  await harness.settle()
  assert.deepEqual(calls, [["edge"]])
  assert.equal(harness.find(node => node.type === "ManagementCredentialDialog"), undefined)
})

test("source loading state preserves the APIM parent and child hierarchy", () => {
  const apim = componentHarness("InfrastructureTableLoading", { mode: "apim" }, { code: workspaceCode })
  assert.ok(apim.find(node => node.props?.role === "rowgroup" && node.props?.["aria-busy"] === "true"))
  assert.equal(apim.all().filter(node => node.props?.role === "row").length, 2)
  const gateways = componentHarness("InfrastructureTableLoading", { mode: "gateways" }, { code: workspaceCode })
  assert.equal(gateways.all().filter(node => node.props?.role === "row").length, 1)
})

test("source mobile actions remain visible and initialization retry retains its dimensions", () => {
  const { parse } = frontendRequire("postcss")
  const css = parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const mobileRules = []
  const retry = new Map()
  css.walkRules(rule => {
    if (rule.parent.type === "atrule" && rule.parent.params === "(max-width: 760px)") mobileRules.push(rule)
    if (rule.selector === ".apim-onboarding-details .apim-onboarding-error > button.apim-onboarding-retry") {
      rule.walkDecls(declaration => retry.set(declaration.prop, declaration.value))
    }
  })
  assert.ok(mobileRules.some(rule => rule.selector === ".model-row-actions:not(.infrastructure-row-actions) > button:not(.toggle)"))
  assert.ok(!mobileRules.some(rule => rule.selector === ".model-row-actions > button:not(.toggle)"))
  const tabs = mobileRules.find(rule => rule.selector === ".model-workspace .model-tabs")
  assert.equal(tabs.nodes.find(declaration => declaration.prop === "width").value, "100%")
  assert.equal(tabs.nodes.find(declaration => declaration.prop === "overflow-x").value, "auto")
  assert.equal(retry.get("width"), "auto")
  assert.equal(retry.get("height"), "28px")
  assert.equal(retry.get("max-width"), "100%")
})

for (const [changes, label] of [
  [{ retired_at: "2026-09-23T00:00:00Z" }, "已退役"],
  [{ declared_state: "absent" }, "未发现"],
  [{ endpoint_url: null }, "未配置"],
  [{ declared_state: "unknown" }, "待同步"],
  [{ last_probe_at: null }, "待验证"],
  [{ reachability_state: "unreachable" }, "不可达"],
  [{}, "已验证"],
]) test(`Gateway evidence: ${label}`, () => {
  const target = topology(changes)
  assert.equal(gatewayEvidence({ ...target.deployments[0], target }).label, label)
})

test("declared self-hosted Gateway without API membership is not verified", () => {
  const target = topology({ kind: "self_hosted" })
  const entry = { ...target.deployments[0], inventory: { associated_api_ids: ["other-api"] }, target }
  assert.equal(gatewayEvidence(entry).label, "未关联")
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
  assert.ok(choices.every(choice => !choice.selectable && choice.reason === "Required Gateway 未就绪"))
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

test("source management clients use Owner session without extra credential parameters", async () => {
  const calls = []
  const api = apiHarness(async (...args) => { calls.push(args); return {} })
  const target = { name: "Unit APIM" }
  const preview = { tenant_id: "unit-tenant", resource_id: "unit-resource" }
  const start = { request: preview, confirmation_sha256: "a".repeat(64) }
  await api.registerApimTarget(target)
  await api.previewApimOnboarding(preview)
  await api.startApimOnboarding(start)
  await api.retryApimOnboarding("first/second")
  await api.validateSelfHostedGateway("first/second")
  assert.equal(calls.length, 5)
  for (const [path, init] of calls) {
    assert.ok(path.startsWith("/api/v1/model-management/"))
    assert.equal(init.headers?.Authorization, undefined)
  }
  assert.equal(calls[0][1], target)
  assert.equal(calls[1][1], preview)
  assert.equal(calls[2][1], start)
  assert.equal(calls[3][1].method, "POST")
  assert.equal(calls[4][1].method, "POST")
  assert.equal(calls[3][1].body, undefined)
  assert.equal(calls[4][1].body, undefined)
  assert.ok(calls[3][0].includes("first%2Fsecond"))
  assert.ok(calls[4][0].includes("first%2Fsecond"))
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

test("new Gateway dialog requires no native ID and retries an uncertain submission with identical identity", async () => {
  const calls = []
  const accepted = []
  const harness = componentHarness("SelfHostedGatewayCreateDialog", {
    targets: [topology()], initialTargetId: null, available: true,
    onClose() {}, onAdopt() {}, onAccepted: value => accepted.push(value), onSettled() {},
  }, { code: creationCode, api: { createSelfHostedGateway: async (...args) => {
    calls.push(args)
    if (calls.length === 1) throw new Error("Response lost")
    return { operation: { id: "created" } }
  } } })
  assert.equal(harness.find(node => node.props?.name === "native_gateway_id"), undefined)
  assert.equal(harness.find(node => node.type === "form").props["data-request-id"], "unit-request-id")
  for (const [name, value] of Object.entries({ display_name: "  Docker edge  ", endpoint_url: "https://edge.test/unit/llm" })) {
    harness.find(node => node.props?.name === name).props.onChange({ target: { value } })
  }
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  harness.find(node => node.type === "Checkbox").props.onCheckedChange(true)
  await submitForm(harness)
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Response lost")
  assert.ok(harness.all().filter(node => node.type === "Input").every(node => node.props.disabled))
  await submitForm(harness)
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[0], calls[1])
  assert.equal(calls[0][2], "unit-request-id")
  assert.equal(calls[0][1].display_name, "Docker edge")
  assert.equal(calls[0][1].hosting_mode, "docker")
  assert.equal("native_gateway_id" in calls[0][1], false)
  assert.equal(accepted[0].id, "created")
})

test("new Gateway creation capability fails closed and old association stays a separate action", async () => {
  let adopted = false
  const harness = componentHarness("SelfHostedGatewayCreateDialog", {
    targets: [topology()], initialTargetId: "first", available: false,
    onClose() {}, onAdopt: () => { adopted = true }, onAccepted() {}, onSettled() {},
  }, { code: creationCode })
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
  assert.ok(harness.find(node => node.props?.role === "status"))
  harness.find(node => node.type === "Button" && node.props.children?.includes?.("关联已有 Gateway")).props.onClick()
  assert.equal(adopted, true)
})

test("startup configuration stays outside query state and DOM, downloads then clears on close", async () => {
  const blobs = []
  const links = []
  const revoked = []
  let requestedSignal
  const harness = componentHarness("SelfHostedGatewayBootstrapDialog", {
    deploymentId: "edge", name: "Docker edge", onClose() {}, onValidate() {},
  }, { code: creationCode, api: { selfHostedGatewayBootstrap: async (id, hours, signal) => {
    assert.equal(id, "edge"); assert.equal(hours, 24); requestedSignal = signal
    return { native_gateway_id: "turnstile-generated", deployment_id: id,
      configuration_endpoint: "https://unit.configuration.azure-api.net", expires_at: "2026-10-01T00:00:00Z",
      image: "mcr.microsoft.com/azure-api-management/gateway:2.12.1", environment_file_name: "turnstile-generated.env",
      environment_file: "config.service.auth=GatewayKey confidential-bootstrap-token", docker_command: "docker run --env-file turnstile-generated.env official-image", renewal_command: "docker stop turnstile-generated && docker rm turnstile-generated && docker run --env-file turnstile-generated.env official-image" }
  } }, globals: {
    URL: { createObjectURL: blob => { blobs.push(blob); return "blob:unit" }, revokeObjectURL: url => revoked.push(url) },
    document: { createElement: () => { const link = { click() { links.push(this) } }; return link } },
  } })
  harness.render()
  const cleanup = harness.effects[0]()
  assert.equal(harness.mutationVariables.length, 0)
  assert.equal(harness.queries.length, 0)
  await harness.find(node => node.type === "Button" && node.props.children?.includes?.("获取启动配置")).props.onClick()
  assert.equal(requestedSignal.aborted, false)
  assert.equal(JSON.stringify(harness.render()).includes("confidential-bootstrap-token"), false)
  const selects = harness.all().filter(node => node.type === "Select")
  selects[1].props.onValueChange("renew")
  assert.ok(harness.find(node => node.type === "textarea").props.value.startsWith("docker stop turnstile-generated"))
  const download = harness.find(node => node.type === "Button" && node.props.children?.includes?.("下载环境文件"))
  download.props.onClick()
  assert.equal(await blobs[0].text(), "config.service.auth=GatewayKey confidential-bootstrap-token")
  assert.equal(links[0].download, "turnstile-generated.env")
  assert.deepEqual(revoked, ["blob:unit"])
  cleanup()
  download.props.onClick()
  assert.equal(blobs.length, 1)
})

test("newly created configuration is waiting for its container, never reported verified", () => {
  const target = topology({ id: "edge", native_gateway_id: "edge", kind: "self_hosted", provisioning_operation_id: "creation", last_probe_at: null, reachability_state: "unknown" })
  const evidence = gatewayEvidence(apimGatewayEntries(target)[0])
  assert.equal(evidence.state, "warning")
  assert.equal(evidence.label, "待启动")
})

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
  harness.find(node => node.type === "Button" && node.props.children?.includes?.("返回修改")).props.onClick()
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

test("initialization follows the source Owner flow without a management credential field", async () => {
  const calls = []
  const harness = onboardingHarness({
    previewApimOnboarding: async (value, credential) => { calls.push({ value, credential }); return initializationPlan() },
    startApimOnboarding: async (value, credential) => { calls.push({ value, credential }); return { operation: { id: "unit-operation" } } },
  })
  assert.equal(harness.find(node => node.type === "ManagementCredentialField"), undefined)
  await submitForm(harness)
  await submitForm(harness)
  assert.deepEqual(calls.map(call => call.credential), [undefined, undefined])
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
  assert.ok(harness.find(node => node.props?.["aria-label"] === "APIM 初始化计划"))
  harness.props.available = false
  assert.equal(harness.find(node => node.props?.type === "submit").props.disabled, true)
})

test("initialization progress retains failure, exact retry identity and completed steps", async () => {
  const retries = []
  const operation = { id: "failed-task", semantic_preview: initializationPlan(), status: "failed", worker_available: true,
    checkpoint: { completed_steps: ["validate_target", "create_api"], retry_of: "previous-task" }, error_message: "Permission preflight failed" }
  const harness = componentHarness("ApimOnboardingRow", { operation, available: true, retryingId: null, onRetry: id => { retries.push(id) } }, { code: onboardingCode })
  assert.equal(harness.all().filter(node => node.props?.["data-complete"] === true).length, 2)
  harness.find(node => node.props?.className === "apim-onboarding-retry").props.onClick()
  assert.deepEqual(retries, ["failed-task"])
  harness.find(node => node.props?.["aria-label"] === "收起初始化进度").props.onClick()
  assert.equal(harness.find(node => node.props?.role === "alert").props.children, "Permission preflight failed")
  harness.props.retryingId = "another-task"
  assert.equal(harness.find(node => node.props?.className === "apim-onboarding-retry").props.disabled, true)
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

test("multi-target clients preserve parent and child identity with exact public URLs", async () => {
  const calls = []
  const rollout = { id: "parent", status: "partial", logical_model_id: "logical-model", children: [
    { apim_target_id: "first", publication_id: "child-one", required: true, status: "succeeded" },
    { apim_target_id: "second", publication_id: "child-two", required: false, status: "failed" },
  ] }
  const response = { rollout, status_url: "/api/v1/model-management/rollouts/parent", publications: [
    { id: "child-one", gateway_profile_id: "first", status: "active" },
    { id: "child-two", gateway_profile_id: "second", status: "failed", retry_requires_credential: true },
  ] }
  const api = apiHarness(async (...args) => { calls.push(args); return args[0].endsWith("?limit=50") ? { items: [rollout] } : rollout },
    async (...args) => { calls.push(args); return response })
  const payload = { children: [{ publication: { gateway_profile_id: "first" } }, { publication: { gateway_profile_id: "second" }, required: false }] }
  const accepted = await api.publishMultiTargetModel(payload)
  assert.equal(calls[0][0], "/api/v1/model-management/rollouts")
  assert.equal(calls[0][1], payload)
  assert.equal(accepted.rollout, rollout)
  assert.equal(accepted.status_url, response.status_url)
  assert.deepEqual(Array.from(accepted.publications, item => item.id), ["child-one", "child-two"])
  assert.equal(accepted.publications[1].retry_requires_credential, true)
  assert.equal((await api.multiTargetRollouts()).items[0], rollout)
  assert.equal(await api.multiTargetRollout("parent/path"), rollout)
  assert.equal(calls[1][0], "/api/v1/model-management/rollouts?limit=50")
  assert.equal(calls[2][0], "/api/v1/model-management/rollouts/parent%2Fpath")
})

test("multi-target clients never turn authorization or availability errors into success", async () => {
  for (const status of [401, 403, 404, 409, 503]) {
    const api = apiHarness(async () => { throw new ApiError(status) })
    await assert.rejects(api.publishMultiTargetModel({ children: [] }), error => error.status === status)
    await assert.rejects(api.multiTargetRollout("parent"), error => error.status === status)
    await assert.rejects(api.multiTargetRollouts(), error => error.status === status)
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
