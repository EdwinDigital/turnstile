import { LoaderCircle, Plug, Save, TriangleAlert, X } from "lucide-react"
import { useState } from "react"

import type {
  ApimTargetTopologyList,
  BrandKey,
  ModelConnectionCreate,
  ModelConnectionUpdate,
  ModelRegistry,
  ModelRuntime,
  ModelVendorKey,
} from "../../data-sources/apim/types"
import { apimGatewayProfiles } from "../../lib/gateway-topology"
import {
  GatewayBrandLogo,
  ProviderBrandLogo,
  gatewayBrandFromIdentity,
  providerBrandFromMetadata,
} from "../brand-logos"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog"
import { Input } from "../ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import {
  DatabricksAuthModeSwitch,
  FoundryAuthModeSwitch,
  type DatabricksAuthMode,
  type FoundryAuthMode,
} from "./foundry-auth-mode-switch"
import { FieldHelp } from "./field-help"
import { ModelVendorSelect } from "./model-vendor-select"
import { isDatabricksWorkspaceUrl } from "./model-publication-connections"
import { isOpenAICompatibleBaseUrl, modelVendorFromMetadata } from "./openai-compatible"

type ConnectionProviderOption = {
  value: string
  name: string
  brandKey: BrandKey
  existingId?: string
  openaiCompatible?: boolean
}

function providerOptionBrand(option: ConnectionProviderOption) {
  return providerBrandFromMetadata(option.brandKey, option.name)
}

function isFoundryProjectEndpoint(value: string) {
  try {
    const endpoint = new URL(value.trim())
    return endpoint.protocol === "https:"
      && endpoint.hostname.endsWith(".services.ai.azure.com")
      && /^\/api\/projects\/[a-zA-Z0-9._-]+\/?$/.test(endpoint.pathname)
      && !endpoint.search
      && !endpoint.hash
  } catch {
    return false
  }
}

function isMatchingFoundryInferenceEndpoint(projectValue: string, inferenceValue: string) {
  try {
    const project = new URL(projectValue.trim())
    const inference = new URL(inferenceValue.trim())
    const account = project.hostname.replace(/\.services\.ai\.azure\.com$/i, "")
    return Boolean(account)
      && inference.protocol === "https:"
      && [
        `${account}.openai.azure.com`,
        `${account}.services.ai.azure.com`,
      ].includes(inference.hostname.toLowerCase())
      && inference.pathname.replace(/\/$/, "") === "/openai/v1"
      && !inference.search
      && !inference.hash
  } catch {
    return false
  }
}

function isBedrockRuntimeUrl(value: string) {
  try {
    const endpoint = new URL(value.trim())
    return endpoint.protocol === "https:"
      && endpoint.hostname.startsWith("bedrock-runtime.")
      && endpoint.hostname.endsWith(".amazonaws.com")
      && (endpoint.pathname === "/" || endpoint.pathname === "")
      && !endpoint.search
      && !endpoint.hash
  } catch {
    return false
  }
}

export function ConnectionDialog({
  registry,
  topology = null,
  topologyLoading = false,
  topologyError = null,
  runtime,
  busy,
  error,
  onClose,
  onCreate,
  onUpdate,
  onAdopt,
}: {
  registry: ModelRegistry
  topology?: ApimTargetTopologyList | null
  topologyLoading?: boolean
  topologyError?: string | null
  runtime?: ModelRuntime
  busy: boolean
  error: string | null
  onClose: () => void
  onCreate: (value: ModelConnectionCreate) => void
  onUpdate: (value: ModelConnectionUpdate) => void
  onAdopt?: (workspaceUrl: string) => void
}) {
  const editing = Boolean(runtime)
  const [adopting, setAdopting] = useState(false)
  const runtimeProvider = runtime
    ? registry.providers.find((provider) => provider.id === runtime.provider_id)
    : undefined
  const providers = runtimeProvider
    ? [runtimeProvider]
    : registry.providers.filter(
        (provider) => provider.enabled
          && (["microsoft_foundry", "amazon_bedrock", "azure_databricks"].includes(provider.brand_key)
            || provider.provider_kind === "openai_compatible"),
      )
  const providerOptions: ConnectionProviderOption[] = [
    ...providers.map((provider) => ({
      value: provider.id,
      name: provider.name,
      brandKey: provider.brand_key as ConnectionProviderOption["brandKey"],
      existingId: provider.id,
      openaiCompatible: provider.provider_kind === "openai_compatible",
    })),
    ...(runtime ? [] : (["microsoft_foundry", "amazon_bedrock", "azure_databricks"] as const)
      .filter((brandKey) => !providers.some((provider) => provider.brand_key === brandKey))
      .map((brandKey) => ({
        value: `template:${brandKey}`,
        name: brandKey === "microsoft_foundry" ? "Microsoft Foundry" : brandKey === "azure_databricks" ? "Azure Databricks" : "Amazon Bedrock",
        brandKey,
      }))),
    ...(runtime ? [] : [{
      value: "template:openai_compatible",
        name: "OpenAI-compatible API",
      brandKey: "generic" as const,
      openaiCompatible: true,
    }]),
  ]
  const runtimeGateway = runtime
    ? registry.gateways.find((gateway) => gateway.id === runtime.gateway_profile_id)
    : undefined
  const gateways = runtime
    ? runtimeGateway ? [runtimeGateway] : []
    : apimGatewayProfiles(registry.gateways, topology, registry.apim_target_topology_available === true)
  const [providerValue, setProviderValue] = useState(
    runtime?.provider_id
      ?? providerOptions.find((provider) => provider.brandKey === "microsoft_foundry")?.value
      ?? providerOptions[0]?.value
      ?? "",
  )
  const [selectedGatewayId, setGatewayId] = useState<string | null>(runtime?.gateway_profile_id ?? null)
  const gatewayId = selectedGatewayId ?? gateways.find(gateway => gateway.is_default)?.id ?? gateways[0]?.id ?? ""
  const [foundryAuthMode, setFoundryAuthMode] = useState<FoundryAuthMode>(
    runtime?.config.auth_strategy === "named_value_api_key"
      ? "api_key"
      : "managed_identity",
  )
  const [projectEndpoint, setProjectEndpoint] = useState(
    typeof runtime?.config.project_endpoint === "string"
      ? runtime.config.project_endpoint
      : "",
  )
  const [inferenceEndpoint, setInferenceEndpoint] = useState(
    typeof runtime?.config.inference_endpoint === "string"
      ? runtime.config.inference_endpoint
      : "",
  )
  const [bedrockRuntimeUrl, setBedrockRuntimeUrl] = useState(
    typeof runtime?.config.backend_url === "string" ? runtime.config.backend_url : "",
  )
  const [openaiBaseUrl, setOpenaiBaseUrl] = useState(
    typeof runtime?.config.base_url === "string" ? runtime.config.base_url : "",
  )
  const [modelVendor, setModelVendor] = useState<ModelVendorKey>(() =>
    modelVendorFromMetadata(runtime?.config ?? runtimeProvider?.config, runtime?.name ?? ""),
  )
  const [connectionName, setConnectionName] = useState(runtime?.name ?? "")
  const [workspaceUrl, setWorkspaceUrl] = useState(
    typeof runtime?.config.workspace_url === "string" ? runtime.config.workspace_url : "",
  )
  const [databricksAuthMode, setDatabricksAuthMode] = useState<DatabricksAuthMode>(
    runtime?.config.auth_strategy === "oauth_client_credentials" ? "oauth_m2m" : "managed_identity",
  )
  const [oauthClientId, setOAuthClientId] = useState(() => {
    const oauth = runtime?.config.oauth
    return oauth && typeof oauth === "object" && "client_id" in oauth
      && typeof oauth.client_id === "string" ? oauth.client_id : ""
  })
  const [enabled, setEnabled] = useState(runtime?.enabled ?? true)
  // Kept as a string so an empty box stays empty rather than becoming a zero discount.
  const [discountPercent, setDiscountPercent] = useState(
    runtime?.price_discount_percent?.toString() ?? "",
  )
  const [formError, setFormError] = useState<string | null>(null)

  const provider = providerOptions.find((item) => item.value === providerValue)
  const gateway = gateways.find((item) => item.id === gatewayId)
  const topologyBlocked = !editing && registry.apim_target_topology_available === true
    && (topologyLoading || Boolean(topologyError) || !topology)
  const openaiCompatible = provider?.openaiCompatible === true
  const foundry = provider?.brandKey === "microsoft_foundry"
    || runtime?.runtime_kind === "foundry"
  const bedrock = provider?.brandKey === "amazon_bedrock"
  const databricks = provider?.brandKey === "azure_databricks"
  const legacyDatabricks = databricks && runtime && runtime.config.control_plane_managed !== true
  const databricksUnavailable = databricks && (!editing || adopting)
    && (registry.databricks_connections_supported !== true
      || databricksAuthMode === "oauth_m2m" && registry.databricks_oauth_supported !== true)

  const chooseFoundryAuthMode = (mode: FoundryAuthMode) => {
    setFoundryAuthMode(mode)
    if (mode === "managed_identity") setInferenceEndpoint("")
    setFormError(null)
  }

  const submit = () => {
    if (busy) return
    if (adopting) {
      if (!onAdopt || databricksUnavailable) return setFormError("后端尚未支持 Databricks 连接。")
      if (!isDatabricksWorkspaceUrl(workspaceUrl)) return setFormError("请输入有效的 Databricks Workspace URL。")
      setFormError(null)
      onAdopt(workspaceUrl.trim())
      return
    }
    if (runtime) {
      if (!connectionName.trim()) return setFormError("请输入连接名称。")
      const discount = discountPercent.trim()
      if (discount && (!Number.isFinite(Number(discount))
        || Number(discount) <= 0 || Number(discount) > 100)) {
        return setFormError("折扣须为 0–100 之间的数字，或留空表示不打折。")
      }
      setFormError(null)
      onUpdate({
        name: connectionName.trim(),
        enabled,
        is_default: enabled && runtime.is_default,
        price_discount_percent: discount ? Number(discount) : null,
      })
      return
    }
    if (topologyBlocked) return setFormError(topologyError ?? "加载资源拓扑")
    if (!provider) return setFormError("请选择提供方。")
    const templateBrand = openaiCompatible ? "openai_compatible" : provider.brandKey === "microsoft_foundry"
      || provider.brandKey === "amazon_bedrock"
      || provider.brandKey === "azure_databricks"
      ? provider.brandKey
      : null
    if (!templateBrand) {
      return setFormError("此提供方不支持托管连接创建。")
    }
    if (!gateway) return setFormError("没有可用的 Azure API Management 网关。")
    if (foundry && !isFoundryProjectEndpoint(projectEndpoint)) {
      return setFormError("请输入有效的 Foundry Project Endpoint。")
    }
    if (
      foundry
      && foundryAuthMode === "api_key"
      && !isMatchingFoundryInferenceEndpoint(projectEndpoint, inferenceEndpoint)
    ) {
      return setFormError("请输入与 Project 属于同一 Foundry 资源的 Inference Endpoint。")
    }
    if (bedrock && !isBedrockRuntimeUrl(bedrockRuntimeUrl)) {
      return setFormError("请输入有效的 Bedrock Runtime URL。")
    }
    if (openaiCompatible && !isOpenAICompatibleBaseUrl(openaiBaseUrl)) {
      return setFormError("请输入不含凭据、查询参数或片段的 HTTPS Base URL。")
    }
    if (databricks) {
      if (databricksUnavailable) return setFormError("后端尚未支持所选 Databricks 认证方式。")
      if (!isDatabricksWorkspaceUrl(workspaceUrl)) return setFormError("请输入有效的 Databricks Workspace URL。")
      if (databricksAuthMode === "oauth_m2m" && !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(oauthClientId.trim())) {
        return setFormError("请输入 Databricks 服务主体的 Client ID。")
      }
    }
    setFormError(null)
    onCreate({
      gateway_profile_id: gateway.id,
      provider: provider.existingId
        ? { existing_id: provider.existingId }
        : { template: templateBrand },
      auth_mode: databricks ? databricksAuthMode : foundry ? foundryAuthMode : undefined,
      foundry_project_endpoint: foundry ? projectEndpoint.trim() : undefined,
      foundry_inference_endpoint: foundry && foundryAuthMode === "api_key"
        ? inferenceEndpoint.trim()
        : undefined,
      bedrock_runtime_url: bedrock ? bedrockRuntimeUrl.trim() : undefined,
      openai_base_url: openaiCompatible ? openaiBaseUrl.trim() : undefined,
      model_vendor: openaiCompatible ? modelVendor : undefined,
      databricks_workspace_url: databricks ? workspaceUrl.trim() : undefined,
      oauth_client_id: databricks && databricksAuthMode === "oauth_m2m" ? oauthClientId.trim() : undefined,
    })
  }

  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog connection-dialog" finalFocus={false}>
      <div className="registry-editor connection-form">
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{adopting ? "接管 Databricks 连接" : editing ? "编辑连接" : "添加连接"}</DialogTitle>
        </DialogHeader>
        <button type="button" className="registry-editor-close" onClick={onClose} disabled={busy} aria-label="关闭"><X size={16} /></button>

        <div className="registry-editor-body connection-dialog-body">
          {topologyBlocked && <div className={`simple-target-state${topologyError ? " error" : ""}`} role={topologyError ? "alert" : "status"}>{topologyError ? <TriangleAlert size={15} /> : <LoaderCircle size={15} className="spin" />}<span>{topologyError ?? "加载资源拓扑"}</span></div>}
          <div className="form-grid">
            <div className="registry-field">
              <span className="registry-field-label-row"><span className="registry-field-label">{editing ? "API 服务商" : "接入类型"}</span>{editing && <FieldHelp>提供方、APIM 网关、Endpoint 和认证方式共同定义已发布路由。要更换连接身份，请添加新连接并迁移模型。</FieldHelp>}</span>
              <Select value={providerValue} onValueChange={(value) => {
                if (!value) return
                setProviderValue(value)
                const selected = registry.providers.find((item) => item.id === value)
                setModelVendor(modelVendorFromMetadata(selected?.config, selected?.name ?? ""))
                setFormError(null)
              }} disabled={busy || editing}>
                <SelectTrigger className="registry-select-trigger" aria-label={editing ? "API 服务商" : "接入类型"}>
                  <SelectValue>{provider ? <span className="registry-option">{provider.openaiCompatible ? <Plug size={15} /> : <ProviderBrandLogo brand={providerOptionBrand(provider)} size={15} />}<span>{provider.name}</span></span> : "选择接入类型"}</SelectValue>
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {providerOptions.map((item) => <SelectItem key={item.value} value={item.value}><span className="registry-option">{item.openaiCompatible ? <Plug size={15} /> : <ProviderBrandLogo brand={providerOptionBrand(item)} size={15} />}<span>{item.name}</span></span></SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="registry-field">
              <span className="registry-field-label">APIM 实例</span>
              {editing && !runtimeGateway ? <Input value="本地 CLI / 直连" disabled /> : <Select value={gatewayId} items={gateways.map(item => ({ value: item.id, label: item.name }))} onValueChange={(value) => { if (value) { setGatewayId(value); setFormError(null) } }} disabled={busy || editing || topologyBlocked}>
                <SelectTrigger className="registry-select-trigger" aria-label="APIM 实例">
                  <SelectValue>{gateway ? <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)} size={15} /><span>{gateway.name}</span></span> : "选择 APIM 网关"}</SelectValue>
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {gateways.map((item) => <SelectItem key={item.id} value={item.id}><span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${item.name} ${item.implementation}`)} size={15} /><span>{item.name}</span></span></SelectItem>)}
                </SelectContent>
              </Select>}
            </div>
          </div>

          {editing && !adopting && <label className="registry-field"><span className="registry-field-label">连接名称</span><Input value={connectionName} onChange={(event) => setConnectionName(event.target.value)} disabled={busy} /></label>}

          {databricks && <>
            <label className="registry-field"><span className="registry-field-label">Workspace URL</span><Input type="url" value={workspaceUrl} onChange={(event) => setWorkspaceUrl(event.target.value)} disabled={busy || editing && !adopting} placeholder={legacyDatabricks && !adopting ? "尚未接管" : "https://adb-example.1.azuredatabricks.net"} /></label>
            <div className="registry-field">
              <span className="registry-field-label-row"><span className="registry-field-label">认证方式</span><FieldHelp>{databricksAuthMode === "oauth_m2m" ? "使用目标 Databricks 账户的服务主体。首个模型发布时提供 OAuth Secret，不是 Entra 应用密钥或 PAT。" : "APIM 托管身份需要同租户的 Workspace 访问和目标模型查询权限。"}</FieldHelp></span>
              <DatabricksAuthModeSwitch value={databricksAuthMode} disabled={busy || editing} oauthSupported={registry.databricks_oauth_supported === true} onChange={setDatabricksAuthMode} />
            </div>
            {databricksAuthMode === "oauth_m2m" && <label className="registry-field"><span className="registry-field-label">Databricks Client ID</span><Input value={oauthClientId} onChange={(event) => setOAuthClientId(event.target.value)} disabled={busy || editing} autoComplete="off" /></label>}
            {databricksUnavailable && <p className="publication-form-note" role="status">后端尚未支持所选 Databricks 认证方式。</p>}
            {adopting && <p className="publication-form-note">接管将验证现有模型并发布候选 Revision，会产生模型调用费用。模型身份、价格和访问分配保持不变。</p>}
          </>}

          {foundry && <>
            <label className="registry-field"><span className="registry-field-label">Project Endpoint</span><Input type="url" value={projectEndpoint} onChange={(event) => setProjectEndpoint(event.target.value)} disabled={busy || editing} placeholder="https://contoso-ai.services.ai.azure.com/api/projects/finops" /></label>
            <div className="registry-field">
              <span className="registry-field-label-row"><span className="registry-field-label">认证方式</span><FieldHelp>{foundryAuthMode === "api_key" ? "此处不收 API Key。添加首个模型时只需提供一次，后续模型复用此连接无需重复提供。" : "使用 Managed Identity 前，请先在目标 Foundry 资源（承载该 Project 的 Foundry Account）的 Access control (IAM) 中，将当前所选 APIM 的 Managed Identity 授予 Cognitive Services User。仅位于同一租户不会自动获得访问权限。连接可先保存；添加首个模型时会验证权限。若缺少权限，发布会暂停并显示 APIM Principal ID 和目标资源，完成授权后再继续验证。"}</FieldHelp></span>
              <FoundryAuthModeSwitch value={foundryAuthMode} disabled={busy || editing} onChange={chooseFoundryAuthMode} />
            </div>
            {foundryAuthMode === "api_key" && <label className="registry-field"><span className="registry-field-label">Inference Endpoint</span><Input type="url" value={inferenceEndpoint} onChange={(event) => setInferenceEndpoint(event.target.value)} disabled={busy || editing} placeholder="https://contoso-ai.openai.azure.com/openai/v1" /></label>}
          </>}

          {bedrock && <>
            <div className="registry-field"><span className="registry-field-label-row"><label className="registry-field-label" htmlFor="bedrock-runtime-url">Bedrock Runtime URL</label><FieldHelp>首次为此连接添加模型时，再提供一次性 API Key。</FieldHelp></span><Input id="bedrock-runtime-url" type="url" value={bedrockRuntimeUrl} onChange={(event) => setBedrockRuntimeUrl(event.target.value)} disabled={busy || editing} placeholder="https://bedrock-runtime.ap-southeast-2.amazonaws.com" /></div>
          </>}

          {openaiCompatible && <div className="openai-compatible-identity-grid">
            <div className="registry-field"><span className="registry-field-label-row"><span className="registry-field-label">API 服务商</span><FieldHelp>选择提供此 API 的服务商，而不是模型原厂。此选项用于连接身份和品牌展示，不会改变 API 协议。</FieldHelp></span>
              <ModelVendorSelect value={modelVendor} onChange={setModelVendor} disabled={busy || editing} />
            </div>
            <div className="registry-field"><span className="registry-field-label-row">
              <label className="registry-field-label" htmlFor="openai-base-url">API Base URL</label>
              <FieldHelp>填写提供方的 HTTPS API Base URL。首次为此连接发布模型时，再提供一次性 API Key。</FieldHelp>
            </span><Input id="openai-base-url" type="url" value={openaiBaseUrl}
              onChange={(event) => setOpenaiBaseUrl(event.target.value)} disabled={busy || editing}
              placeholder="https://api.example.com/v1" />
            </div>
          </div>}

          {editing && !adopting && <div className="form-switches"><div className="registry-checkbox-field"><Checkbox id="connection-enabled" checked={enabled} onCheckedChange={(checked) => setEnabled(checked === true)} disabled={busy} /><label htmlFor="connection-enabled">启用</label></div></div>}
          {editing && !adopting && <div className="registry-field">
            <div className="registry-field-label-row">
              <label htmlFor="connection-discount" className="registry-field-label">默认折扣</label>
              <span className="model-editor-unit" data-no-localize>%</span>
              <FieldHelp>
                该连接下「跟随官方价」的模型，实际单价 = 官方价 × 此折扣。90 表示九折。
                留空表示按官方价原价计费。单个模型可以单独覆盖。
              </FieldHelp>
            </div>
            <Input id="connection-discount" type="number" inputMode="decimal"
              min={0} max={100} step="any" disabled={busy} value={discountPercent}
              placeholder="留空则不打折" onChange={(event) => setDiscountPercent(event.target.value)} />
          </div>}

          {(formError || error) && <div className="registry-error" role="alert">{formError ?? error}</div>}
        </div>

        <DialogFooter className="registry-editor-footer">
          {legacyDatabricks && !adopting && onAdopt && <Button type="button" variant="outline" className="connection-adopt-action" onClick={() => setAdopting(true)} disabled={busy || registry.databricks_connections_supported !== true}>接管连接</Button>}
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>取消</Button>
          <Button type="button" onClick={submit} disabled={busy || topologyBlocked || databricksUnavailable || !providerOptions.length || (!editing && !gateway)}>{editing && !adopting && <Save size={14} />}{busy ? "正在保存" : adopting ? "验证并接管" : editing ? "保存更改" : "添加连接"}</Button>
        </DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
}
