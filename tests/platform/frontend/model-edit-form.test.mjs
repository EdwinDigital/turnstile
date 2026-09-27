import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

import {
  applyCatalogOption,
  createModelEditDraft,
  discountedRate,
  modelKeyFromReference,
  modelEditHasChanges,
  modelEditPayload,
  modelEditRoleOptions,
  resolveDiscount,
  setModelEditDefault,
  toggleModelRole,
  validateModelEdit,
} from "../../../frontend/src/components/model-management/model-edit-form.ts"
import * as modelEditForm from "../../../frontend/src/components/model-management/model-edit-form.ts"
import * as modelVendor from "../../../frontend/src/components/model-management/openai-compatible.ts"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))
const entry = fileURLToPath(new URL("../../../frontend/src/components/model-management/model-edit-dialog.tsx", import.meta.url))
const bundle = await rolldown({ input: entry, external: path => path !== entry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let component
try { component = (await bundle.generate({ format: "cjs" })).output[0].code }
finally { await bundle.close() }

const priceEntry = fileURLToPath(new URL("../../../frontend/src/components/model-management/model-price-source.tsx", import.meta.url))
const priceBundle = await rolldown({ input: priceEntry, external: path => path !== priceEntry, transform: { jsx: { runtime: "automatic" } }, treeshake: false })
let priceComponent
try { priceComponent = (await priceBundle.generate({ format: "cjs" })).output[0].code }
finally { await priceBundle.close() }

function editorHarness(model, providerKind = "microsoft_foundry") {
  const slots = []
  let cursor = 0
  const imports = {
    react: { useId: () => "unit-editor", useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
    } },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "./model-edit-form": modelEditForm,
    "./model-availability": { ModelAvailabilityStatus: "ModelAvailabilityStatus", ModelAvailabilitySwitch: "ModelAvailabilitySwitch", useModelAvailabilityCommand: () => ({ inFlight: false, error: null, change() {} }) },
    "./openai-compatible": modelVendor,
    "../brand-logos": { GatewayBrandLogo: "GatewayBrandLogo", ProviderBrandLogo: "ProviderBrandLogo", gatewayBrandFromIdentity: value => value, providerBrandFromMetadata: value => value },
  }
  const exports = {}
  runInNewContext(component, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const saved = []
  const props = {
    model, busy: false, error: null, onClose() {}, onSave: value => saved.push(value),
    registry: {
      gateways: [{ id: "gateway", name: "Unit gateway", implementation: "apim" }],
      providers: [{ id: model.provider_id, provider_kind: providerKind }],
      runtimes: [{ id: model.runtime_id, gateway_profile_id: "gateway", brand_key: providerKind, name: "Unit connection", config: { model_vendor: "deepseek" } }],
    },
  }
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(typeof value.type === "function" ? value.type(value.props) : value.props?.children)] : []
  const render = () => { cursor = 0; return exports.ModelEditDialog(props) }
  return { props, saved, nodes, find: predicate => nodes(render()).find(predicate), all: () => nodes(render()) }
}

function savedModel(overrides = {}) {
  return {
    id: "model-id",
    provider_id: "provider-id",
    runtime_id: "runtime-id",
    model_key: "published-model",
    upstream_model_id: "upstream-deployment",
    family_key: "generic",
    assignment_required: true,
    capabilities: ["chat", "streaming"],
    display_name: "Existing model",
    context_window: 128000,
    input_cost_per_million: 2,
    output_cost_per_million: 8,
    cached_cost_per_million: 0.5,
    cache_write_cost_per_million: 1,
    allowed_roles: ["owner", "member", "custom-role"],
    enabled: true,
    is_default: false,
    // Every model carries these once the registry knows about list prices. "manual" is what a
    // model that was priced by hand reports, which is every model that existed before.
    price_source: "manual",
    price_reference: null,
    price_discount_percent: null,
    ...overrides,
  }
}

test("unchanged model metadata round-trips without a pending change", () => {
  const model = savedModel()
  const draft = createModelEditDraft(model)
  const payload = modelEditPayload(model, draft)
  assert.equal(modelEditHasChanges(draft, createModelEditDraft(model)), false)
  for (const [key, value] of Object.entries(payload)) assert.deepEqual(value, model[key], key)
  assert.equal("id" in payload, false)
  assert.equal("gateway_profile_id" in payload, false)
})

test("draft fields cannot replace saved routing identity or capabilities", () => {
  const model = savedModel()
  const draft = {
    ...createModelEditDraft(model),
    displayName: " Renamed model ",
    inputPrice: "3.123456789",
    provider_id: "other-provider",
    runtime_id: "other-runtime",
    model_key: "other-alias",
    upstream_model_id: "other-deployment",
    family_key: "other-family",
    assignment_required: false,
    capabilities: ["vision"],
  }
  const payload = modelEditPayload(model, draft)
  for (const key of ["provider_id", "runtime_id", "model_key", "upstream_model_id",
    "family_key", "assignment_required", "capabilities"]) {
    assert.deepEqual(payload[key], model[key], key)
  }
  assert.equal(payload.display_name, "Renamed model")
  assert.equal(payload.input_cost_per_million, 3.123456789)
  assert.notEqual(payload.capabilities, model.capabilities)
})

test("nulls and explicit zero prices retain distinct meanings", () => {
  const model = savedModel({
    upstream_model_id: null,
    context_window: null,
    input_cost_per_million: null,
    output_cost_per_million: 0,
    cached_cost_per_million: null,
    cache_write_cost_per_million: 0,
  })
  const draft = createModelEditDraft(model)
  assert.equal(draft.contextWindow, "")
  assert.equal(draft.inputPrice, "")
  assert.equal(draft.outputPrice, "0")
  const payload = modelEditPayload(model, draft)
  for (const [key, value] of Object.entries(payload)) assert.deepEqual(value, model[key], key)
})

test("cleared cache rates request backend fallback without inventing prices", () => {
  const model = savedModel()
  const payload = modelEditPayload(model, {
    ...createModelEditDraft(model), cacheReadPrice: "", cacheWritePrice: " ",
  })
  assert.equal(payload.cached_cost_per_million, null)
  assert.equal(payload.cache_write_cost_per_million, null)
  assert.equal(payload.input_cost_per_million, model.input_cost_per_million)
})

test("role options preserve custom roles without granting them implicitly", () => {
  const model = savedModel({ allowed_roles: ["admin", "custom-role"] })
  const draft = createModelEditDraft(model)
  assert.deepEqual(modelEditRoleOptions(model), ["owner", "member", "admin", "custom-role"])
  assert.deepEqual(modelEditPayload(model, draft).allowed_roles, model.allowed_roles)
  assert.notEqual(draft.allowedRoles, model.allowed_roles)
  const roles = toggleModelRole(draft.allowedRoles, "owner", true)
  assert.deepEqual(toggleModelRole(roles, "owner", true), roles)
  assert.deepEqual(toggleModelRole(roles, "owner", false), model.allowed_roles)
  assert.deepEqual(modelEditPayload(model, { ...draft, allowedRoles: [] }).allowed_roles, [])
})

test("restored roles are unchanged regardless of checkbox order", () => {
  const initial = createModelEditDraft(savedModel())
  assert.equal(modelEditHasChanges(initial, {
    ...initial, allowedRoles: [...initial.allowedRoles].reverse(),
  }), false)
  assert.equal(modelEditHasChanges(initial, { ...initial, allowedRoles: [] }), true)
})

test("invalid names cannot produce a save payload", () => {
  const model = savedModel()
  for (const displayName of ["", "   ", "x".repeat(256)]) {
    const draft = { ...createModelEditDraft(model), displayName }
    assert.equal(validateModelEdit(draft), "display_name")
    assert.throws(() => modelEditPayload(model, draft), /display_name/)
  }
})

test("context windows require positive safe integers and prices finite non-negative values", () => {
  const draft = createModelEditDraft(savedModel())
  for (const contextWindow of ["0", "-1", "1.5", "Infinity", "invalid", "9007199254740992"]) {
    assert.equal(validateModelEdit({ ...draft, contextWindow }), "context_window")
  }
  for (const price of ["-1", "Infinity", "NaN", "1e999"]) {
    for (const field of ["inputPrice", "outputPrice", "cacheReadPrice", "cacheWritePrice"]) {
      assert.equal(validateModelEdit({ ...draft, [field]: price }), "prices")
    }
  }
  assert.equal(validateModelEdit({ ...draft, contextWindow: "1", inputPrice: "0" }), null)
})

test("default metadata changes never include or change model availability", () => {
  const model = savedModel()
  const initial = createModelEditDraft(model)
  const promoted = setModelEditDefault(initial, true)
  assert.equal("enabled" in promoted, false)
  assert.equal(promoted.isDefault, true)
  const disabled = createModelEditDraft(savedModel({ enabled: false, is_default: true }))
  assert.equal(validateModelEdit(disabled), null)
  assert.equal("enabled" in modelEditPayload(model, disabled), false)
  assert.equal(setModelEditDefault(promoted, false).isDefault, false)
  assert.equal("enabled" in initial, false)
  assert.equal(initial.isDefault, false)
})

test("the editor uses the dedicated availability switch and metadata save never re-enables a model", () => {
  const harness = editorHarness(savedModel({ enabled: false, is_default: true }))
  const availability = { model_id: "model-id", status: "succeeded", desired_enabled: false }
  harness.props.availability = availability
  const control = harness.find(node => node.type === "ModelAvailabilitySwitch")
  assert.equal(control.props.readback, availability)
  assert.equal(control.props.registryEnabled, false)
  const labels = harness.all().filter(node => node.type === "label" && node.props.className === "model-editor-checkbox")
  const defaultLabel = labels.find(node => node.props.children.some(child => child.props?.children === "设为默认"))
  assert.equal(labels.some(node => node.props.children.some(child => child.props?.children === "启用")), false)
  defaultLabel.props.children[0].props.onCheckedChange(false)
  harness.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(harness.saved.length, 1)
  assert.equal(harness.saved[0].is_default, false)
  assert.equal("enabled" in harness.saved[0], false)
})

test("model editor restores identity, help and units without changing saved routing", () => {
  const model = savedModel({ provider_name: "Unit provider" })
  const view = editorHarness(model)
  assert.equal(view.find(node => node.type === "DialogContent").props.finalFocus, false)
  assert.equal(view.find(node => node.type === "DialogDescription").props.children, "更新模型名称、价格和访问配置。连接与模型标识保持不变。")
  const identity = view.find(node => node.props?.className === "model-editor-identity")
  assert.deepEqual(view.nodes(identity).filter(node => node.type === "dt").map(node => node.props.children), ["目标网关", "接入类型", "连接", "模型别名", "Deployment Name"])
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children.startsWith("连接和模型标识决定")))
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children.startsWith("设为整个模型目录")))
  assert.ok(view.find(node => node.props?.className === "model-editor-unit" && node.props.children === "Tokens"))
  const advanced = view.find(node => node.props?.className === "model-editor-advanced-body")
  assert.ok(view.nodes(advanced).some(node => node.type === "p" && node.props.children.startsWith("角色限制不替代")))
  assert.ok(view.nodes(advanced).some(node => node.type === "p" && node.props.children.startsWith("当前应用登录角色")))
  view.find(node => node.type === "Input" && node.props.maxLength === 255).props.onChange({ target: { value: "Renamed" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 1)
  assert.equal(view.saved[0].display_name, "Renamed")
  for (const key of ["provider_id", "runtime_id", "model_key", "upstream_model_id", "capabilities", "allowed_roles"]) assert.deepEqual(view.saved[0][key], model[key])
})

test("model editor explains image default restrictions and preserves validation", () => {
  const view = editorHarness(savedModel({ capabilities: ["image_generation"], context_window: null, cached_cost_per_million: null }))
  assert.ok(view.find(node => node.type === "FieldHelp" && node.props.children === "图片模型不能设为聊天默认"))
  const defaultLabel = view.find(node => node.type === "label" && view.nodes(node).some(child => child.type === "span" && child.props.children === "设为默认"))
  assert.equal(view.nodes(defaultLabel).find(node => node.type === "Checkbox").props.disabled, true)
  assert.equal(view.find(node => node.props?.id === "unit-editor-context"), undefined)
  view.find(node => node.type === "Input" && node.props.maxLength === 255).props.onChange({ target: { value: "Image renamed" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 0)
  assert.equal(view.find(node => node.props?.role === "alert").props.children, "请填写文字输入、缓存文字和图像输出单价。")
  view.props.busy = true
  assert.ok(view.all().filter(node => node.type === "Input" || node.type === "Checkbox").every(node => node.props.disabled))
})

test("model editor retains source validation wording and one vendor identity row", () => {
  const view = editorHarness(savedModel(), "openai_compatible")
  const identity = view.find(node => node.props?.className === "model-editor-identity")
  const labels = view.nodes(identity).filter(node => node.type === "dt").map(node => node.props.children)
  assert.deepEqual(labels, ["目标网关", "API 服务商", "连接", "模型别名", "上游模型 ID"])
  view.find(node => node.type === "Input" && node.props.id === "unit-editor-context").props.onChange({ target: { value: "-1" } })
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 0)
  assert.equal(view.find(node => node.props?.role === "alert").props.children, "上下文窗口须为正整数，或留空。")
})

test("model editor density and responsive rules retain the source composition", () => {
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const declarations = (selector, media = null) => {
    const matches = []
    stylesheet.walkRules(rule => {
      if (rule.selector === selector && (rule.parent.type === "atrule" ? rule.parent.params : null) === media) matches.push(rule)
    })
    assert.equal(matches.length, 1, selector)
    return Object.fromEntries(matches[0].nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  }
  const identity = declarations(".model-editor-identity")
  assert.equal(identity.background, "var(--muted)")
  assert.equal(identity.padding, "10px 12px")
  assert.equal(identity.gap, "8px 14px")
  assert.equal(identity["border-radius"], "8px")
  const identityCode = declarations(".model-editor-identity code")
  assert.equal(identityCode.font, "inherit")
  assert.equal(identityCode["user-select"], "text")
  const pricing = declarations(".model-price-arithmetic")
  for (const property of ["background", "padding", "border-radius"]) {
    assert.equal(pricing[property], identity[property], property)
  }
  assert.equal(pricing["min-width"], "0")
  assert.equal(declarations(".model-price-grid-head, .model-price-grid-row").padding, "3px 0")
  assert.equal(declarations(".model-price-context-table th, .model-price-context-table td").padding, "5px 4px")
  assert.equal(declarations('.model-price-context-table th[rowspan], .model-price-context-table th[scope="row"]')["padding-inline-start"], "0")
  stylesheet.walkRules(rule => {
    assert.notEqual(rule.selector, ".model-price-context-table tr > :first-child")
  })
  const loading = declarations(".model-price-loading")
  assert.equal(loading.display, "flex")
  assert.equal(loading["align-items"], "center")
  assert.equal(loading.gap, "6px")
  assert.equal(declarations(".model-price-loading > svg").flex, "0 0 auto")
  assert.equal(declarations(".model-editor-advanced > summary::before").content, '"+"')
  assert.equal(declarations(".model-editor-advanced[open] > summary::before").content, '"\\2212"')
  assert.equal(declarations(".model-editor-advanced-body").padding, "6px 0 2px")
  assert.equal(declarations(".model-editor-checkbox")["min-height"], "26px")
  assert.equal(declarations(".model-editor-checkbox-grid")["grid-template-columns"], "repeat(3, minmax(0, 1fr))")
  assert.equal(declarations(".model-editor-identity", "(max-width: 640px)")["grid-template-columns"], "1fr")
  assert.equal(declarations(".model-editor-checkbox-grid", "(max-width: 640px)")["grid-template-columns"], "repeat(2, minmax(0, 1fr))")
})

for (const [locale, exportName] of [["en", "ENGLISH_CORE_PHRASES"], ["ja", "JAPANESE_CORE_PHRASES"], ["ko", "KOREAN_CORE_PHRASES"]]) {
  test(`model editor source help and validation are localized in ${locale}`, async () => {
    const phrases = (await import(new URL(`../../../frontend/src/locales/${locale}/phrases-core.ts`, import.meta.url)))[exportName]
    assert.ok(phrases["价格来源"])
    assert.ok(phrases["手工填写"])
    if (locale === "en") {
      assert.equal(phrases["价格来源"], "Price source")
      assert.equal(phrases["手工填写"], "Manual")
      assert.equal(phrases["来源"], "Source")
    }
    for (const phrase of ["更新模型名称、价格和访问配置。连接与模型标识保持不变。", "显示名称须为 1–255 个字符。", "上下文窗口须为正整数，或留空。", "单价须为非负有限数字，或留空。", "连接和模型标识决定已发布路由，仅供查看；更换连接请走模型接入与发布流程。", "设为整个模型目录的默认模型，并将其连接设为默认；不改变网关路由优先级。", "图片模型不能设为聊天默认", "角色限制不替代人员模型分配，仍需满足连接和人员的访问策略。", "当前应用登录角色为 Owner 和 Member；其他已有角色值按兼容配置保留。"]) {
      assert.ok(phrases[phrase], phrase)
      assert.notEqual(phrases[phrase], phrase)
    }
  })
}

const catalogOption = (overrides = {}) => ({
  reference: "azure_retail:Azure OpenAI:gpt 4.1:Global:*",
  deployment: "Global",
  regions: ["brazilsouth", "canadaeast", "eastus"],
  region_required: false,
  input_per_million: 2,
  output_per_million: 8,
  cached_per_million: 0.5,
  cache_write_per_million: null,
  ...overrides,
})

test("a model keeps its typed rates until someone opts it into a list price", () => {
  const model = savedModel()
  const draft = createModelEditDraft(model)
  assert.equal(draft.priceSource, "manual")
  const payload = modelEditPayload(model, draft)
  assert.equal(payload.price_source, "manual")
  assert.equal(payload.price_reference, null)
  assert.equal(payload.input_cost_per_million, model.input_cost_per_million)
})

test("choosing a deployment prices every bucket the source publishes", () => {
  const draft = applyCatalogOption(
    createModelEditDraft(savedModel()), "azure_retail", catalogOption(), 90)
  assert.equal(draft.priceSource, "azure_retail")
  assert.equal(draft.priceReference, "azure_retail:Azure OpenAI:gpt 4.1:Global:*")
  assert.equal(draft.inputPrice, "1.8")
  assert.equal(draft.outputPrice, "7.2")
  assert.equal(draft.cacheReadPrice, "0.45")
  assert.equal(draft.cacheWritePrice, "")
})

test("a discount of 100 percent and no discount price identically", () => {
  assert.equal(discountedRate(2, 100), 2)
  assert.equal(discountedRate(2, null), 2)
  assert.equal(discountedRate(15, 66), 9.9)
})

test("a model discount overrides the connection, and blank inherits it", () => {
  const base = createModelEditDraft(savedModel())
  assert.deepEqual(resolveDiscount(base, 90), { percent: 90, inherited: true })
  assert.deepEqual(resolveDiscount({ ...base, discountPercent: "66" }, 90),
    { percent: 66, inherited: false })
  assert.deepEqual(resolveDiscount(base, null), { percent: null, inherited: true })
})

test("following a list price requires a chosen reference, and a discount stays in range", () => {
  const base = createModelEditDraft(savedModel())
  assert.equal(validateModelEdit({ ...base, priceSource: "anthropic" }), "price_reference")
  assert.equal(validateModelEdit({ ...base, discountPercent: "0" }), "discount")
  assert.equal(validateModelEdit({ ...base, discountPercent: "101" }), "discount")
  assert.equal(validateModelEdit({ ...base, discountPercent: "90" }), null)
})


test("a stored reference points back at the model it was chosen from", () => {
  assert.equal(
    modelKeyFromReference("azure_retail:Azure OpenAI:gpt 4.1:Global:*"),
    "azure_retail:Azure OpenAI:gpt 4.1")
  assert.equal(
    modelKeyFromReference("anthropic:Anthropic:Claude Opus 5:List price:*"),
    "anthropic:Anthropic:Claude Opus 5")
  // Anything that is not a reference has no model to point at, and saying so beats guessing.
  assert.equal(modelKeyFromReference("nonsense"), null)
})

function catalogResponse(overrides = {}) {
  return {
    model_entry: { key: "azure_retail:Azure OpenAI:gpt 4.1", label: "gpt 4.1", product: "Azure OpenAI", source: "azure_retail" },
    options: [catalogOption()], complete: true, unreadable: [], other_meters: [], note: null,
    ...overrides,
  }
}

function contextCatalogModel(band, overrides = {}) {
  const label = `Example Model ${band} Std`
  return { key: `azure_retail:Example Product:${label}`, label, product: "Example Product", source: "azure_retail", ...overrides }
}

test("context pricing grouping is independent of model names, products and service tiers", () => {
  const short = contextCatalogModel("ShortCo")
  const long = contextCatalogModel("LongCo")
  const priority = contextCatalogModel("LongCo", { label: "Example Model LongCo PP", key: "priority" })
  const other = catalogResponse().model_entry
  const result = modelEditForm.groupContextPriceModels([long, priority, short, other])
  assert.equal(result.length, 3)
  assert.equal(result[0].label, "Example Model Std")
  assert.equal(result[0].key, short.key)
  assert.deepEqual(result[0].contextPricing.tiers.map(tier => tier.model), [short, long])
  assert.ok(result[0].contextPricing.tiers.every(tier => tier.inputRange === null))
  assert.deepEqual(result[1].contextPricing.tiers.map(tier => tier.model), [priority])
  assert.equal(result[2], other)
  assert.equal(short.label, "Example Model ShortCo Std")
  assert.equal(modelEditForm.groupContextPriceModels([short])[0].contextPricing.tiers.length, 1)
  const variants = ["5.6 sol", "6 astra", "Future Model 99", "Some Other Vendor"]
  for (const name of variants) {
    for (const marker of [["ShortCo", "LongCo"], ["short context", "long context"], ["Short-Context", "Long-Context"]]) {
      const models = marker.map(band => ({ ...short, key: `${name}:${band}`, label: `${name} ${band} PP` }))
      const grouped = modelEditForm.groupContextPriceModels(models)
      assert.equal(grouped.length, 1)
      assert.equal(grouped[0].label, `${name} PP`)
      assert.ok(grouped[0].contextPricing.tiers.every(tier => tier.inputRange === null))
    }
  }
  assert.equal(modelEditForm.groupContextPriceModels([short, { ...long, product: "Different Product" }]).length, 2)
  assert.equal(modelEditForm.groupContextPriceModels([short, { ...long, source: "anthropic" }]).length, 2)
})

test("context pricing never derives a threshold from a known or new model name", () => {
  for (const [product, name] of [["Azure OpenAI GPT5", "5.6 sol"], ["Azure OpenAI GPT6", "6 astra"], ["New Publisher", "future model 99"]]) {
    const models = ["ShortCo", "LongCo"].map(band => contextCatalogModel(band, {
      key: `${product}:${name}:${band}`, label: `${name} ${band} Std`, product,
    }))
    const [grouped] = modelEditForm.groupContextPriceModels(models)
    assert.ok(grouped.contextPricing.tiers.every(tier => tier.inputRange === null))
  }
})

test("context pricing preserves backend ranges and provenance without bundled rules", () => {
  const source = { rule_id: "published-input-context", url: "https://example.com/model#pricing", revision: "sha256:unit", verified_on: "2026-09-21" }
  const ranges = [{ min_input_tokens: 0, max_input_tokens: 91000 }, { min_input_tokens: 91001, max_input_tokens: null }]
  for (const name of ["Any model", "Model invented after this release"]) {
    const models = ranges.map((input_range, order) => contextCatalogModel("", {
      key: `vendor:${name}:${order}`, label: `SKU ${order}`, product: "Any product", source: "anthropic",
      context_tier: { group_key: name, model_label: name, service_tier: "standard",
        key: order === 0 ? "short" : "long", label: `Band ${order}`, order,
        input_range, input_range_source: source },
    }))
    const [grouped] = modelEditForm.groupContextPriceModels(models)
    assert.deepEqual(grouped.contextPricing.tiers.map(tier => tier.inputRange), ranges)
    assert.ok(grouped.contextPricing.tiers.every(tier => tier.inputRangeSource === source))
  }
})

test("custom context thresholds are explicit and require backend support", () => {
  const tiers = ["short", "long"].map((key, order) => ({
    key, order, label: key, inputRange: null,
    option: catalogOption({ reference: `azure_retail:Any:Future ${key}:Global:*` }),
  }))
  const draft = {
    ...createModelEditDraft(savedModel(), true, true), priceSource: "azure_retail",
    contextPricingPreview: true, contextPriceTiers: tiers,
    contextPriceReferences: tiers.map(tier => tier.option.reference),
    contextRuleMode: "custom", contextThreshold: "12345",
  }
  assert.equal(validateModelEdit(draft), null)
  assert.deepEqual(modelEditForm.effectiveContextPriceTiers(draft).map(tier => tier.inputRange), [
    { min_input_tokens: 0, max_input_tokens: 12345 }, { min_input_tokens: 12346, max_input_tokens: null },
  ])
  assert.deepEqual(modelEditPayload(savedModel(), draft).context_price_rule, {
    short_context_max_tokens: 12345, note: "", source_url: null,
  })
  assert.equal(validateModelEdit({ ...draft, contextPriceRulesSupported: false }), "context_price_rule_preview")
  for (const value of ["", "0", "-1", "1.5", "Infinity", "9007199254740991"]) {
    assert.equal(validateModelEdit({ ...draft, contextThreshold: value }), "context_price_rule")
  }
  assert.equal(validateModelEdit({ ...draft, contextRuleMode: "catalog" }), "context_pricing_incomplete")
  assert.equal(modelEditForm.setModelEditPriceSource(draft, "manual").contextRuleMode, "catalog")
})

test("request cost formulas use recorded token buckets and preserve unpriced versus zero", async () => {
  const { estimateRequestCost, requestCostCalculation } = await import("../../../frontend/src/lib/pricing.ts")
  const request = {
    prompt_tokens: 100, cached_tokens: 200, cache_write_tokens: 50, completion_tokens: 10,
    total_tokens: 310, estimated: false, reconciled_at: null, cost_available: true,
    input_price_per_million: 2, cached_price_per_million: 0.5,
    cache_write_price_per_million: 3, output_price_per_million: 8,
    estimated_cost: 0.000505, ingest_error: null,
    price_context_tier: "short",
    price_context_snapshot: { context_tiers: [{ key: "short", min_input_tokens: 0, max_input_tokens: 500 }] },
  }
  const calculation = requestCostCalculation(request)
  assert.deepEqual(calculation.terms.map(term => term.tokens), [100, 150, 50, 10])
  assert.equal(calculation.inputTokens, 300)
  assert.equal(calculation.calculatedCost, 0.000505)
  assert.equal(calculation.matchesRecordedCost, true)
  assert.equal(estimateRequestCost({ ...request, estimated_cost: 0 }, []), 0)
  assert.equal(estimateRequestCost({ ...request, estimated_cost: 0, cost_available: false }, [savedModel()]), null)
  assert.equal(requestCostCalculation({ ...request, input_price_per_million: null }), null)
  assert.equal(requestCostCalculation({ ...request, cache_write_tokens: 201 }), null)
  assert.equal(requestCostCalculation({ ...request, prompt_tokens: 501 }), null)
  assert.equal(requestCostCalculation({ ...request, estimated: true }), null)
  assert.equal(estimateRequestCost({ ...request, usage_available: false }, [savedModel()]), null)
  assert.equal(requestCostCalculation({ ...request, usage_available: false }), null)
  assert.equal(requestCostCalculation({ ...request, estimated_cost: 1 }).matchesRecordedCost, false)
})

test("context pricing pairs exact deployment and regional rates without deriving long prices", () => {
  const shortModel = contextCatalogModel("ShortCo")
  const longModel = contextCatalogModel("LongCo")
  const [model] = modelEditForm.groupContextPriceModels([shortModel, longModel])
  const short = catalogResponse({ model_entry: shortModel, options: [catalogOption({
    reference: `${shortModel.key}:Global:*`, regions: ["eastus", "westus"],
    input_per_million: 4, output_per_million: 20, cached_per_million: 0.4, cache_write_per_million: 5,
  })] })
  const long = catalogResponse({ model_entry: longModel, options: [
    catalogOption({ reference: `${longModel.key}:Global:eastus`, regions: ["eastus"], input_per_million: 9, output_per_million: 31 }),
    catalogOption({ reference: `${longModel.key}:Global:westus`, regions: ["westus"], input_per_million: 10, output_per_million: 33 }),
    catalogOption({ reference: `${longModel.key}:Data Zone:*`, deployment: "Data Zone", regions: ["eastus"], input_per_million: 11, output_per_million: 35 }),
  ] })
  const paired = modelEditForm.pairContextPriceOptions(model, [short, long])
  assert.equal(paired.complete, true)
  assert.equal(paired.options.length, 2)
  assert.ok(paired.options.every(option => option.region_required && option.deployment === "Global"))
  assert.deepEqual(paired.options.map(option => option.regions), [["eastus"], ["westus"]])
  assert.deepEqual(paired.options.map(option => option.contextPricing.tiers[1].option.output_per_million), [31, 33])
  assert.equal(new Set(paired.options.map(option => option.reference)).size, 2)
  assert.equal(paired.options[1].references_by_region.westus, paired.options[1].reference)
  assert.equal(paired.options[0].contextPricing.tiers[0].option.cache_write_per_million, 5)
  assert.equal(modelEditForm.pairContextPriceOptions(model, [short, { ...long, complete: false }]).options.length, 0)
  assert.equal(modelEditForm.pairContextPriceOptions(model, [short, { ...long, options: [] }]).options.length, 0)
  assert.throws(() => modelEditForm.pairContextPriceOptions(model, [long, short]), /不匹配/)
  assert.throws(() => modelEditForm.pairContextPriceOptions(model, [short, { ...long, options: [long.options[0], { ...long.options[0], input_per_million: 99 }] }]), /冲突/)
})

test("context pricing structured metadata supports arbitrary bands and explicit input ranges", () => {
  const ranges = [
    { min_input_tokens: 0, max_input_tokens: 12345 },
    { min_input_tokens: 12346, max_input_tokens: 98765 },
    { min_input_tokens: 98766, max_input_tokens: null },
  ]
  const models = ranges.map((input_range, index) => contextCatalogModel("", {
    key: `vendor:${index}`, label: `Opaque SKU ${index}`, source: "anthropic", product: "Any product",
    context_tier: { group_key: "opaque-model", model_label: "Any model", service_tier: "Flexible", key: `band-${index}`,
      label: `Band ${index + 1}`, order: index, input_range },
  }))
  const [model] = modelEditForm.groupContextPriceModels([...models].reverse())
  assert.equal(model.label, "Any model · Flexible")
  const responses = models.map((model_entry, index) => catalogResponse({ model_entry, options: [catalogOption({
    reference: `${model_entry.key}:Global:*`, input_per_million: index + 1,
  })] }))
  const paired = modelEditForm.pairContextPriceOptions(model, responses)
  assert.equal(paired.options.length, 1)
  assert.deepEqual(paired.options[0].contextPricing.tiers.map(tier => tier.inputRange), ranges)
  assert.deepEqual(paired.options[0].contextPricing.tiers.map(tier => tier.option.input_per_million), [1, 2, 3])
  const invalid = structuredClone(responses)
  invalid[1].model_entry.context_tier.input_range.min_input_tokens = 12345
  assert.throws(() => modelEditForm.pairContextPriceOptions(model, invalid), /区间无效/)
  invalid[1].model_entry.context_tier.input_range.min_input_tokens = 12347
  assert.throws(() => modelEditForm.pairContextPriceOptions(model, invalid), /区间无效/)
})

test("context pricing uses ranges returned by details even when search metadata is empty", () => {
  const models = ["ShortCo", "LongCo"].map(band => contextCatalogModel(band))
  const [candidate] = modelEditForm.groupContextPriceModels(models)
  const ranges = [{ min_input_tokens: 0, max_input_tokens: 12345 }, { min_input_tokens: 12346, max_input_tokens: null }]
  const responses = models.map((model, index) => catalogResponse({ model_entry: {
    ...model, context_tier: {
      group_key: "example model std", model_label: "Example Model Std", service_tier: "",
      key: index === 0 ? "short" : "long", label: index === 0 ? "Short context" : "Long context",
      order: index, input_range: ranges[index],
    },
  } }))
  const result = modelEditForm.pairContextPriceOptions(candidate, responses)
  assert.deepEqual(result.options[0].contextPricing.tiers.map(tier => tier.inputRange), ranges)
  assert.ok(candidate.contextPricing.tiers.every(tier => tier.inputRange === null))
  responses[1].model_entry.context_tier.service_tier = "priority"
  assert.throws(() => modelEditForm.pairContextPriceOptions(candidate, responses), /不匹配/)
})

test("context pricing preview loads both real catalog keys and discounts both without saving one tier", async () => {
  const shortModel = contextCatalogModel("ShortCo")
  const longModel = contextCatalogModel("LongCo")
  const models = [shortModel, longModel]
  const requested = []
  const harness = priceSourceHarness(key => {
    requested.push(key)
    return catalogResponse({ model_entry: key === shortModel.key ? shortModel : longModel, options: [catalogOption({
      reference: `${key}:Global:*`, input_per_million: key === shortModel.key ? 4 : 9,
      output_per_million: key === shortModel.key ? 20 : 31,
    })] })
  }, savedModel(), { models, unavailable: [] })
  const originalInput = harness.draft.inputPrice
  await harness.find(node => node.type?.name === "ModelStep").props.onSearch()
  const matches = harness.find(node => node.type?.name === "ModelStep").props.matches
  assert.equal(matches.length, 1)
  assert.equal(matches[0].label, "Example Model Std")
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(matches[0])
  harness.find(node => node.props?.id === "model-discount").props.onChange({ target: { value: "80" } })
  assert.deepEqual(requested, [shortModel.key, longModel.key])
  assert.ok(harness.find(node => node.type === "ResizableTable" && node.props.className === "model-price-context-table" && node.props.resizable === false))
  const input = harness.find(node => node.props?.["data-price-bucket"] === "input_per_million")
  assert.deepEqual(Array.from(input.props.children[1], cell => cell.props.children), ["$4.00", "$3.20", "$9.00", "$7.20"])
  const output = harness.find(node => node.props?.["data-price-bucket"] === "output_per_million")
  assert.deepEqual(Array.from(output.props.children[1], cell => cell.props.children), ["$20.00", "$16.00", "$31.00", "$24.80"])
  assert.equal(harness.draft.inputPrice, originalInput)
  assert.equal(harness.draft.priceReference, "")
  assert.equal(harness.draft.contextPricingPreview, true)
  assert.equal(validateModelEdit(harness.draft), "context_pricing_preview")
  assert.equal(harness.all().filter(node => node.type === "ModelPriceText" || node.type?.name === "ModelPriceText")
    .filter(node => node.props.children === "阈值未提供").length, 2)
  assert.throws(() => harness.payload(), /context_pricing_preview/)
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  assert.equal(harness.draft.contextPricingPreview, false)
  assert.equal(harness.draft.inputPrice, originalInput)
  assert.equal(harness.all().some(node => node.props?.className === "model-price-context-table"), false)
})

test("context threshold control previews explicit ranges and preserves same-scope refresh", async () => {
  const models = [contextCatalogModel("ShortCo"), contextCatalogModel("LongCo")]
  const harness = priceSourceHarness(key => catalogResponse({
    model_entry: models.find(model => model.key === key),
    options: [catalogOption({ reference: `${key}:Global:*` })],
  }), savedModel(), { models, unavailable: [] })
  await harness.find(node => node.type?.name === "ModelStep").props.onSearch()
  const candidate = harness.find(node => node.type?.name === "ModelStep").props.matches[0]
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(candidate)
  Object.assign(harness.draft, { contextPricingSupported: true, contextPriceRulesSupported: true })
  harness.find(node => node.type === "button" && node.props.children === "管理员自定义").props.onClick()
  harness.find(node => node.props?.id === "model-context-threshold").props.onChange({ target: { value: "98765" } })
  assert.equal(validateModelEdit(harness.draft), null)
  assert.equal(harness.payload().context_price_rule.short_context_max_tokens, 98765)
  const table = harness.find(node => node.type?.name === "ContextPriceTable")
  assert.deepEqual(table.props.pricing.tiers.map(tier => tier.inputRange), [
    { min_input_tokens: 0, max_input_tokens: 98765 }, { min_input_tokens: 98766, max_input_tokens: null },
  ])
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(candidate)
  assert.equal(harness.draft.contextThreshold, "98765")
  harness.find(node => node.type?.name === "ModelStep").props.setQuery("another model")
  assert.equal(harness.draft.contextRuleMode, "catalog")
  assert.equal(harness.draft.contextThreshold, "")
})

function contextRefreshFixture(customRule = true) {
  const threshold = customRule ? 98765 : 1000
  const rule = { short_context_max_tokens: threshold, note: "Accepted estimate", source_url: "https://example.com/pricing" }
  const tiers = ["short", "long"].map((key, index) => ({
    key, label: key, reference: `azure_retail:Unit:meter-${key}:Regional:westus`,
    min_input_tokens: index ? threshold + 1 : 0, max_input_tokens: index ? null : threshold,
    input_price_per_million: 2 + index, output_price_per_million: 8,
    cached_price_per_million: 0.5, cache_write_price_per_million: 1,
    list_prices: { input: 2 + index, output: 8, cached: 0.5, cache_write: 1 },
    model_key: `azure_retail:Unit:meter-${key}`, model_label: `Meter ${key}`,
    product: "Unit", group_key: "unit-model", service_tier: "Standard",
    deployment: "Regional", regions: ["westus"],
  }))
  const model = savedModel({
    price_source: "azure_retail", price_reference: "azure_retail:context:unit",
    price_discount_percent: 80, context_price_rule: customRule ? rule : null,
    context_price_tiers: tiers, input_cost_per_million: null, output_cost_per_million: null,
    cached_cost_per_million: null, cache_write_cost_per_million: null,
  })
  return {
    model,
    responseFor(key) {
      const index = tiers.findIndex(tier => tier.model_key === key)
      assert.ok(index >= 0)
      const tier = tiers[index]
      return catalogResponse({
        model_entry: { key, label: tier.model_label, source: "azure_retail", product: "Unit",
          context_tier: { group_key: "unit-model", model_label: "Unit model", service_tier: "Standard",
            key: tier.key, label: tier.label, order: index,
            input_range: { min_input_tokens: index ? 1001 : 0, max_input_tokens: index ? null : 1000 } } },
        options: [
          catalogOption({ reference: `${key}:Global:*`, regions: ["*"] }),
          catalogOption({ reference: `${key}:Regional:eastus`, deployment: "Regional",
            regions: ["eastus", "westus"], region_required: true, input_per_million: 2.25 + index,
            references_by_region: { eastus: `${key}:Regional:eastus`, westus: `${key}:Regional:westus` } }),
        ],
      })
    },
  }
}

for (const customRule of [false, true]) {
  test(`context refresh preserves saved region and rule (${customRule ? "custom" : "catalog"})`, async () => {
    const { model, responseFor } = contextRefreshFixture(customRule)
    const harness = priceSourceHarness(responseFor, model)
    Object.assign(harness.draft, { contextPricingSupported: true, contextPriceRulesSupported: true })
    const expectedReferences = model.context_price_tiers.map(tier => tier.reference)
    const picker = harness.find(node => node.type?.name === "ModelStep")
    await picker.props.onChoose(picker.props.chosen)
    assert.deepEqual(Array.from(harness.draft.contextPriceReferences), expectedReferences)
    assert.equal(harness.draft.contextRuleMode, customRule ? "custom" : "catalog")
    assert.equal(harness.draft.contextThreshold, customRule ? "98765" : "")
    assert.deepEqual(modelEditForm.contextPriceRulePayload(harness.draft), customRule ? model.context_price_rule : undefined)
    assert.deepEqual(Array.from(harness.draft.contextPriceTiers, tier => tier.option.input_per_million), [2.25, 3.25])
    assert.equal(harness.draft.contextPriceRefresh, true)
    assert.equal(validateModelEdit(harness.draft), null)
    const selected = harness.find(node => node.type?.name === "DeploymentStep").props.selected
    assert.equal(selected.deployment, "Regional")
    assert.equal(selected.reference, selected.references_by_region.westus)
    assert.deepEqual(Array.from(harness.payload().context_price_references), expectedReferences)
    assert.equal(harness.payload().price_discount_percent, 80)
  })
}

for (const failure of ["missing", "incomplete", "offline"]) {
  test(`context refresh blocks ${failure} scope and preserves retry selection`, async () => {
    const { model, responseFor } = contextRefreshFixture()
    let failing = true
    const harness = priceSourceHarness(key => {
      if (failing && failure === "offline") throw new Error("offline")
      const response = responseFor(key)
      if (failing && failure === "incomplete") response.complete = false
      if (failing && failure === "missing") response.options = response.options.slice(0, 1)
      return response
    }, model)
    Object.assign(harness.draft, { contextPricingSupported: true, contextPriceRulesSupported: true })
    const expectedReferences = model.context_price_tiers.map(tier => tier.reference)
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const picker = harness.find(node => node.type?.name === "ModelStep")
      const refreshing = picker.props.onChoose(picker.props.chosen)
      assert.notEqual(validateModelEdit(harness.draft), null)
      await refreshing
      assert.deepEqual(Array.from(harness.draft.contextPriceReferences), expectedReferences)
      assert.deepEqual(modelEditForm.contextPriceRulePayload(harness.draft), model.context_price_rule)
      assert.equal(validateModelEdit(harness.draft), "context_pricing_incomplete")
      assert.throws(() => harness.payload(), /context_pricing_incomplete/)
    }
    failing = false
    const picker = harness.find(node => node.type?.name === "ModelStep")
    await picker.props.onChoose(picker.props.chosen)
    assert.deepEqual(Array.from(harness.payload().context_price_references), expectedReferences)
    assert.deepEqual(harness.payload().context_price_rule, model.context_price_rule)
  })
}

test("context pricing preview renders every metadata band and its own range", async () => {
  const bounds = [[0, 14999], [15000, 45999], [46000, null]]
  const models = bounds.map(([min_input_tokens, max_input_tokens], index) => contextCatalogModel("", {
    key: `source-band-${index}`, label: `Opaque meter ${index}`,
    context_tier: { group_key: "generic", model_label: "Generic model", service_tier: "Standard",
      key: `range-${index}`, label: `Range ${index + 1}`, order: index,
      input_range: { min_input_tokens, max_input_tokens } },
  }))
  const [candidate] = modelEditForm.groupContextPriceModels(models)
  const reads = []
  const harness = priceSourceHarness(key => {
    reads.push(key)
    const index = models.findIndex(model => model.key === key)
    return catalogResponse({ model_entry: models[index], options: [catalogOption({
      reference: `${key}:Global:*`, input_per_million: index + 1, output_per_million: (index + 1) * 10,
    })] })
  })
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(candidate)
  harness.find(node => node.props?.id === "model-discount").props.onChange({ target: { value: "50" } })
  assert.deepEqual(reads, models.map(model => model.key))
  const headers = harness.all().filter(node => node.type === "th" && node.props.scope === "colgroup")
  assert.equal(headers.length, 3)
  assert.deepEqual(Array.from(headers, header => header.props.children[0].props.children), ["Range 1", "Range 2", "Range 3"])
  const input = harness.find(node => node.props?.["data-price-bucket"] === "input_per_million")
  assert.deepEqual(Array.from(input.props.children[1], cell => cell.props.children), ["$1.00", "$0.5000", "$2.00", "$1.00", "$3.00", "$1.50"])
  const text = harness.all().filter(node => node.type === "span" && node.props["data-no-localize"] === true)
    .map(node => node.props.children)
  for (const range of ["≤ 14,999", "15,000 - 45,999", "≥ 46,000"]) assert.ok(text.includes(range), range)
  assert.equal(harness.all().some(node => node.type?.name === "ModelPriceText" && node.props.children === "阈值未提供"), false)
  assert.throws(() => harness.payload(), /context_pricing_preview/)
})

test("context pricing preview rejects stale results and incomplete regional pairs", async () => {
  const shortModel = contextCatalogModel("ShortCo")
  const longModel = contextCatalogModel("LongCo")
  const [candidate] = modelEditForm.groupContextPriceModels([shortModel, longModel])
  let finishLong
  const harness = priceSourceHarness(key => key === shortModel.key
    ? catalogResponse({ model_entry: shortModel })
    : new Promise(resolve => { finishLong = resolve }))
  const request = harness.find(node => node.type?.name === "ModelStep").props.onChoose(candidate)
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  finishLong(catalogResponse({ model_entry: longModel }))
  await request
  assert.equal(harness.draft.priceSource, "manual")
  assert.equal(harness.draft.contextPricingPreview, false)
  assert.equal(harness.all().some(node => node.props?.className === "model-price-context-table"), false)

  const mismatch = priceSourceHarness(key => catalogResponse({
    model_entry: key === shortModel.key ? shortModel : longModel,
    options: [catalogOption({ regions: [key === shortModel.key ? "eastus" : "westus"] })],
  }))
  await mismatch.find(node => node.type?.name === "ModelStep").props.onChoose(candidate)
  assert.equal(mismatch.find(node => node.props?.role === "alert").props.children.props.children, "未找到完整的分档价目。")
  assert.equal(mismatch.all().some(node => node.props?.className === "model-price-context-table"), false)
  assert.throws(() => mismatch.payload(), /context_pricing_preview/)
})

test("context pricing can save complete references only when supported and restores its snapshot", () => {
  const base = savedModel({ context_price_tiers: [0, 1].map(index => ({
    key: `tier-${index}`, label: `Tier ${index}`, reference: `azure_retail:Unit:opaque-${index}:Global:eastus`,
    min_input_tokens: index ? 1001 : 0, max_input_tokens: index ? null : 1000,
    input_price_per_million: index ? 6.4 : 3.2, output_price_per_million: index ? 24 : 16,
    cached_price_per_million: index ? 0.64 : 0.32, cache_write_price_per_million: index ? 8 : 4,
    list_prices: { input: index ? 8 : 4, output: index ? 30 : 20, cached: index ? 0.8 : 0.4, cache_write: index ? 10 : 5 },
    model_key: `azure_retail:Unit:opaque-${index}`, model_label: `Opaque ${index}`, product: "Unit",
    group_key: "model", service_tier: "Standard", deployment: "Global", regions: ["eastus"],
  })), price_source: "azure_retail", price_discount_percent: 80,
    input_cost_per_million: null, output_cost_per_million: null, cached_cost_per_million: null, cache_write_cost_per_million: null,
  })
  const unsupported = modelEditForm.createModelEditDraft(base)
  assert.equal(validateModelEdit(unsupported), "context_pricing_preview")
  const draft = modelEditForm.createModelEditDraft(base, true)
  assert.equal(validateModelEdit(draft), null)
  assert.equal(modelEditHasChanges(draft, { ...draft }), false)
  const restored = modelEditForm.restoreContextPrices(base)
  assert.equal(restored.options[0].contextPricing.tiers.length, 2)
  assert.equal(restored.options[0].contextPricing.tiers[1].inputRange.min_input_tokens, 1001)
  const payload = modelEditPayload(base, { ...draft, discountPercent: "70" })
  assert.deepEqual(payload.context_price_references, base.context_price_tiers.map(tier => tier.reference))
  assert.equal(payload.price_reference, null)
  assert.equal(payload.input_cost_per_million, null)
  assert.equal(payload.price_discount_percent, 70)
  assert.equal(Object.hasOwn(payload, "context_price_tiers"), false)
  assert.equal(payload.context_price_refresh, false)
  const invalid = structuredClone(draft)
  invalid.contextPriceTiers[0].inputRange = null
  assert.equal(validateModelEdit(invalid), "context_pricing_incomplete")
})

test("context pricing saved schedule reopens offline without silently accepting a new rate", () => {
  const tiers = [0, 1].map(index => ({
    key: String(index), label: `Tier ${index}`, reference: `azure_retail:Unit:opaque-${index}:Global:eastus`,
    min_input_tokens: index ? 1001 : 0, max_input_tokens: index ? null : 1000,
    input_price_per_million: 2, output_price_per_million: 8, cached_price_per_million: 0.5, cache_write_price_per_million: 1,
    list_prices: { input: 2, output: 8, cached: 0.5, cache_write: 1 },
    model_key: `azure_retail:Unit:opaque-${index}`, model_label: `Opaque ${index}`, product: "Unit", group_key: "unit-model",
    service_tier: "Standard", deployment: "Global", regions: ["eastus"],
  }))
  const model = savedModel({context_price_tiers:tiers,price_source:"azure_retail",price_reference:"azure_retail:context:digest"})
  let reads = 0
  const harness = priceSourceHarness(() => { reads += 1; throw new Error("offline") }, model)
  assert.ok(harness.find(node => node.props?.className === "model-price-context-table"))
  assert.equal(reads, 0)
  assert.equal(harness.draft.contextPriceRefresh, false)
  assert.deepEqual(harness.draft.contextPriceReferences, tiers.map(tier => tier.reference))
  harness.find(node => node.props?.id === "model-discount").props.onChange({target:{value:"80"}})
  harness.all()
  assert.equal(harness.draft.contextPriceRefresh, true)
  assert.equal(reads, 0)
})

test("context pricing coverage uses tier schedules and never reprices aggregates as one request", async () => {
  const pricing = await import("../../../frontend/src/lib/pricing.ts")
  const model = savedModel({input_cost_per_million:null,output_cost_per_million:null,context_price_tiers:[{},{}]})
  assert.equal(pricing.hasModelPrices(model),true)
  assert.equal(pricing.estimateBucketCost({key:model.id,label:model.display_name,input:999999,cached:0,output:10},[model]),null)
  assert.equal(pricing.estimateRequestCost({model_id:model.id,estimated_cost:1.234},[model]),1.234)
  assert.equal(pricing.estimateRequestCost({model_id:model.id,estimated_cost:0},[model]),null)
  assert.equal(pricing.estimateRequestCost({model_id:model.id,estimated_cost:0,cost_available:true},[model]),0)
})

test("recorded request costs survive incomplete pricing coverage and model removal", async () => {
  const pricing = await import("../../../frontend/src/lib/pricing.ts")
  const contextModel = savedModel({ input_cost_per_million: null, output_cost_per_million: null, context_price_tiers: [{}, {}] })
  const unpricedModel = savedModel({ id: "unpriced", model_key: "unpriced", input_cost_per_million: null, output_cost_per_million: null, context_price_tiers: [] })
  const request = { model_id: contextModel.id, model_name: contextModel.display_name, estimated_cost: 0.0000816 }

  assert.equal(pricing.estimateRequestCost(request, [contextModel, unpricedModel]), 0.0000816)
  assert.equal(pricing.estimateRequestCost(request, [unpricedModel]), 0.0000816)
  assert.equal(pricing.estimateRequestCost({ ...request, model_id: unpricedModel.id, model_name: unpricedModel.model_key, estimated_cost: 0 }, [unpricedModel]), null)
  assert.equal(pricing.priceRequests([request], [unpricedModel])[0].estimated_cost, 0.0000816)
})

test("context pricing selected region supplies that region's references for every tier", () => {
  const [candidate] = modelEditForm.groupContextPriceModels([contextCatalogModel("ShortCo"), contextCatalogModel("LongCo")])
  const responses = candidate.contextPricing.tiers.map(tier => catalogResponse({model_entry:tier.model,options:[catalogOption({
    reference: `${tier.model.key}:Global:eastus`, regions: ["eastus", "westus"],
    references_by_region: { eastus: `${tier.model.key}:Global:eastus`, westus: `${tier.model.key}:Global:westus` },
  })]}))
  const option = modelEditForm.pairContextPriceOptions(candidate, responses).options[0]
  const draft = applyCatalogOption(createModelEditDraft(savedModel()), "azure_retail",
    { ...option, reference: option.references_by_region.westus }, 80)
  assert.ok(draft.contextPriceReferences.every(reference => reference.endsWith(":westus")))
})

test("context pricing preview hides flat-rate editor inputs and remains unsavable", () => {
  const view = editorHarness(savedModel())
  view.find(node => node.type === "ModelPriceSourceFields").props.setDraft(current => ({
    ...current, priceSource: "azure_retail", contextPricingPreview: true,
  }))
  for (const suffix of ["input", "output", "cache-read", "cache-write"]) {
    assert.equal(view.find(node => node.props?.id === `unit-editor-${suffix}`), undefined)
  }
  assert.ok(view.find(node => node.props?.id === "unit-editor-context"))
  assert.equal(view.find(node => node.type === "Button" && node.props.type === "submit").props.disabled, true)
  view.find(node => node.type === "form").props.onSubmit({ preventDefault() {} })
  assert.equal(view.saved.length, 0)
  assert.equal(view.find(node => node.props?.role === "alert"), undefined)
})

test("context pricing preview stays blocked when one tier is missing", async () => {
  const shortModel = contextCatalogModel("ShortCo")
  const [candidate] = modelEditForm.groupContextPriceModels([shortModel])
  let reads = 0
  const harness = priceSourceHarness(() => { reads += 1; return catalogResponse() })
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(candidate)
  assert.equal(reads, 0)
  assert.equal(harness.find(node => node.props?.role === "alert").props.children.props.children, "未找到完整的分档价目。")
  assert.equal(harness.all().some(node => node.props?.className === "model-price-context-table"), false)
  assert.equal(harness.draft.contextPricingPreview, true)
  assert.throws(() => harness.payload(), /context_pricing_preview/)
})

function priceSourceHarness(response, model = savedModel(), searchResponse = { models: [], unavailable: [] }) {
  const slots = []
  const effects = []
  const pending = []
  const searches = []
  const props = { model, modelName: "", busy: false, connectionDiscount: 90 }
  let cursor = 0
  let effectCursor = 0
  let draft = { ...createModelEditDraft(model), priceSource: "azure_retail" }
  const imports = {
    react: {
      useState(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial
        return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value }]
      },
      useMemo: callback => callback(),
      useRef(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = { current: initial }
        return slots[index]
      },
      useEffect(callback, dependencies) {
        const index = effectCursor++
        const previous = effects[index]
        if (previous && dependencies.every((value, position) => Object.is(value, previous.dependencies[position]))) return
        effects[index] = { dependencies }
        pending.push(() => {
          previous?.cleanup?.()
          effects[index].cleanup = callback()
        })
      },
    },
    "react/jsx-runtime": frontendRequire("react/jsx-runtime"),
    "@base-ui/react/combobox": { Combobox: new Proxy({}, { get: (_target, part) => `Combobox${part}` }) },
    "../../locales/index": { useLocale: () => ({ locale: "zh-CN" }), translateForLocale: async value => value },
    "./model-edit-form": modelEditForm,
    "../../data-sources/apim/api": { dataSource: {
      priceCatalogOptions: async key => typeof response === "function" ? response(key) : response,
      priceCatalogModels: async query => { searches.push(query); return searchResponse },
    } },
  }
  const exports = {}
  runInNewContext(priceComponent, { exports, require: name => imports[name] ?? new Proxy({}, { get: (_target, key) => key }) })
  const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
    : value && typeof value === "object" ? [value, ...nodes(typeof value.type === "function" ? value.type(value.props) : value.props?.children)] : []
  const render = () => {
    cursor = 0
    effectCursor = 0
    const tree = exports.ModelPriceSourceFields({ ...props, draft, setDraft: updater => { draft = updater(draft) } })
    for (const effect of pending.splice(0)) effect()
    return nodes(tree)
  }
  return {
    props, searches,
    get draft() { return draft },
    payload: () => modelEditPayload(model, draft),
    all: render,
    find: predicate => render().find(predicate),
    async flush() { await Promise.resolve(); await Promise.resolve(); return render() },
  }
}

test("price sources use the platform segmented buttons and preserve source changes", () => {
  const harness = priceSourceHarness(catalogResponse())
  const group = harness.find(node => node.type === "ButtonGroup" && node.props["aria-label"] === "价格来源")

  assert.ok(group)
  const sourceRow = harness.find(node => node.props?.className === "model-price-source-row")
  assert.equal(sourceRow.props.children[0].props.children[0].props.children, "价格来源")
  assert.equal(sourceRow.props.children[1].type, "ButtonGroup")
  assert.equal(group.props["data-value"], "azure_retail")
  assert.ok(group.props.className.includes("model-tabs"))
  assert.ok(group.props.className.includes("usage-metric-segment"))
  assert.equal(harness.find(node => node.type === "button" && node.props["aria-label"] === "Azure 零售价").props["aria-pressed"], true)
  assert.equal(harness.find(node => node.type === "button" && node.props["aria-label"] === "Azure 零售价").props.className, "active")
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  assert.equal(harness.draft.priceSource, "manual")
  assert.equal(harness.all().some(node => node.type === "input" && node.props.type === "radio"), false)
})

test("catalog fields put labels above controls and keep discount on its own row", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness(response)
  const model = harness.find(node => node.props?.className === "registry-field"
    && node.props.children[1]?.type === "ComboboxRoot")
  assert.equal(model.props.children[0].props.className, "registry-field-label-row")
  const discount = harness.find(node => node.props?.className === "registry-field"
    && node.props.children[1]?.props?.className === "model-price-discount-input")
  assert.equal(discount.props.children[0].props.className, "registry-field-label-row")
  assert.equal(discount.props.children.length, 2)
  assert.equal(harness.find(node => node.type === "label" && node.props.htmlFor === "model-price-model").props.children, "模型")
  assert.ok(harness.find(node => node.type === "FieldHelp" && node.props.children.includes("这是官方价目里的模型")))
  assert.equal(harness.find(node => node.type === "label" && node.props.htmlFor === "model-discount").props.children, "折扣")
  let fields = harness.find(node => node.props?.className === "model-price-catalog-fields")
  assert.deepEqual(Array.from(fields.props.children.filter(Boolean), node => node.type.name), ["ModelStep"])

  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  fields = harness.find(node => node.props?.className === "model-price-catalog-fields")
  assert.deepEqual(Array.from(fields.props.children.filter(Boolean), node => node.type.name), ["ModelStep", "DeploymentStep"])
  const following = harness.find(node => node.props?.className === "model-price-follow")
  assert.equal(following.props.children[0].props.className, "model-price-catalog-fields")
  assert.equal(following.props.children.filter(node => node?.type?.name === "DiscountField").length, 1)
  assert.equal(harness.all().some(node => node.props?.className === "model-price-inline-field"), false)
})

for (const otherMeters of [[], ["gpt 4.1 Batch Input", "gpt 4.1 Fine Tuning"]]) {
  test(`other meters expand at the bottom of the price card with ${otherMeters.length} entries`, async () => {
    const response = catalogResponse({ other_meters: otherMeters, unreadable: ["unrecognized meter"] })
    const harness = priceSourceHarness(response)
    await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
    const draftBefore = { ...harness.draft }
    const pricing = harness.find(node => node.props?.className === "model-price-arithmetic")
    const header = pricing.props.children[0]
    assert.equal(header.props.className, "registry-field-label-row")
    assert.equal(header.props.children[0].props.children, "实际单价")
    const details = pricing.props.children.at(-1)
    if (otherMeters.length) {
      assert.equal(details.type, "details")
      assert.equal(details.props.className, "model-editor-advanced")
      assert.equal(details.props.open, undefined)
      const summary = details.props.children[0]
      assert.equal(summary.type, "summary")
      assert.equal(summary.props.children[0].props.children, "计的是别的东西的计量表")
      assert.equal(summary.props.children[1].props.children, otherMeters.length)
      assert.equal(summary.props.children[1].props["data-no-localize"], true)
      const body = details.props.children[1]
      assert.equal(body.props.className, "model-editor-advanced-body")
      const items = body.props.children[1]
      assert.deepEqual(Array.from(items, node => node.props.children), otherMeters)
      assert.ok(items.every(node => node.type === "code" && node.props["data-no-localize"] === true))
    } else {
      assert.equal(details, false)
    }
    assert.equal(harness.all().some(node => node.type === "Popover" || node.type === "DropdownMenu"), false)
    const disclosures = harness.all().filter(node => node.type === "details")
    assert.equal(disclosures.length, otherMeters.length ? 2 : 1)
    assert.equal(disclosures.at(-1).props.children[0].props.children[1].props.children, "未能识别的计量表")
    assert.deepEqual({ ...harness.draft }, draftBefore)
  })
}

test("model input contains the search action before the dropdown arrow", () => {
  const harness = priceSourceHarness(catalogResponse())
  const field = harness.find(node => node.props?.className === "model-price-search")
  assert.equal(field.props.children[0].type, "ComboboxInput")
  const actions = field.props.children[1]
  assert.equal(actions.props.className, "model-price-input-actions")
  assert.equal(actions.props.children[0].type, "Button")
  assert.equal(actions.props.children[0].props["aria-label"], "搜索")
  assert.equal(actions.props.children[0].props.variant, "ghost")
  assert.equal(actions.props.children[1].type, "ComboboxTrigger")
  assert.equal(actions.props.children[1].props.render.props.variant, "ghost")
  assert.equal(actions.props.children[1].props.children.type, "ChevronDown")
})

test("selecting Azure searches the deployment name automatically without choosing a price", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness(response, savedModel(), {
    models: [response.model_entry, { ...response.model_entry, source: "anthropic", key: "other" }],
    unavailable: [],
  })
  harness.props.model = undefined
  harness.props.modelName = "GPT-5.6-Luna"
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  harness.find(node => node.type === "button" && node.props["aria-label"] === "Azure 零售价").props.onClick()
  await harness.flush()
  assert.deepEqual(harness.searches, ["GPT-5.6-Luna"])
  const picker = harness.find(node => node.type === "ComboboxRoot")
  assert.equal(picker.props.inputValue, "GPT-5.6-Luna")
  assert.equal(picker.props.open, true)
  assert.equal(picker.props.items.length, 1)
  assert.equal(picker.props.items[0].source, "azure_retail")
  assert.equal(picker.props.value, null)
  assert.equal(harness.draft.priceReference, "")
  picker.props.onInputValueChange("custom keyword", { reason: "input-change" })
  harness.props.modelName = "another-deployment"
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, "custom keyword")
  assert.equal(harness.searches.length, 1)
})

test("an empty deployment does not trigger automatic catalog enumeration", async () => {
  const harness = priceSourceHarness(catalogResponse())
  harness.props.model = undefined
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  harness.find(node => node.type === "button" && node.props["aria-label"] === "Azure 零售价").props.onClick()
  await harness.flush()
  assert.deepEqual(harness.searches, [])
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, "")
})

test("catalog candidates are portaled below the model input instead of extending the form", async () => {
  const response = catalogResponse()
  const candidates = [response.model_entry, { ...response.model_entry, key: "second", label: "gpt 4.1 mini" }]
  const harness = priceSourceHarness(response, savedModel(), { models: candidates, unavailable: [] })
  const before = harness.find(node => node.type?.name === "ModelStep")
  assert.equal(before.props.matches, null)
  harness.find(node => node.type === "Button" && node.props["aria-label"] === "搜索").props.onClick()
  await harness.flush()
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.open, true)
  assert.equal(harness.all().some(node => node.type === "ul" && node.props.className === "model-price-results"), false)
  assert.ok(harness.find(node => node.type === "ComboboxPortal"))
  assert.equal(harness.find(node => node.type === "ComboboxPositioner").props.align, "start")
  assert.equal(harness.find(node => node.type === "ComboboxPositioner").props.className, "select-positioner")
  assert.equal(harness.find(node => node.type === "ComboboxPositioner").props.sideOffset, 4)
  assert.equal(harness.find(node => node.type === "ComboboxPositioner").props.anchor,
    harness.find(node => node.type === "ComboboxInput").props.ref)
  assert.equal(harness.find(node => node.type === "ComboboxPopup").props["data-slot"], "select-content")
  assert.equal(harness.all().filter(node => node.type === "ComboboxItem").length, 2)
  assert.ok(harness.all().filter(node => node.type === "ComboboxItem").every(node => node.props["data-slot"] === "select-item"))
  assert.ok(harness.find(node => node.type === "span" && node.props["data-slot"] === "select-item-text"))
  const icons = harness.all().filter(node => node.type === "ModelBrandLogo")
  assert.equal(icons.length, candidates.length)
  for (const [index, icon] of icons.entries()) {
    assert.equal(icon.props.identity, `${candidates[index].label} ${candidates[index].product}`)
    assert.equal(icon.props.providerName, candidates[index].product)
    assert.equal(icon.props.size, 16)
  }
  harness.find(node => node.type === "ComboboxRoot").props.onValueChange(candidates[0])
  await harness.flush()
  assert.equal(harness.draft.priceReference, response.options[0].reference)
  const selected = harness.find(node => node.type === "ComboboxRoot")
  assert.equal(selected.props.value.key, candidates[0].key)
  assert.equal(selected.props.inputValue, candidates[0].label)
  assert.equal(selected.props.open, false)
  assert.equal(harness.all().some(node => node.type === "button" && node.props.children === "重新选择"), false)
})

test("editing the query invalidates stale catalog candidates and delayed search responses", async () => {
  let finish
  const response = catalogResponse()
  const harness = priceSourceHarness(response, savedModel(), new Promise(resolve => { finish = resolve }))
  harness.find(node => node.type === "Button" && node.props["aria-label"] === "搜索").props.onClick()
  harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("new search", { reason: "input-change" })
  finish({ models: [response.model_entry], unavailable: [] })
  await harness.flush()
  assert.equal(harness.find(node => node.type?.name === "ModelStep").props.matches, null)
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.open, false)
  assert.equal(harness.draft.priceReference, "")
})

test("closing the catalog popup retains its query and candidate selection does not cancel price loading", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness(response)
  const picker = harness.find(node => node.type === "ComboboxRoot")
  for (const reason of ["focus-out", "escape-key", "item-press", "input-clear"]) {
    picker.props.onInputValueChange("", { reason })
    assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, picker.props.inputValue)
  }
  picker.props.onValueChange(response.model_entry)
  picker.props.onInputValueChange(response.model_entry.label, { reason: "item-press" })
  await harness.flush()
  assert.equal(harness.draft.priceReference, response.options[0].reference)
})

test("backspacing a default deployment query preserves each edit through popup resets", () => {
  const deployment = "gpt-4.1-845778"
  const harness = priceSourceHarness(catalogResponse(), savedModel({ display_name: deployment }))
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, deployment)

  for (let length = deployment.length - 1; length >= 0; length--) {
    const edited = deployment.slice(0, length)
    const picker = harness.find(node => node.type === "ComboboxRoot")
    picker.props.onInputValueChange(edited, { reason: "input-change" })
    harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("", { reason: "input-clear" })
    assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, edited)
    assert.equal(harness.draft.priceReference, "")
  }

  harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("gpt-4.1", { reason: "input-change" })
  harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("", { reason: "clear-press" })
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, "")
})

test("deployment dropdown uses the global Select and applies the existing catalog pricing handler", async () => {
  const regional = catalogOption({
    deployment: "Regional", reference: "azure_retail:Azure OpenAI:gpt 4.1:Regional:westus",
    input_per_million: 3, regions: ["westus"], region_required: true,
  })
  const response = catalogResponse({ options: [catalogOption(), regional] })
  const harness = priceSourceHarness(response)
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  const group = harness.find(node => node.type === "Select" && node.props.name === "price-deployment")

  const trigger = () => harness.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "计价方式")
  assert.equal(trigger().props.children.props.children.props.children, "Global · $2.00 / $8.00")
  assert.equal(trigger().props.title, "Global · $2.00 / $8.00")
  assert.equal(harness.find(node => node.type === "SelectContent").props.alignItemWithTrigger, false)
  group.props.onValueChange("Regional")
  assert.equal(harness.payload().price_reference, regional.reference)
  assert.equal(harness.payload().input_cost_per_million, 2.7)
  assert.equal(harness.find(node => node.props?.name === "price-deployment").props.value, "Regional")
  assert.equal(trigger().props.children.props.children.props.children, "Regional · $3.00 / $8.00")
  assert.equal(trigger().props.title, "Regional · $3.00 / $8.00")
  assert.equal(harness.all().some(node => node.type === "RadioGroup"), false)
})

test("editing the selected catalog model clears its reference without a separate reselect action", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness(response)
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  assert.equal(validateModelEdit(harness.draft), null)
  harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("gpt mini", { reason: "input-change" })
  harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("", { reason: "input-clear" })
  assert.equal(harness.draft.priceReference, "")
  assert.equal(validateModelEdit(harness.draft), "price_reference")
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, "gpt mini")
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.value, null)
  assert.equal(harness.find(node => node.type?.name === "DeploymentStep"), undefined)
})

test("a selected model can be searched again with Enter and replaced in the same dropdown", async () => {
  const first = catalogResponse()
  const second = catalogResponse({
    model_entry: { ...first.model_entry, key: "azure_retail:Azure OpenAI:gpt 4.1 mini", label: "gpt 4.1 mini" },
    options: [catalogOption({ reference: "azure_retail:Azure OpenAI:gpt 4.1 mini:Global:*", input_per_million: 0.4 })],
  })
  const harness = priceSourceHarness(
    key => key === first.model_entry.key ? first : second,
    savedModel(), { models: [second.model_entry], unavailable: [] },
  )
  harness.find(node => node.type === "ComboboxRoot").props.onValueChange(first.model_entry)
  await harness.flush()
  harness.find(node => node.type === "ComboboxRoot").props.onInputValueChange("mini", { reason: "input-change" })
  let prevented = false
  harness.find(node => node.type === "ComboboxInput").props.onKeyDown({
    key: "Enter", nativeEvent: { isComposing: false },
    currentTarget: { getAttribute: () => null }, preventDefault() { prevented = true },
  })
  await harness.flush()
  assert.equal(prevented, true)
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.open, true)
  harness.find(node => node.type === "ComboboxRoot").props.onValueChange(second.model_entry)
  await harness.flush()
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.inputValue, second.model_entry.label)
  assert.equal(harness.find(node => node.type === "ComboboxRoot").props.open, false)
  assert.equal(harness.payload().price_reference, second.options[0].reference)
  assert.equal(harness.payload().input_cost_per_million, 0.36)
})

test("late catalog responses cannot undo a price source change", async () => {
  let finish
  const response = catalogResponse()
  const pending = new Promise(resolve => { finish = resolve })
  const harness = priceSourceHarness(pending)
  const before = { ...harness.draft }
  const choosing = harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  harness.find(node => node.type === "button" && node.props["aria-label"] === "手工填写").props.onClick()
  finish(response)
  await choosing
  assert.equal(harness.draft.priceSource, "manual")
  assert.equal(harness.draft.priceReference, "")
  assert.equal(harness.draft.inputPrice, before.inputPrice)
  assert.equal(harness.draft.cacheReadPrice, before.cacheReadPrice)
})

test("pricing selectors reuse global tabs and dropdown styling without a separate selected view", () => {
  const declarations = (path, selector) => {
    const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL(path, import.meta.url), "utf8"))
    const rules = []
    stylesheet.walkRules(rule => {
      if (rule.selector === selector && rule.parent.type === "root") rules.push(rule)
    })
    assert.equal(rules.length, 1, selector)
    return Object.fromEntries(rules[0].nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value]))
  }
  const controls = "../../../frontend/src/styles/finops.css"
  assert.equal(declarations(controls, "section.simple-pricing")["padding-top"], "16px")
  const source = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-source-choice")
  assert.equal(source["justify-self"], "start")
  assert.equal(source["max-width"], "100%")
  assert.equal(source.display, undefined)
  assert.equal(source.padding, undefined)
  assert.equal(source.background, undefined)
  const discount = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-discount-input")
  assert.equal(discount.position, "relative")
  const discountInput = declarations("../../../frontend/src/styles/model-platform.css", ".registry-field .model-price-discount-input > input")
  assert.equal(discountInput["padding-inline-end"], "32px")
  const suffix = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-discount-input > .model-editor-unit")
  assert.equal(suffix.position, "absolute")
  assert.equal(suffix["inset-inline-end"], "10px")
  assert.equal(suffix.top, "50%")
  assert.equal(suffix.transform, "translateY(-50%)")
  assert.equal(suffix["pointer-events"], "none")
  const popup = declarations("../../../frontend/src/styles/model-platform.css", '[data-slot="select-content"].model-price-catalog-popup')
  assert.deepEqual(popup, { width: "var(--anchor-width)", "min-width": "0" })
  const input = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-search > input")
  assert.equal(input["padding-inline-end"], "68px")
  const actions = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-input-actions")
  assert.equal(actions.position, "absolute")
  assert.equal(actions["inset-inline-end"], "4px")
  assert.equal(actions.background, undefined)
  assert.equal(actions.border, undefined)
  const iconButton = declarations("../../../frontend/src/styles/model-platform.css", '.model-price-input-actions > [data-slot="button"]')
  assert.equal(iconButton.background, "transparent")
  assert.equal(iconButton.border, "0")
  assert.equal(iconButton["box-shadow"], undefined)
  const stylesheet = frontendRequire("postcss").parse(readFileSync(new URL("../../../frontend/src/styles/model-platform.css", import.meta.url), "utf8"))
  const sourceRow = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-source-row")
  assert.equal(sourceRow.display, "grid")
  assert.equal(sourceRow["grid-template-columns"], "minmax(0, 1fr)")
  const catalogFields = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-catalog-fields")
  assert.equal(catalogFields.display, "grid")
  assert.equal(catalogFields["grid-template-columns"], "minmax(0, 1fr)")
  assert.equal(catalogFields["align-items"], "start")
  assert.equal(catalogFields.gap, "16px")
  const desktopRows = []
  stylesheet.walkRules(rule => {
    if (rule.selector === ".model-price-catalog-fields" && rule.parent.type === "atrule") {
      assert.equal(rule.parent.params, "(min-width: 641px)")
      desktopRows.push(Object.fromEntries(rule.nodes.filter(node => node.type === "decl").map(node => [node.prop, node.value])))
    }
    if (rule.selector === ".model-price-source-row") assert.equal(rule.parent.type, "root")
    assert.equal(rule.selector.includes(".model-price-inline-field"), false)
    assert.equal(rule.selector.includes(".model-price-chosen"), false)
    assert.equal(rule.selector.includes(".model-price-options"), false)
    assert.equal(rule.selector.includes(".model-price-region"), false)
    if (rule.selector.includes(".model-price-catalog-")) {
      assert.ok([
        ".model-price-catalog-fields", '[data-slot="select-content"].model-price-catalog-popup',
      ].includes(rule.selector))
    }
  })
  assert.deepEqual(desktopRows, [{ "grid-auto-flow": "column", "grid-auto-columns": "minmax(0, 1fr)", "grid-template-columns": "minmax(0, 1fr)" }])
  const following = declarations("../../../frontend/src/styles/model-platform.css", ".model-price-follow")
  assert.equal(following["border-top"], undefined)
  assert.equal(following["padding-top"], "12px")
  assert.equal(following.gap, "16px")
})

for (const complete of [false, undefined]) {
  test(`an incomplete catalog (${complete}) cannot replace the model's typed prices`, async () => {
    const response = catalogResponse({ complete, options: [catalogOption({ cached_per_million: null })] })
    const harness = priceSourceHarness(response)
    const before = { ...harness.draft }

    await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)

    assert.deepEqual(JSON.parse(JSON.stringify(harness.draft)), before)
    assert.equal(validateModelEdit(harness.draft), "price_reference")
    assert.throws(() => harness.payload(), /price_reference/)
    assert.equal(harness.find(node => node.props?.name === "price-deployment").props.disabled, true)
    harness.find(node => node.type?.name === "DeploymentStep").props.onChoose(response.options[0])
    assert.deepEqual(JSON.parse(JSON.stringify(harness.draft)), before)
  })
}

test("partial prices do not overwrite saved buckets when a discount changes", async () => {
  const response = catalogResponse({ complete: false, options: [catalogOption({ cached_per_million: null })] })
  const model = savedModel({ price_source: "azure_retail", price_reference: response.options[0].reference })
  const harness = priceSourceHarness(response, model)
  harness.all()
  await harness.flush()

  const discount = harness.find(node => node.props?.id === "model-discount")
  assert.equal(discount.props.disabled, true)
  discount.props.onChange({ target: { value: "80" } })
  harness.all()

  for (const field of ["input_cost_per_million", "output_cost_per_million", "cached_cost_per_million", "cache_write_cost_per_million", "price_reference"]) {
    assert.equal(harness.payload()[field], model[field], field)
  }
})

test("complete catalog selection still applies the connection discount", async () => {
  const response = catalogResponse()
  const harness = priceSourceHarness(response)

  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)

  assert.equal(harness.payload().input_cost_per_million, 1.8)
  assert.equal(harness.payload().cached_cost_per_million, 0.45)
  assert.equal(validateModelEdit(harness.draft), null)
  assert.equal(harness.find(node => node.props?.name === "price-deployment").props.disabled, false)
})

test("the region follows pricing mode and survives saving, discount changes, and reopening", async () => {
  const references = Object.fromEntries(["eastus", "westus", "northeurope"].map(region => [region, `azure_retail:Azure OpenAI:gpt 4.1:Regional:${region}`]))
  const response = catalogResponse({ options: [
    catalogOption({ reference: references.eastus, deployment: "Regional", regions: ["eastus", "westus"], region_required: true, references_by_region: { eastus: references.eastus, westus: references.westus } }),
    catalogOption({ reference: references.northeurope, deployment: "Regional", regions: ["northeurope"], region_required: true, references_by_region: { northeurope: references.northeurope }, input_per_million: 3 }),
  ] })
  const harness = priceSourceHarness(response)
  await harness.find(node => node.type?.name === "ModelStep").props.onChoose(response.model_entry)
  const fields = harness.find(node => node.props?.className === "model-price-catalog-fields")
  assert.deepEqual(Array.from(fields.props.children.filter(Boolean), node => node.type.name), ["ModelStep", "DeploymentStep", "RegionStep"])
  const regionRow = harness.find(node => node.props?.className === "registry-field"
    && node.props.children[1]?.props?.name === "price-region")
  assert.equal(regionRow.props.children.length, 2)
  assert.equal(regionRow.props.children[0].props.children[0].props.children, "区域")
  const regionSelect = regionRow.props.children[1]
  assert.equal(regionSelect.type, "Select")
  assert.equal(regionSelect.props.disabled, false)
  assert.equal(regionSelect.props.children[1].props.alignItemWithTrigger, false)
  assert.equal(regionSelect.props.children[1].props.align, "start")
  const trigger = harness.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "区域")
  assert.equal(trigger.props.className, "registry-select-trigger")
  assert.equal(trigger.props.children.props.children.props.children, "eastus")
  assert.equal(harness.all().filter(node => node.type === "SelectGroup").length, 2)
  assert.equal(harness.all().filter(node => node.type === "SelectLabel").length, 2)
  assert.equal(harness.all().some(node => ["select", "option", "optgroup"].includes(node.type)), false)
  const westus = harness.find(node => node.type === "SelectItem" && node.props.children.props.children === "westus")
  assert.equal(westus.props.value, references.westus)
  regionSelect.props.onValueChange(null)
  assert.equal(harness.payload().price_reference, references.eastus)
  regionSelect.props.onValueChange(references.northeurope)
  assert.equal(harness.payload().price_reference, references.northeurope)
  assert.equal(harness.payload().input_cost_per_million, 2.7)
  const deploymentLabel = () => harness.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "计价方式")
    .props.children.props.children.props.children
  assert.equal(deploymentLabel(), "Regional · $3.00 / $8.00")
  harness.find(node => node.type === "Select" && node.props.name === "price-region").props.onValueChange(westus.props.value)
  assert.equal(harness.payload().price_reference, references.westus)
  assert.equal(deploymentLabel(), "Regional · $2.00 / $8.00")
  assert.equal(harness.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "区域")
    .props.children.props.children.props.children, "westus")
  harness.find(node => node.type === "Select" && node.props.name === "price-deployment").props.onValueChange("Regional")
  assert.equal(harness.payload().price_reference, references.westus)

  harness.find(node => node.props?.id === "model-discount").props.onChange({ target: { value: "80" } })
  harness.all()
  assert.equal(harness.payload().input_cost_per_million, 1.6)
  assert.equal(harness.payload().price_reference, references.westus)
  assert.equal(deploymentLabel(), "Regional · $2.00 / $8.00")

  const reopened = priceSourceHarness(response, savedModel(harness.payload()))
  reopened.all()
  await reopened.flush()
  assert.equal(reopened.find(node => node.type === "ComboboxRoot").props.inputValue, response.model_entry.label)
  assert.equal(reopened.find(node => node.type === "ComboboxRoot").props.value.key, response.model_entry.key)
  assert.equal(reopened.find(node => node.type === "Select" && node.props.name === "price-region").props.value, references.westus)
  assert.equal(reopened.find(node => node.type === "SelectTrigger" && node.props["aria-label"] === "区域")
    .props.children.props.children.props.children, "westus")
  reopened.props.busy = true
  assert.equal(reopened.find(node => node.type === "Select" && node.props.name === "price-region").props.disabled, true)
})
