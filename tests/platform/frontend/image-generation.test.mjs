import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire, stripTypeScriptTypes } from "node:module"
import test from "node:test"
import { setImmediate as nextTurn } from "node:timers/promises"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import { generatedImageBlob, imageDownloadFilename } from "../../../frontend/src/data-sources/apim/image-generation.ts"
import * as gatewayTopology from "../../../frontend/src/lib/gateway-topology.ts"

const apiSource = readFileSync(new URL("../../../frontend/src/data-sources/apim/api.ts", import.meta.url), "utf8")
const normalizationStart = apiSource.indexOf("const LEGACY_CLOUD_CODE_PROVIDER_ID")
const normalizationEnd = apiSource.indexOf("const normalizeGatewayPublication", normalizationStart)
assert.ok(normalizationStart >= 0 && normalizationEnd > normalizationStart)
const normalizeRegistry = runInNewContext(`${stripTypeScriptTypes(apiSource.slice(normalizationStart, normalizationEnd))}\nnormalizeRegistry`)
const emptyRegistry = { gateways: [], providers: [], runtimes: [], models: [] }

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const invocationEntry = fileURLToPath(new URL("../../../frontend/src/data-sources/apim/pages/dashboard-invocation.tsx", import.meta.url))
const invocationBundle = await rolldown({ input: invocationEntry, external: id => id !== invocationEntry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let invocationComponent
try { invocationComponent = (await invocationBundle.generate({ format: "cjs" })).output[0].code }
finally { await invocationBundle.close() }

for (const imageMode of [false, true]) test(`invocation UI submits explicit Gateway identity and blocks stale selection, image=${imageMode}`, async () => {
  const registry = normalizeRegistry({ ...emptyRegistry, explicit_gateway_selection_supported: true, self_hosted_gateway_enabled: true,
    image_generation_supported: true, image_configuration_schema_version: 4,
    gateways: [{ id: "gateway", name: "Unit APIM", implementation: "apim", enabled: true }],
    runtimes: [{ id: "runtime", name: "Unit runtime", config: {} }],
    models: [{ id: "model", runtime_id: "runtime", model_key: "unit-model", display_name: "Unit model", enabled: true, capabilities: [imageMode ? "image_generation" : "chat"], image_profile: { version: 4 } }],
  })
  const topology = { items: [{ target: { gateway_profile_id: "gateway", service_name: "unit-apim", api_id: "unit-api", drift_state: "in_sync", capabilities: { response_observer: "unsupported" } },
    deployments: [{ deployment: { id: "edge", apim_target_id: "gateway", native_gateway_id: "native-edge", display_name: "Edge", kind: "self_hosted", declared_state: "present", endpoint_url: "https://edge.example.test", retired_at: null },
      inventory: { associated_api_ids: ["unit-api"] }, published_models: [{ id: "model" }] }],
  }] }
  const slots = [], requests = []
  let cursor = 0
  const imports = {
    react: { useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }] }, useEffect() {}, useMemo: callback => callback() },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@tanstack/react-query": { useQuery: options => ({ data: options.queryKey?.[0] === "targets" ? topology : registry }), useQueryClient: () => ({}) },
    "../../../providers/auth-provider": { useAuth: () => ({ user: { email: "unit@example.com", role: "member" } }) },
    "../../../lib/gateway-topology": gatewayTopology,
    "../queries": { finopsQueries: { registry: () => ({}), apimTargets: () => ({ queryKey: ["targets"] }) }, invalidateFinOps() {} },
    "../api": { dataSource: {
      invokeModel: async value => { requests.push(value); return { request_id: "unit-request", correlation_id: null, gateway: "apim", content: "OK", usage: null } },
      generateImage: async value => { requests.push(value); return { request_id: "unit-request" } },
    } },
    "./dashboard-shared": { PanelTitle: "PanelTitle", queryError: error => error.message, formatLatency: () => "1 ms" },
  }
  const exports = {}
  runInNewContext(invocationComponent, { exports, crypto: { randomUUID: () => "unit-run" }, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const entities = { organizations: [{ id: "org", name: "Org" }], departments: [{ id: "department", name: "Department" }], projects: [{ id: "project", name: "Project", parent_id: "department" }], agents: [{ id: "agent", name: "Agent", parent_id: "project" }], users: [] }
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
  const find = predicate => { cursor = 0; return nodes(exports.AgentInvocation({ entities })).find(predicate) }
  const submit = () => find(node => node.type === "button" && node.props.className === "primary-button")
  if (imageMode) find(node => node.type === "button" && node.props.children?.includes("文生图")).props.onClick()
  find(node => node.type === "Select" && node.props.value === "__default__").props.onValueChange("model:model")
  find(node => node.type === "Select" && node.props.value === "__default__").props.onValueChange("gateway")
  find(node => node.type === "textarea").props.onChange({ target: { value: "Unit prompt" } })
  if (!imageMode) {
    const limit = () => find(node => node.type === "Input" && node.props["aria-label"] === "最大输出 Tokens")
    limit().props.onChange({ target: { value: "0" } })
    assert.equal(submit().props.disabled, true)
    submit().props.onClick()
    assert.equal(requests.length, 0)
    limit().props.onChange({ target: { value: "64" } })
  }
  assert.equal(submit().props.disabled, false)
  submit().props.onClick()
  await nextTurn()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].gateway_profile_id, "gateway")
  assert.equal(requests[0].gateway_deployment_id, "edge")
  assert.equal(requests[0].runtime_id, undefined)
  assert.equal(requests[0].max_output_tokens, imageMode ? undefined : 64)
  assert.equal(requests[0].stream, false)
  topology.items[0].deployments[0].deployment.retired_at = "2026-01-01T00:00:00Z"
  assert.equal(submit().props.disabled, true)
  submit().props.onClick()
  registry.models[0].enabled = false
  assert.equal(submit().props.disabled, true)
  submit().props.onClick()
  assert.equal(requests.length, 1)
})

for (const apiFormat of ["openai_chat", "anthropic_messages", undefined]) {
  test(`text invocation preserves provider sampling defaults for ${apiFormat ?? "unspecified"}`, async () => {
    const registry = normalizeRegistry({
      ...emptyRegistry,
      runtimes: [{ id: "runtime", name: "Unit runtime", config: { api_format: apiFormat, supports_temperature: true } }],
      models: [{ id: "text", runtime_id: "runtime", model_key: "unit-text", display_name: "Unit text", enabled: true, capabilities: ["chat"] }],
    })
    const slots = []
    const requests = []
    let cursor = 0
    const imports = {
      react: {
        useState(initial) {
          const index = cursor++
          if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
          return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
        },
        useEffect() {}, useMemo: callback => callback(),
      },
      "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
      "@tanstack/react-query": { useQuery: options => ({ data: options.queryKey?.[0] === "targets" ? null : registry }), useQueryClient: () => ({}) },
      "../../../providers/auth-provider": { useAuth: () => ({ user: { email: "unit@example.com", name: "Unit", role: "member" } }) },
      "../queries": { finopsQueries: { registry: () => ({}), apimTargets: () => ({ queryKey: ["targets"] }) }, invalidateFinOps() {} },
      "../../../lib/gateway-topology": gatewayTopology,
      "../api": { dataSource: { invokeModel: async request => { requests.push(request); return { content: "OK" } } } },
      "./dashboard-shared": { PanelTitle: "PanelTitle", queryError: error => error.message },
    }
    const exports = {}
    runInNewContext(invocationComponent, { exports, crypto: { randomUUID: () => "unit-run" }, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
    const entities = { organizations: [{ id: "org", name: "Org" }], departments: [{ id: "department", name: "Department" }], projects: [{ id: "project", name: "Project", parent_id: "department" }], agents: [{ id: "agent", name: "Agent", parent_id: "project" }], users: [] }
    const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
    const render = () => { cursor = 0; return exports.AgentInvocation({ entities }) }
    const find = predicate => nodes(render()).find(predicate)
    find(node => node.type === "Select").props.onValueChange("model:text")
    find(node => node.type === "textarea").props.onChange({ target: { value: "Unit prompt" } })
    const submit = find(node => node.type === "button" && node.props.className === "primary-button")
    assert.equal(submit.props.disabled, false)
    submit.props.onClick()
    await nextTurn()
    assert.equal(requests.length, 1)
    assert.equal(requests[0].model_id, "text")
    assert.equal(requests[0].runtime_id, "runtime")
    assert.equal(requests[0].max_output_tokens, 1024)
    assert.equal(requests[0].stream, true)
    assert.equal(Object.hasOwn(requests[0], "temperature"), false)
  })
}

for (const [enabled, version, allowed, rejected] of [[true, 4, true, false], [true, 4, true, true], [false, 4, false, false], [undefined, 4, false, false], [true, 3, false, false], [true, undefined, false, false], [true, 5, false, false]]) {
  test(`invocation component consumes normalized image capability ${enabled}/${version}, rejected=${rejected}`, async () => {
    const registry = normalizeRegistry({
      ...emptyRegistry, image_generation_supported: enabled, image_configuration_schema_version: version,
      runtimes: [{ id: "runtime", name: "Unit runtime", config: {} }],
      models: [{ id: "image", runtime_id: "runtime", model_key: "unit-image", display_name: "Unit image", enabled: true, capabilities: ["image_generation"], image_profile: { version: 4 } }],
    })
    const slots = []
    let cursor = 0
    const requests = []
    const pendingRequest = Promise.withResolvers()
    const hooks = {
      useState(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
        return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
      },
      useEffect() {}, useMemo: callback => callback(),
    }
    const imports = {
      react: hooks, "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
      "@tanstack/react-query": { useQuery: options => ({ data: options.queryKey?.[0] === "targets" ? null : registry }), useQueryClient: () => ({}) },
      "../../../providers/auth-provider": { useAuth: () => ({ user: { email: "unit@example.com", name: "Unit", role: "member" } }) },
      "../queries": { finopsQueries: { registry: () => ({}), apimTargets: () => ({ queryKey: ["targets"] }) }, invalidateFinOps() {} },
      "../../../lib/gateway-topology": gatewayTopology,
      "../api": { dataSource: { generateImage: request => { requests.push(request); return pendingRequest.promise } } },
      "./dashboard-shared": { PanelTitle: "PanelTitle", queryError: error => error.message },
    }
    const exports = {}
    runInNewContext(invocationComponent, { exports, crypto: { randomUUID: () => "unit-run" }, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
    const entities = { organizations: [{ id: "org", name: "Org" }], departments: [{ id: "department", name: "Department" }], projects: [{ id: "project", name: "Project", parent_id: "department" }], agents: [{ id: "agent", name: "Agent", parent_id: "project" }], users: [] }
    const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === "object" ? [value, ...nodes(value.props?.children)] : []
    const render = () => { cursor = 0; return exports.AgentInvocation({ entities }) }
    const find = predicate => nodes(render()).find(predicate)
    const modeButtons = () => nodes(render()).filter(node => node.type === "button" && typeof node.props["aria-pressed"] === "boolean")
    const submit = () => find(node => node.type === "button" && node.props.className === "primary-button")
    assert.deepEqual(modeButtons().map(node => node.props.children.at(-1)), ["对话", "文生图"])
    assert.deepEqual(modeButtons().map(node => node.props["aria-pressed"]), [true, false])
    assert.equal(submit().props.children.at(-1), "发起调用")
    find(node => node.type === "button" && node.props.children?.includes("文生图")).props.onClick()
    assert.deepEqual(modeButtons().map(node => node.props["aria-pressed"]), [false, true])
    assert.equal(submit().props.children.at(-1), "生成图片")
    assert.equal(find(node => node.type === "SelectValue").props.children, "选择图片模型")
    assert.equal(find(node => node.type === "SelectTrigger").props.title, "选择图片模型")
    assert.equal(find(node => node.type === "SelectItem" && node.props.value === "__default__").props.children, "选择图片模型")
    assert.equal(Boolean(find(node => node.props?.role === "alert" && node.props.children === "后端尚未支持图像参数透传")), version !== 4)
    find(node => node.type === "Select").props.onValueChange("model:image")
    find(node => node.type === "textarea").props.onChange({ target: { value: "unit prompt" } })
    assert.equal(submit().props.disabled, !allowed)
    submit().props.onClick()
    assert.equal(find(node => node.type === "textarea").props.disabled, allowed)
    if (allowed) {
      assert.equal(submit().props.children.at(-1), "调用中...")
      assert.equal(submit().props.disabled, true)
      assert.equal(find(node => node.type === "Select").props.disabled, true)
      assert.equal(find(node => node.type === "ImageOptions").props.disabled, true)
      assert.ok(modeButtons().every(node => node.props.disabled))
      modeButtons()[0].props.onClick()
      assert.deepEqual(modeButtons().map(node => node.props["aria-pressed"]), [false, true])
      if (rejected) pendingRequest.reject(new Error("Unit image request failed"))
      else pendingRequest.resolve({ request_id: "unit-response" })
    }
    await nextTurn()
    assert.equal(requests.length, allowed ? 1 : 0)
    assert.equal(find(node => node.type === "textarea").props.disabled, false)
    assert.equal(submit().props.children.at(-1), "生成图片")
    if (allowed) {
      assert.equal(requests[0].model_id, "image")
      assert.equal(requests[0].metadata.user_id, "unit@example.com")
      assert.equal(requests[0].n, 1)
      assert.equal(requests[0].stream, false)
      assert.equal(Boolean(find(node => node.type === "ImageGenerationResult").props.result), !rejected)
      assert.equal(find(node => node.props?.role === "alert")?.props.children, rejected ? "Unit image request failed" : undefined)
      find(node => node.type === "button" && node.props.children?.includes("对话")).props.onClick()
      assert.equal(submit().props.children.at(-1), "发起调用")
      find(node => node.type === "button" && node.props.children?.includes("文生图")).props.onClick()
      assert.equal(find(node => node.type === "ImageGenerationResult").props.result, null)
      assert.equal(find(node => node.type === "textarea").props.value, "")
      assert.equal(find(node => node.type === "Select").props.value, "__default__")
      assert.equal(find(node => node.props?.role === "alert"), undefined)
    }
  })
}

for (const [locale, generate, placeholder] of [["en", "Generate image", "Select an image model"], ["ja", "画像を生成", "画像モデルを選択"], ["ko", "이미지 생성", "이미지 모델 선택"]]) {
  test(`image invocation labels retain their ${locale} translations`, async () => {
    const { IMAGE_GENERATION_PHRASES: phrases } = await import(new URL(`../../../frontend/src/locales/${locale}/phrases-image-generation.ts`, import.meta.url))
    assert.equal(phrases["生成图片"], generate)
    assert.equal(phrases["选择图片模型"], placeholder)
    assert.ok(phrases["后端尚未支持图像参数透传"])
  })
}

test("image result actions preserve compact circular ghost styling", async () => {
  const entry = fileURLToPath(new URL("../../../frontend/src/data-sources/apim/pages/image-generation-result.tsx", import.meta.url))
  const bundle = await rolldown({ input: entry, external: id => id !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
  let component
  try { component = (await bundle.generate({ format: "cjs" })).output[0].code }
  finally { await bundle.close() }
  const exports = {}
  const imports = {
    react: { useState: initial => [initial, () => {}], useEffect() {} },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "./dashboard-shared": { PanelTitle: "PanelTitle", formatLatency: () => "0ms" },
  }
  runInNewContext(component, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const rendered = exports.ImageGenerationResult({ result: null, pending: false, available: true })
  const actions = Array.from(rendered.props.children[0].props.action.props.children)
  assert.deepEqual(actions.map(action => action.props["aria-label"]), ["查看原图", "下载图片"])
  for (const action of actions) {
    assert.equal(action.props.variant, "ghost")
    assert.equal(action.props.size, "icon-sm")
    assert.equal(action.props.className, "rounded-full")
    assert.equal(action.props.title, action.props["aria-label"])
    assert.equal(action.props.disabled, true)
  }
  const completed = exports.ImageGenerationResult({ result: { size: "1024x1024", output_format: "png" }, pending: false, available: true })
  const facts = completed.props.children[1].props.children[1].props.children
  const quality = facts.find(node => node.props.children[0].props.children === "图像质量")
  assert.equal(quality.props.children[1].props.children, "\u2014")
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/data-sources/apim/pages/image-generation.css", import.meta.url), "utf8"))
  const selector = '.invoke-result > .finops-panel-title .finops-panel-title-actions > [data-slot="button"]'
  const declarations = target => {
    const rule = stylesheet.nodes.find(node => node.type === "rule" && node.selector === target)
    assert.ok(rule, `Missing image action style: ${target}`)
    return Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  }
  assert.deepEqual(declarations(selector), { border: "0", background: "transparent" })
  assert.deepEqual(declarations(`${selector}.rounded-full`), { "border-radius": "999px" })
  assert.deepEqual(declarations(`${selector}:enabled:hover`), { background: "var(--muted)" })
})

test("retry client sends paid image authorization only for explicit boolean consent", async () => {
  const start = apiSource.indexOf("  retryGatewayPublication:")
  const end = apiSource.indexOf("  resumeGatewayPublicationAuthorization:", start)
  assert.ok(start >= 0 && end > start)
  const expression = apiSource.slice(start, end).replace(/^\s*retryGatewayPublication:\s*/, "").replace(/,\s*$/, "")
  const writes = []
  const retry = runInNewContext(stripTypeScriptTypes(`(${expression})`), {
    writeJson: async (path, body) => { writes.push({ path, body }); return body },
    normalizeGatewayPublication: value => value,
  })
  for (const consent of [undefined, false, "true", 1]) {
    await retry("publication", undefined, consent)
    assert.equal(writes.at(-1).body.authorize_image_probes, undefined)
  }
  await retry("publication", "unit-key", true)
  assert.equal(writes.at(-1).body.authorize_image_probes, true)
  assert.equal(writes.at(-1).body.api_key, "unit-key")
  assert.equal(writes.at(-1).path, "/api/v1/model-management/publications/publication/retry")
})

test("registry normalization preserves Databricks capabilities independently and fails closed", () => {
  for (const connections of [true, undefined, null, false, "true", 1]) {
    for (const oauth of [true, undefined, null, false, "true", 1]) {
      const registry = normalizeRegistry({
        ...emptyRegistry,
        databricks_connections_supported: connections,
        databricks_oauth_supported: oauth,
      })
      assert.equal(registry.databricks_connections_supported, connections === true)
      assert.equal(registry.databricks_oauth_supported, oauth === true)
    }
  }
})

test("registry normalization preserves enabled image capability and v4 configuration", () => {
  const defaults = { output_token_reserve: 8192 }
  const registry = normalizeRegistry({
    ...emptyRegistry,
    backend_pool_session_affinity_supported: true,
    image_generation_supported: true,
    image_configuration_defaults: defaults,
    image_configuration_schema_version: 4,
  })
  assert.equal(registry.image_generation_supported, true)
  assert.equal(registry.image_configuration_defaults, defaults)
  assert.equal(registry.image_configuration_schema_version, 4)
  assert.equal(registry.backend_pool_session_affinity_supported, true)
})

test("registry normalization does not enable absent or non-boolean image capability", () => {
  for (const supported of [undefined, null, false, "true", 1]) {
    const registry = normalizeRegistry({
      ...emptyRegistry,
      image_generation_supported: supported,
      image_configuration_schema_version: 4,
    })
    assert.equal(registry.image_generation_supported, false)
  }
})

test("registry normalization preserves unsupported image configuration versions", () => {
  for (const version of [undefined, 3, 5]) {
    const registry = normalizeRegistry({
      ...emptyRegistry,
      image_generation_supported: true,
      image_configuration_defaults: null,
      image_configuration_schema_version: version,
    })
    assert.equal(registry.image_configuration_defaults, null)
    assert.equal(registry.image_configuration_schema_version, version)
    assert.notEqual(registry.image_configuration_schema_version, 4)
  }
})

const result = {
  request_id: "request-image-1",
  output_format: "png",
  data: [{ media_type: "image/png", b64_json: Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).toString("base64") }],
}

test("preview creates a typed Blob without external URLs", async () => {
  const blob = generatedImageBlob(result)
  assert.equal(blob.type, "image/png")
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [137, 80, 78, 71, 13, 10, 26, 10])
})

for (const data of [[], [...result.data, ...result.data], [{ media_type: "image/svg+xml", b64_json: "abcd" }], [{ media_type: "image/png", b64_json: "!invalid!" }], [{ media_type: "image/png", b64_json: "" }]]) {
  test(`invalid image payload ${JSON.stringify(data)} is rejected`, () => {
    assert.throws(() => generatedImageBlob({ ...result, data }))
  })
}

test("oversized Base64 is rejected before decoding", () => {
  assert.throws(() => generatedImageBlob({ ...result, data: [{ media_type: "image/png", b64_json: "A".repeat(16 * 1024 * 1024 + 1) }] }))
})

test("download names contain no paths or prompt content", () => {
  assert.equal(imageDownloadFilename({ ...result, request_id: "../../unsafe/name" }), "turnstile-unsafename.png")
  assert.equal(imageDownloadFilename({ ...result, request_id: "..." }), "turnstile-image.png")
})

for (const [format, extension] of [["png", "png"], ["jpeg", "jpg"], ["webp", "webp"]]) {
  test(`preview and download preserve returned ${format} format`, () => {
    const response = { ...result, output_format: format, data: [{ ...result.data[0], media_type: `image/${format}` }] }
    assert.equal(generatedImageBlob(response).type, `image/${format}`)
    assert.equal(imageDownloadFilename(response), `turnstile-request-image-1.${extension}`)
  })
}