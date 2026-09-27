import { useEffect, useId, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowRightLeft, Boxes, CheckCircle2, ChevronRight, CircleAlert, Download, Link2, MoreHorizontal, PanelLeft, Pencil, Plus, RefreshCw, Save, Search, Server, Trash2, Wifi, WifiOff, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import { finopsKeys, finopsQueries, storeApimTarget, storeGatewayDeploymentName, storeGatewayName, storeRegistry } from "../../data-sources/apim/queries"
import type { ApimTargetRemovalPreview, ApimTargetTopology, ApimTargetTopologyList, ApimTargetWrite, GatewayReleaseOperation, ModelRegistry, SelfHostedGatewayAssociationChange, SelfHostedGatewayRemovalPreview, SelfHostedGatewayWrite } from "../../data-sources/apim/types"
import { apimFormPayload, apimFormValid, gatewayFormPayload, gatewayFormValid, gatewayNameFormValid, isGatewayEndpoint } from "../../lib/gateway-forms"
import { MODEL_MANAGEMENT_TABS, MODEL_MANAGEMENT_TAB_LABELS as tabLabels, apimGatewayEntries, apimOnboardingDetails, apimOnboardingOperationName, apimOnboardingRows, apimTargetDisplayName, gatewayEntries, gatewayEvidence, pendingInventorySyncId, type GatewayEntry, type ModelManagementTab } from "../../lib/gateway-topology"
import { useLocale } from "../../locales"
import { useAuth } from "../../providers/auth-provider"
import { useTimezone } from "../../providers/timezone-provider"
import { ApimLogo, ManagedGatewayLogo } from "../brand-logos"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogTitle } from "../ui/alert-dialog"
import { Button } from "../ui/button"
import { ButtonGroup } from "../ui/button-group"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../ui/dropdown-menu"
import { ExpandableSearch } from "../ui/expandable-search"
import { Input } from "../ui/input"
import { ResizableGridTable } from "../ui/resizable-table"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"
import { ApimOnboardingDialog, ApimOnboardingRow, pendingApimOnboarding } from "./apim-onboarding-dialog"
import { SelfHostedGatewayBootstrapDialog, SelfHostedGatewayCreateDialog, SelfHostedGatewayProvisionProgress } from "./self-hosted-gateway-dialog"

type InfrastructureTab = Extract<ModelManagementTab, "apim" | "gateways">
type RemovalTarget = { kind: "apim" | "gateway"; id: string; name: string }
type RemovalPreview = ApimTargetRemovalPreview | SelfHostedGatewayRemovalPreview
type GatewayProvisionItem = { operation: GatewayReleaseOperation; target: ApimTargetTopology | null }
const resourceTableColumnMinWidths = [180, 120, 160, 80, 136] as const
const gatewayHostingLabels: Record<string, string> = {
  aks: "AKS", kubernetes: "Kubernetes", arc: "Azure Arc", container_apps: "Azure Container Apps",
  docker: "Docker", azure_managed: "Azure 托管", other: "其他环境",
}
const emptyApimForm: ApimTargetWrite = {
  name: "", management_endpoint: "", tenant_id: "", subscription_id: "", resource_group: "",
  service_name: "", api_id: "", gateway_endpoint: "", management_auth_profile_ref: "system_managed_identity",
  dashboard_subscription_id: "turnstile-dashboard", probe_subscription_id: "turnstile-publisher-probe",
}
const emptyGatewayForm: SelfHostedGatewayWrite = {
  native_gateway_id: "", display_name: "", endpoint_url: "", location: "", hosting_mode: "kubernetes", release_gate: "required",
}
const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const publishedModelSummary = (count: number, apiId: string) => `${count} 个已发布模型 · __TARGET_API__`.replace("__TARGET_API__", apiId)
const gatewayPublishedModels = (entry: GatewayEntry) => entry.published_models ?? []
const provisionMatchesGateway = (operation: GatewayReleaseOperation, entry: GatewayEntry) =>
  entry.deployment.provisioning_operation_id === operation.id
  || entry.deployment.id === operation.checkpoint.deployment_id
  || (entry.target.target.gateway_profile_id === operation.gateway_profile_id
    && entry.deployment.native_gateway_id.toLowerCase() === String(operation.semantic_preview.native_gateway_id ?? "").toLowerCase())

function InfrastructureErrorNotice({ children }: { children: React.ReactNode }) {
  return <div className="registry-error smh-notice infrastructure-error-notice" role="alert"><CircleAlert size={14} aria-hidden="true" /><div className="smh-notice-copy"><span>{children}</span></div></div>
}

export function ApimGatewayWorkspace({ mode, registry, topology, loading, error, onTabChange, onToggleSidebar }: {
  mode: InfrastructureTab; registry: ModelRegistry; topology: ApimTargetTopologyList | null
  loading: boolean; error: unknown; onTabChange: (tab: ModelManagementTab) => void; onToggleSidebar: () => void
}) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const [search, setSearch] = useState("")
  const [apimOpen, setApimOpen] = useState(false)
  const [adoptOpen, setAdoptOpen] = useState(false)
  const [gatewayOpen, setGatewayOpen] = useState(false)
  const [createGatewayOpen, setCreateGatewayOpen] = useState(false)
  const [startupGatewayId, setStartupGatewayId] = useState<string | null>(null)
  const [gatewayTargetId, setGatewayTargetId] = useState<string | null>(null)
  const [editingApim, setEditingApim] = useState<ApimTargetTopology | null>(null)
  const [editingGateway, setEditingGateway] = useState<GatewayEntry | null>(null)
  const [associationTarget, setAssociationTarget] = useState<GatewayEntry | null>(null)
  const [collapsedTargetIds, setCollapsedTargetIds] = useState<Set<string>>(() => new Set())
  const [removalTarget, setRemovalTarget] = useState<RemovalTarget | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [syncOperationId, setSyncOperationId] = useState<string | null>(() => pendingInventorySyncId(
    queryClient.getQueriesData<GatewayReleaseOperation>({ queryKey: finopsKeys.gatewayReleaseOperation("").slice(0, -1) })
      .flatMap(([, operation]) => operation ? [operation] : []),
  ))
  const notifiedOperation = useRef<string | null>(null)
  const pendingInitializations = useRef(new Set<string>())
  const pendingProvisions = useRef(new Set<string>())
  const provisions = useQuery({
    ...finopsQueries.selfHostedGatewayProvisions(),
    enabled: canManage && mode === "gateways" && topology !== null && "self_hosted_gateway_provisioning_available" in topology,
  })
  const onboarding = useQuery({
    ...finopsQueries.apimOnboarding(),
    enabled: canManage && mode === "apim" && topology !== null && "onboarding_available" in topology,
    refetchInterval: query => query.state.data?.items.some(pendingApimOnboarding) ? 1500 : false,
  })
  const operation = useQuery(finopsQueries.gatewayReleaseOperation(syncOperationId))
  const removal = useQuery({
    queryKey: ["control-plane", "resource-removal-preview", removalTarget?.kind, removalTarget?.id],
    queryFn: (): Promise<RemovalPreview> => {
      if (!removalTarget) throw new Error("No resource selected")
      return removalTarget.kind === "apim" ? dataSource.previewApimTargetRemoval(removalTarget.id)
        : dataSource.selfHostedGatewayRemovalPreview(removalTarget.id)
    },
    enabled: removalTarget !== null, staleTime: 0, retry: false,
  })
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: finopsKeys.registry })
    await queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets })
  }
  const addApim = useMutation({
    mutationFn: (value: ApimTargetWrite) => dataSource.registerApimTarget(value),
    onSuccess: async target => { await storeApimTarget(queryClient, target); setApimOpen(false); setAdoptOpen(false); setNotice("APIM 实例已验证并添加"); await refresh() },
  })
  const renameApim = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => dataSource.renameGateway(id, name),
    onSuccess: async (registry, value) => {
      if (registry) await storeRegistry(queryClient, registry)
      else await storeGatewayName(queryClient, value.id, value.name)
      setEditingApim(null)
      setNotice("APIM 显示名称已更新")
    },
  })
  const renameDeployment = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => dataSource.renameSelfHostedGateway(id, name),
    onSuccess: async (_, value) => { await storeGatewayDeploymentName(queryClient, value.id, value.name); setEditingGateway(null); setNotice("网关显示名称已更新") },
  })
  const refreshOnboarding = () => { void queryClient.invalidateQueries({ queryKey: finopsKeys.apimOnboarding }) }
  const refreshProvisions = () => { void queryClient.invalidateQueries({ queryKey: finopsKeys.selfHostedGatewayProvisions }) }
  const acceptedProvision = (current: GatewayReleaseOperation) => {
    pendingProvisions.current.add(current.id)
    queryClient.setQueryData(finopsKeys.gatewayReleaseOperation(current.id), current)
    setCreateGatewayOpen(false)
    if (mode !== "gateways") onTabChange("gateways")
    refreshProvisions()
  }
  const retryProvision = useMutation({
    mutationFn: (id: string) => dataSource.retrySelfHostedGatewayProvision(id),
    onSuccess: accepted => acceptedProvision(accepted.operation), onSettled: refreshProvisions,
  })
  const acceptedOnboarding = (current: GatewayReleaseOperation) => {
    pendingInitializations.current.add(current.id)
    queryClient.setQueryData(finopsKeys.gatewayReleaseOperation(current.id), current)
    setApimOpen(false)
    refreshOnboarding()
  }
  const retryOnboarding = useMutation({
    mutationFn: (id: string) => dataSource.retryApimOnboarding(id),
    onSuccess: accepted => acceptedOnboarding(accepted.operation), onSettled: refreshOnboarding,
  })
  const syncTarget = useMutation({
    mutationFn: dataSource.syncApimTarget,
    onSuccess: accepted => {
      queryClient.setQueryData(finopsKeys.gatewayReleaseOperation(accepted.operation.id), accepted.operation)
      setNotice(null)
      setSyncOperationId(accepted.operation.id)
    },
  })
  const addGateway = useMutation({
    mutationFn: ({ targetId, value }: { targetId: string; value: SelfHostedGatewayWrite }) => dataSource.addSelfHostedGateway(targetId, value),
    onSuccess: async (_, variables) => {
      setGatewayOpen(false)
      setCollapsedTargetIds(current => { const next = new Set(current); next.delete(variables.targetId); return next })
      setNotice("网关关联已保存，请同步 APIM 后验证连接")
      await refresh()
      if (topology?.inventory_sync_available) syncTarget.mutate(variables.targetId)
    },
  })
  const validateGateway = useMutation({
    mutationFn: (id: string) => dataSource.validateSelfHostedGateway(id),
    onSuccess: () => setNotice("网关连接验证成功"), onSettled: refresh,
  })
  const changeAssociation = useMutation({
    mutationFn: ({ deploymentId, value }: { deploymentId: string; value: SelfHostedGatewayAssociationChange }) => dataSource.changeSelfHostedGatewayAssociation(deploymentId, value),
    onSuccess: async () => { setAssociationTarget(null); setNotice("APIM 关联已更新，请同步目标 APIM 后重新验证"); await refresh() },
  })
  const remove = useMutation<RemovalPreview, Error, RemovalTarget>({
    mutationFn: target => target.kind === "apim" ? dataSource.removeApimTarget(target.id) : dataSource.removeSelfHostedGateway(target.id),
    onSuccess: async (_, target) => {
      const removed = target.kind === "gateway" ? gatewayEntries(topology).find(entry => entry.deployment.id === target.id) : null
      setRemovalTarget(null)
      setNotice(target.kind === "apim" ? "APIM 实例已从 Turnstile 移除，Azure 资源未更改" : "网关已从 Turnstile 移除，用户部署未更改")
      await refresh()
      if (removed && topology?.inventory_sync_available) syncTarget.mutate(removed.target.target.gateway_profile_id)
    },
  })
  useEffect(() => {
    const current = operation.data
    if (!current || !["succeeded", "failed", "restored"].includes(current.status) || notifiedOperation.current === current.id) return
    notifiedOperation.current = current.id
    void queryClient.invalidateQueries({ queryKey: finopsKeys.registry })
    void queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets })
    if (current.status === "succeeded") setNotice("APIM 清单同步完成")
  }, [operation.data, queryClient])
  useEffect(() => {
    let completed = false
    for (const current of onboarding.data?.items ?? []) {
      if (pendingApimOnboarding(current)) pendingInitializations.current.add(current.id)
      else if (pendingInitializations.current.delete(current.id) && current.status === "succeeded") completed = true
    }
    if (completed) {
      setNotice("APIM 初始化完成，托管网关已验证")
      void queryClient.invalidateQueries({ queryKey: finopsKeys.registry })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets })
    }
  }, [onboarding.data, queryClient])
  useEffect(() => {
    if (!canManage || mode !== "gateways") return
    for (const current of provisions.data?.items ?? []) {
      if (pendingApimOnboarding(current)) pendingProvisions.current.add(current.id)
      else if (pendingProvisions.current.delete(current.id) && current.status === "succeeded") {
        void queryClient.invalidateQueries({ queryKey: finopsKeys.registry })
        void queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets })
      }
    }
  }, [canManage, mode, provisions.data, queryClient])
  const initializations = apimOnboardingRows(canManage ? onboarding.data?.items ?? [] : [], topology?.items ?? [])
  const initializingIds = new Set(initializations.map(item => item.gateway_profile_id))
  const matches = (values: unknown[]) => values.join(" ").toLowerCase().includes(search.toLowerCase())
  const onboardingItems = initializations.filter(item => matches([apimOnboardingOperationName(item), ...Object.values(apimOnboardingDetails(item))]))
  const targets = (topology?.items ?? []).filter(item => !initializingIds.has(item.target.gateway_profile_id)).filter(item => matches([
    apimTargetDisplayName(item, registry.gateways), item.target.service_name, item.target.resource_group, item.target.subscription_id,
    item.target.api_id, item.management_endpoint, item.gateway_endpoint, item.configuration_endpoint,
    ...item.deployments.flatMap(entry => [entry.deployment.display_name, entry.deployment.native_gateway_id, entry.deployment.endpoint_url, entry.deployment.hosting_mode, ...entry.published_models.flatMap(model => [model.display_name, model.model_key])]),
  ]))
  const gatewayInventory = gatewayEntries(topology)
  const gatewayProvisions: GatewayProvisionItem[] = (canManage ? provisions.data?.items ?? [] : [])
    .filter(current => current.status !== "succeeded")
    .map(current => ({ operation: current, target: (topology?.items ?? []).find(item => item.target.gateway_profile_id === current.gateway_profile_id) ?? null }))
  const registeredGateways = gatewayInventory.filter(entry => !gatewayProvisions.some(item => provisionMatchesGateway(item.operation, entry)))
  const provisionItems = gatewayProvisions.filter(({ operation: current, target }) => {
    const configuration = current.semantic_preview.configuration as Partial<SelfHostedGatewayWrite> | undefined
    return matches([configuration?.display_name, configuration?.endpoint_url, configuration?.hosting_mode, current.semantic_preview.native_gateway_id, target?.target.service_name, target?.configuration_endpoint])
  })
  const gateways = registeredGateways.filter(entry => matches([entry.deployment.display_name, entry.deployment.native_gateway_id, entry.deployment.endpoint_url, entry.target.target.service_name, entry.configuration_endpoint, ...gatewayPublishedModels(entry).map(model => model.display_name)]))
  const syncing = Boolean(operation.data && !["succeeded", "failed", "restored"].includes(operation.data.status))
  const busy = addApim.isPending || renameApim.isPending || addGateway.isPending || syncTarget.isPending || syncing
    || renameDeployment.isPending || validateGateway.isPending || changeAssociation.isPending || remove.isPending
  const lifecycleAvailable = topology?.resource_lifecycle_available === true
  const gatewayAttachmentAvailable = lifecycleAvailable && registry.self_hosted_gateway_enabled === true
  const gatewayCreationAvailable = gatewayAttachmentAvailable && topology?.self_hosted_gateway_provisioning_available === true
  const gatewayNameSupported = canManage && lifecycleAvailable && topology?.self_hosted_gateway_name_edit_supported === true
  const topologyUnavailable = !loading && topology === null && Boolean(error)
  const counts: Record<ModelManagementTab, number | string> = {
    models: registry.models.length, connections: registry.runtimes.length,
    apim: topology ? new Set([...topology.items.map(item => item.target.gateway_profile_id), ...initializingIds]).size : "\u2014",
    gateways: topology ? registeredGateways.length + gatewayProvisions.length : "\u2014",
  }
  const openGateway = (targetId: string | null = null) => { setGatewayTargetId(targetId); setCreateGatewayOpen(true) }
  const openStartup = (entry: GatewayEntry) => { setStartupGatewayId(entry.deployment.id); if (mode !== "gateways") onTabChange("gateways") }
  const editGateway = (entry: GatewayEntry) => { renameDeployment.reset(); setEditingGateway(entry) }
  const openRemoval = (target: RemovalTarget) => { remove.reset(); setRemovalTarget(target) }
  const toggleTarget = (id: string) => setCollapsedTargetIds(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })

  return <div className="smh-workspace model-workspace infrastructure-workspace">
    <header className="smh-page-header"><div><Button type="button" variant="ghost" size="icon-sm" className="model-mobile-sidebar-toggle" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button><span className="smh-header-icon">{mode === "apim" ? <ApimLogo size={17} /> : <Boxes size={17} />}</span><h1>{tabLabels[mode]}</h1><span>{counts[mode]}</span></div>
      {canManage && <div className="smh-header-actions"><button type="button" disabled={busy || loading || !lifecycleAvailable || (mode === "gateways" && !gatewayAttachmentAvailable)} title={!lifecycleAvailable ? "当前 API 版本不支持资源生命周期操作" : undefined} onClick={() => mode === "apim" ? setApimOpen(true) : openGateway()}><Plus size={14} /><span>{mode === "apim" ? "添加 APIM 实例" : "添加网关"}</span></button></div>}
    </header>
    <div className="model-toolbar"><ButtonGroup className="model-tabs usage-metric-segment" aria-label="模型管理类型">{MODEL_MANAGEMENT_TABS.map(tab => <button key={tab} data-tab={tab} className={mode === tab ? "active" : ""} aria-pressed={mode === tab} onClick={() => onTabChange(tab)}>{tabLabels[tab]}<span>{counts[tab]}</span></button>)}</ButtonGroup><ExpandableSearch value={search} onChange={setSearch} placeholder={`搜索${tabLabels[mode]}...`} /></div>
    {notice && <div className="registry-notice smh-notice" role="status"><CheckCircle2 size={14} /><div className="smh-notice-copy"><span>{notice}</span></div><button type="button" className="smh-notice-close" title="关闭" aria-label="关闭" onClick={() => setNotice(null)}><X size={13} /></button></div>}
    {syncing && <div className="registry-progress smh-notice" role="status"><RefreshCw className={operation.data?.worker_available ? "spin" : ""} size={14} /><div className="smh-notice-copy"><span>{operation.data?.worker_available ? "正在同步 APIM 清单" : "同步已排队，后台处理服务当前不可用"}</span></div></div>}
    {(onboarding.error || retryOnboarding.error) && <InfrastructureErrorNotice>{errorMessage(onboarding.error ?? retryOnboarding.error)}</InfrastructureErrorNotice>}
    {(operation.error || operation.data?.status === "failed") && <InfrastructureErrorNotice>{operation.error ? errorMessage(operation.error) : operation.data?.error_message ?? "APIM 清单同步失败"}</InfrastructureErrorNotice>}
    {topology && !lifecycleAvailable && <div className="registry-progress smh-notice infrastructure-read-only-notice" role="status"><ApimLogo size={14} /><div className="smh-notice-copy"><span>当前独立 API 版本仅支持查看 APIM 与网关；部署匹配版本后才能添加、验证、重新关联或移除资源。</span></div></div>}
    {Boolean(error) && topology !== null && <InfrastructureErrorNotice>{`无法加载 APIM 拓扑：${errorMessage(error)}`}</InfrastructureErrorNotice>}
    {(addApim.error || addGateway.error || syncTarget.error || validateGateway.error || changeAssociation.error) && <InfrastructureErrorNotice>操作失败：{errorMessage(addApim.error ?? addGateway.error ?? syncTarget.error ?? validateGateway.error ?? changeAssociation.error)}</InfrastructureErrorNotice>}
    {loading && <InfrastructureTableLoading mode={mode} />}
    {topologyUnavailable && <ResourceTable kind={mode === "apim" ? "apim" : "gateway"} label={`${tabLabels[mode]}加载失败`} headers={resourceHeaders(mode)}><div className="smh-empty infrastructure-state-error" role="alert"><CircleAlert size={24} /><b>{`无法加载 APIM 拓扑：${errorMessage(error)}`}</b></div></ResourceTable>}
    {!loading && !topologyUnavailable && mode === "apim" && <ApimTable targets={targets} profiles={registry.gateways} onboardingItems={onboardingItems} onboardingAvailable={onboarding.data?.available === true} retryingId={retryOnboarding.isPending ? retryOnboarding.variables : null} onRetry={id => retryOnboarding.mutate(id)} filtered={Boolean(search.trim())} collapsedTargetIds={collapsedTargetIds} busy={busy} lifecycleAvailable={lifecycleAvailable} inventorySyncAvailable={topology?.inventory_sync_available === true} gatewayAttachmentAvailable={gatewayAttachmentAvailable} syncingId={syncTarget.isPending ? syncTarget.variables : syncing ? operation.data?.gateway_profile_id ?? null : null} validatingId={validateGateway.isPending ? validateGateway.variables : null} onToggleTarget={toggleTarget} onSync={id => syncTarget.mutate(id)} onEdit={target => { renameApim.reset(); setEditingApim(target) }} onEditGateway={editGateway} onAttach={openGateway} onBootstrap={gatewayCreationAvailable ? openStartup : undefined} onValidate={id => validateGateway.mutate(id)} onChange={setAssociationTarget} onRemoveGateway={entry => openRemoval({ kind: "gateway", id: entry.deployment.id, name: entry.deployment.display_name })} onRemove={target => openRemoval({ kind: "apim", id: target.target.gateway_profile_id, name: apimTargetDisplayName(target, registry.gateways) })} />}
    {!loading && !topologyUnavailable && mode === "gateways" && <GatewayTable entries={gateways} provisionItems={provisionItems} provisioningAvailable={gatewayCreationAvailable} provisionError={provisions.error} retryingId={retryProvision.isPending ? retryProvision.variables : null} retryError={retryProvision.error ? { id: retryProvision.variables, error: retryProvision.error } : null} onRetryProvision={id => retryProvision.mutate(id)} onProvisionBootstrap={id => setStartupGatewayId(id)} filtered={Boolean(search.trim())} busy={busy} lifecycleAvailable={lifecycleAvailable} validatingId={validateGateway.isPending ? validateGateway.variables : null} onEdit={editGateway} onBootstrap={gatewayCreationAvailable ? openStartup : undefined} onValidate={id => validateGateway.mutate(id)} onChange={setAssociationTarget} onRemove={entry => openRemoval({ kind: "gateway", id: entry.deployment.id, name: entry.deployment.display_name })} />}
    {!loading && !topologyUnavailable && <footer className="model-table-footer">{`共 ${mode === "apim" ? targets.length + onboardingItems.length : gateways.length + provisionItems.length} 个${tabLabels[mode]}`}</footer>}
    {apimOpen && <ApimOnboardingDialog available={topology?.onboarding_available === true && onboarding.data?.available !== false} onClose={() => setApimOpen(false)} onAdopt={() => { setApimOpen(false); setAdoptOpen(true) }} onAccepted={acceptedOnboarding} onSettled={refreshOnboarding} />}
    {adoptOpen && <ApimEditor busy={addApim.isPending} error={addApim.error} onClose={() => setAdoptOpen(false)} onSubmit={value => addApim.mutate(value)} />}
    {editingApim && <InfrastructureNameEditor key={editingApim.target.gateway_profile_id} kind="apim" identity={editingApim.target.service_name} name={apimTargetDisplayName(editingApim, registry.gateways)} supported={canManage && registry.gateway_name_edit_supported === true} busy={renameApim.isPending} error={renameApim.error} onClose={() => setEditingApim(null)} onSubmit={name => renameApim.mutate({ id: editingApim.target.gateway_profile_id, name })} />}
    {editingGateway && <InfrastructureNameEditor key={editingGateway.deployment.id} kind="gateway" identity={`${editingGateway.deployment.native_gateway_id} · ${editingGateway.target.target.service_name}`} name={editingGateway.deployment.display_name} available={gatewayEntries(topology).some(entry => entry.deployment.id === editingGateway.deployment.id)} supported={gatewayNameSupported} busy={renameDeployment.isPending} error={renameDeployment.error} onClose={() => setEditingGateway(null)} onSubmit={name => renameDeployment.mutate({ id: editingGateway.deployment.id, name })} />}
    {gatewayOpen && <GatewayEditor targets={(topology?.items ?? []).filter(item => !initializingIds.has(item.target.gateway_profile_id))} initialTargetId={gatewayTargetId} available={canManage && gatewayAttachmentAvailable} busy={addGateway.isPending} error={addGateway.error} onClose={() => setGatewayOpen(false)} onSubmit={(targetId, value) => addGateway.mutate({ targetId, value })} />}
    {createGatewayOpen && <SelfHostedGatewayCreateDialog targets={topology?.items ?? []} initialTargetId={gatewayTargetId} available={canManage && gatewayCreationAvailable} onClose={() => setCreateGatewayOpen(false)} onAdopt={targetId => { setCreateGatewayOpen(false); setGatewayTargetId(targetId); addGateway.reset(); setGatewayOpen(true) }} onAccepted={acceptedProvision} onSettled={refreshProvisions} />}
    {startupGatewayId && canManage && mode === "gateways" && <SelfHostedGatewayBootstrapDialog key={startupGatewayId} deploymentId={startupGatewayId} name={gatewayEntries(topology).find(entry => entry.deployment.id === startupGatewayId)?.deployment.display_name ?? startupGatewayId} onClose={() => setStartupGatewayId(null)} onValidate={() => { validateGateway.mutate(startupGatewayId); setStartupGatewayId(null) }} />}
    {associationTarget && <GatewayAssociationEditor entry={associationTarget} targets={topology?.items ?? []} canChangeAssociation={(topology?.items.length ?? 0) > 1} lifecycleAvailable={lifecycleAvailable} busy={changeAssociation.isPending} error={changeAssociation.error} onClose={() => setAssociationTarget(null)} onSubmit={value => changeAssociation.mutate({ deploymentId: associationTarget.deployment.id, value })} />}
    {removalTarget && <RemovalDialog target={removalTarget} preview={removal.data ?? null} loading={removal.isPending || removal.isFetching} busy={remove.isPending} error={removal.error ?? remove.error} onClose={() => { if (!remove.isPending) setRemovalTarget(null) }} onConfirm={() => { if (removal.data?.removal_allowed && !removal.isFetching && !removal.error) remove.mutate(removalTarget) }} />}
  </div>
}

type GatewayActions = { busy: boolean; lifecycleAvailable: boolean; onEdit: (entry: GatewayEntry) => void; onValidate: (id: string) => void; onChange: (entry: GatewayEntry) => void; onRemove: (entry: GatewayEntry) => void; onBootstrap?: (entry: GatewayEntry) => void }

function ApimTable({ targets, profiles, onboardingItems, onboardingAvailable, retryingId, onRetry, filtered, collapsedTargetIds, busy, lifecycleAvailable, inventorySyncAvailable, gatewayAttachmentAvailable, syncingId, validatingId, onToggleTarget, onSync, onEdit, onEditGateway, onAttach, onBootstrap, onValidate, onChange, onRemoveGateway, onRemove }: {
  targets: ApimTargetTopology[]; profiles: ModelRegistry["gateways"]; onboardingItems: GatewayReleaseOperation[]
  onboardingAvailable: boolean; retryingId: string | null; onRetry: (id: string) => void; filtered: boolean
  collapsedTargetIds: Set<string>; busy: boolean; lifecycleAvailable: boolean; inventorySyncAvailable: boolean
  gatewayAttachmentAvailable: boolean; syncingId: string | null; validatingId: string | null
  onToggleTarget: (id: string) => void; onSync: (id: string) => void; onEdit: (target: ApimTargetTopology) => void
  onEditGateway: (entry: GatewayEntry) => void; onAttach: (id: string) => void; onValidate: (id: string) => void
  onBootstrap?: (entry: GatewayEntry) => void
  onChange: (entry: GatewayEntry) => void; onRemoveGateway: (entry: GatewayEntry) => void; onRemove: (target: ApimTargetTopology) => void
}) {
  return <ResourceTable kind="apim" label="APIM 实例列表" headers={resourceHeaders("apim")}>
    {targets.map(item => {
      const targetId = item.target.gateway_profile_id
      const displayName = apimTargetDisplayName(item, profiles)
      const resourceLabel = displayName === item.target.service_name ? item.target.resource_group : `${item.target.service_name} · ${item.target.resource_group}`
      const entries = apimGatewayEntries(item)
      const expanded = entries.length > 0 && !collapsedTargetIds.has(targetId)
      const childrenId = `apim-gateways-${targetId}`
      return <section className="connection-provider-group apim-target-group" role="rowgroup" aria-label={displayName} key={targetId}>
        <article className="connection-provider-heading apim-target-heading" role="row">
          <div className="connection-provider-identity" role="cell">{entries.length ? <button type="button" className="model-list-icon apim-hierarchy-toggle" aria-label={expanded ? "折叠网关" : "展开网关"} aria-expanded={expanded} aria-controls={childrenId} onClick={() => onToggleTarget(targetId)}><span className="apim-hierarchy-logo"><ApimLogo size={16} /></span><ChevronRight className="apim-hierarchy-chevron" size={16} /></button> : <span className="model-list-icon" data-model-family="apim"><ApimLogo size={16} /></span>}<span><b title={displayName} data-no-localize>{displayName}</b><small className="apim-target-resource" title={resourceLabel} data-no-localize>{resourceLabel}</small></span></div>
          <span className="connection-provider-summary" role="cell"><b title={item.gateway_endpoint ?? undefined} data-no-localize={Boolean(item.gateway_endpoint) || undefined}>{item.gateway_endpoint ?? "未发现网关地址"}</b><small><span>管理地址</span> · <span title={item.management_endpoint} data-no-localize>{item.management_endpoint}</span> · <span>配置地址</span> · <span title={item.configuration_endpoint} data-no-localize>{item.configuration_endpoint}</span></small></span>
          <span className="connection-provider-summary" role="cell"><b>{`${entries.length} 个网关`}</b><small>{item.published_models_available === false ? "模型关系需升级 API" : publishedModelSummary(item.published_model_count, item.target.api_id)}</small></span>
          <div className="model-status-cell" role="cell"><ResourceReadiness state={item.connection_state === "connected" ? "ready" : item.connection_state === "unavailable" ? "unavailable" : "warning"} label={item.connection_state === "connected" ? "已同步" : item.connection_state === "unavailable" ? "不可用" : "待验证"} detail={item.connection_state === "connected" ? "APIM 清单已验证；并非实时数据面健康状态" : item.connection_state === "unavailable" ? "APIM 清单与配置不一致" : "尚未同步"} timestamp={item.target.last_inventory_sync_at} /></div>
          <InfrastructureActions name={displayName} checkLabel="验证并同步 APIM" removeLabel="从 Turnstile 移除 APIM" busy={busy} checkDisabled={!inventorySyncAvailable} removeDisabled={!lifecycleAvailable} checking={syncingId === targetId} onCheck={() => onSync(targetId)} onEdit={profiles.some(profile => profile.id === targetId) ? () => onEdit(item) : undefined} onAttach={() => onAttach(targetId)} attachDisabled={!gatewayAttachmentAvailable || item.target.capabilities.self_hosted_gateway === "unsupported"} onRemove={() => onRemove(item)} disclosure={entries.length ? { expanded, controls: childrenId, onToggle: () => onToggleTarget(targetId) } : undefined} />
        </article>
        {entries.length > 0 && <div id={childrenId} className="apim-gateway-children" hidden={!expanded}>{entries.map(entry => <GatewayChildRow key={entry.deployment.id} entry={entry} busy={busy} lifecycleAvailable={lifecycleAvailable} validating={validatingId === entry.deployment.id} onEdit={() => onEditGateway(entry)} onValidate={() => onValidate(entry.deployment.id)} onChange={() => onChange(entry)} onBootstrap={onBootstrap ? () => onBootstrap(entry) : undefined} onRemove={() => onRemoveGateway(entry)} />)}</div>}
      </section>
    })}
    {onboardingItems.map(operation => <ApimOnboardingRow key={operation.gateway_profile_id} operation={operation} available={onboardingAvailable && !busy} retryingId={retryingId} onRetry={onRetry} />)}
    {!targets.length && !onboardingItems.length && <InfrastructureEmpty icon={filtered ? <Search size={27} /> : <ApimLogo size={27} />} label={filtered ? "没有匹配的 APIM 实例" : "尚未添加 APIM 实例"} />}
  </ResourceTable>
}

function GatewayChildRow({ entry, busy, lifecycleAvailable, validating, onEdit, onValidate, onChange, onRemove, onBootstrap }: {
  entry: GatewayEntry; busy: boolean; lifecycleAvailable: boolean; validating: boolean
  onEdit: () => void; onValidate: () => void; onChange: () => void; onRemove: () => void
  onBootstrap?: () => void
}) {
  const managed = entry.deployment.kind === "managed"
  const connected = gatewayEvidence(entry).state === "ready"
  const models = gatewayPublishedModels(entry)
  const endpointMetadata = managed ? entry.deployment.location ?? entry.target.target.service_location : entry.configuration_endpoint
  return <article className="model-list-row connection-list-row infrastructure-resource-row gateway apim-gateway-child-row" role="row" data-gateway-kind={entry.deployment.kind} aria-label={`${entry.deployment.display_name} · ${entry.target.target.service_name}`}>
    <div className="model-primary-cell connection-primary-cell" role="cell"><span className="connection-branch" aria-hidden="true" /><span className="model-list-icon gateway-child-icon" data-model-family="apim">{managed ? <ManagedGatewayLogo size={16} /> : connected ? <Wifi size={15} /> : <WifiOff size={15} />}</span><div><b title={entry.deployment.display_name} data-no-localize>{entry.deployment.display_name}</b><span className="gateway-deployment-metadata">{managed ? <span>托管网关</span> : <><code title={entry.deployment.native_gateway_id} data-no-localize>{entry.deployment.native_gateway_id}</code> · <span>{gatewayHostingLabels[entry.deployment.hosting_mode]}</span></>}</span></div></div>
    <div className="model-runtime-cell" role="cell"><b title={entry.deployment.endpoint_url ?? undefined} data-no-localize={Boolean(entry.deployment.endpoint_url) || undefined}>{entry.deployment.endpoint_url ?? "端点未配置"}</b><span>{!managed && <><span>配置地址</span> · </>}<span title={endpointMetadata} data-no-localize>{endpointMetadata}</span></span></div>
    <div className="model-pricing-cell" role="cell"><b>{publishedModelSummary(models.length, entry.target.target.api_id)}</b><span>{models.length ? models.map(model => model.display_name).join("、") : "尚未发布模型"}</span></div>
    <div className="model-status-cell" role="cell"><GatewayReadiness entry={entry} /></div>
    {managed ? <div className="model-row-actions infrastructure-row-actions" role="cell" /> : <InfrastructureActions name={entry.deployment.display_name} checkLabel="验证连接" editLabel="编辑网关名称" removeLabel="从 Turnstile 移除网关" busy={busy} checkDisabled={!lifecycleAvailable || !entry.deployment.endpoint_url} removeDisabled={!lifecycleAvailable} checking={validating} onCheck={onValidate} onEdit={onEdit} onBinding={onChange} onBootstrap={entry.deployment.provisioning_operation_id ? onBootstrap : undefined} onRemove={onRemove} />}
  </article>
}

function GatewayTable({ entries, provisionItems = [], provisioningAvailable = false, provisionError, retryingId, retryError, onRetryProvision, onProvisionBootstrap, filtered, busy, lifecycleAvailable, validatingId, onEdit, onValidate, onChange, onRemove, onBootstrap }: GatewayActions & {
  entries: GatewayEntry[]; provisionItems?: GatewayProvisionItem[]; provisioningAvailable?: boolean
  provisionError?: unknown; retryingId?: string | null; retryError?: { id: string | undefined; error: unknown } | null
  onRetryProvision?: (id: string) => void; onProvisionBootstrap?: (id: string) => void
  filtered: boolean; validatingId: string | null
}) {
  return <ResourceTable kind="gateway" label="自托管网关列表" headers={resourceHeaders("gateways")}>
    {Boolean(provisionError) && <div role="row"><div role="cell" aria-colspan={5}><InfrastructureErrorNotice>{errorMessage(provisionError)}</InfrastructureErrorNotice></div></div>}
    {provisionItems.map(({ operation: current, target }) => <SelfHostedGatewayProvisionProgress key={current.id} operation={current} target={target} available={provisioningAvailable} retrying={retryingId === current.id} error={retryError?.id === current.id ? retryError.error : null} onRetry={id => onRetryProvision?.(id)} onBootstrap={onProvisionBootstrap} />)}
    {entries.map(entry => {
      const connected = gatewayEvidence(entry).state === "ready"
      const models = gatewayPublishedModels(entry)
      return <article className="model-list-row infrastructure-resource-row gateway" role="row" key={entry.deployment.id}>
        <div className="model-primary-cell" role="cell"><span className="model-list-icon gateway-child-icon" data-model-family="apim">{connected ? <Wifi size={15} /> : <WifiOff size={15} />}</span><div><b title={entry.deployment.display_name} data-no-localize>{entry.deployment.display_name}</b><span className="gateway-deployment-metadata"><code title={entry.deployment.native_gateway_id} data-no-localize>{entry.deployment.native_gateway_id}</code> · <span>{gatewayHostingLabels[entry.deployment.hosting_mode]}</span></span></div></div>
        <div className="model-runtime-cell" role="cell"><b title={`${entry.target.target.service_name} · ${entry.target.target.resource_group}`}>{entry.target.target.service_name}</b><span title={models.map(model => model.display_name).join("、")}>{entry.published_models_available === false ? "模型关系需升级 API" : publishedModelSummary(models.length, entry.target.target.api_id)}</span></div>
        <div className="model-pricing-cell" role="cell"><b title={entry.deployment.endpoint_url ?? undefined} data-no-localize={Boolean(entry.deployment.endpoint_url) || undefined}>{entry.deployment.endpoint_url ?? "端点未配置"}</b><span title={entry.configuration_endpoint}><span>配置地址</span> · <span data-no-localize>{entry.configuration_endpoint}</span></span></div>
        <div className="model-status-cell" role="cell"><GatewayReadiness entry={entry} /></div>
        <InfrastructureActions name={entry.deployment.display_name} checkLabel="验证连接" editLabel="编辑网关名称" removeLabel="从 Turnstile 移除网关" busy={busy} checkDisabled={!lifecycleAvailable || !entry.deployment.endpoint_url} removeDisabled={!lifecycleAvailable} checking={validatingId === entry.deployment.id} onCheck={() => onValidate(entry.deployment.id)} onEdit={() => onEdit(entry)} onBinding={() => onChange(entry)} onBootstrap={entry.deployment.provisioning_operation_id && onBootstrap ? () => onBootstrap(entry) : undefined} onRemove={() => onRemove(entry)} />
      </article>
    })}
    {!entries.length && !provisionItems.length && !provisionError && <InfrastructureEmpty icon={filtered ? <Search size={27} /> : <Server size={27} />} label={filtered ? "没有匹配的自托管网关" : "尚未添加自托管网关"} />}
  </ResourceTable>
}

function InfrastructureActions({ name, checkLabel, editLabel = "编辑 APIM 名称", removeLabel, busy, checkDisabled, removeDisabled, checking, onCheck, onEdit, onAttach, attachDisabled = false, onBinding, onRemove, onBootstrap, disclosure }: {
  name: string; checkLabel: string; editLabel?: string; removeLabel: string; busy: boolean; checkDisabled: boolean; removeDisabled: boolean; checking: boolean
  onCheck: () => void; onEdit?: () => void; onAttach?: () => void; attachDisabled?: boolean; onBinding?: () => void; onRemove: () => void
  onBootstrap?: () => void
  disclosure?: { expanded: boolean; controls: string; onToggle: () => void }
}) {
  const { user } = useAuth()
  const gatewayNameActions = Boolean(onEdit && onBinding)
  const menuActions = Boolean(onAttach || gatewayNameActions)
  return <div className={`model-row-actions infrastructure-row-actions${gatewayNameActions ? " gateway-name-actions" : ""}`} role="cell">
    {user?.role === "owner" && <>
      <button type="button" className="connection-health-action" aria-label={checkLabel} title={checkLabel} disabled={busy || checkDisabled} onClick={onCheck}><RefreshCw className={checking ? "spin" : ""} size={14} /></button>
      {onEdit && <button type="button" className="model-edit-action" aria-label={editLabel} title={editLabel} disabled={busy} onClick={onEdit}><Pencil size={14} /></button>}
      {onBinding && <button type="button" className="model-edit-action" aria-label="APIM 绑定" title="APIM 绑定" disabled={busy} onClick={onBinding}><Link2 size={14} /></button>}
      {!menuActions && <button type="button" className="model-delete-action" aria-label={removeLabel} title={removeLabel} disabled={busy || removeDisabled} onClick={onRemove}><Trash2 size={14} /></button>}
      <DropdownMenu><DropdownMenuTrigger render={<button type="button" className={menuActions ? "infrastructure-menu-trigger model-edit-action" : "infrastructure-menu-trigger"} aria-label="更多操作" title={name} disabled={busy} />}><MoreHorizontal size={15} /></DropdownMenuTrigger><DropdownMenuContent align="end" className="infrastructure-row-menu">
        {onAttach && <DropdownMenuItem disabled={busy || attachDisabled} title={attachDisabled ? "此 APIM 当前不支持挂载自托管网关" : undefined} onClick={onAttach}><Plus size={14} />新建自托管网关</DropdownMenuItem>}
        {onBootstrap && <DropdownMenuItem disabled={busy} onClick={onBootstrap}><Download size={14} />启动配置</DropdownMenuItem>}
        <DropdownMenuItem disabled={busy || checkDisabled} onClick={onCheck}><RefreshCw size={14} />{checkLabel}</DropdownMenuItem>
        {onEdit && <DropdownMenuItem disabled={busy} onClick={onEdit}><Pencil size={14} />{editLabel}</DropdownMenuItem>}
        {onBinding && <DropdownMenuItem disabled={busy} onClick={onBinding}><Link2 size={14} />APIM 绑定</DropdownMenuItem>}
        <DropdownMenuItem variant="destructive" disabled={busy || removeDisabled} onClick={onRemove}><Trash2 size={14} />{removeLabel}</DropdownMenuItem>
      </DropdownMenuContent></DropdownMenu>
    </>}
    {disclosure ? <button type="button" className="connection-provider-chevron apim-row-chevron" aria-label={disclosure.expanded ? "折叠网关" : "展开网关"} aria-expanded={disclosure.expanded} aria-controls={disclosure.controls} onClick={disclosure.onToggle}><ChevronRight size={15} /></button> : !gatewayNameActions && <span className="apim-row-chevron-placeholder" aria-hidden="true" />}
  </div>
}

function resourceHeaders(mode: InfrastructureTab) {
  return mode === "apim" ? ["APIM 实例", "端点", "关联资源", "状态", ""] : ["网关", "所属 APIM / 模型", "端点", "状态", ""]
}

function ResourceTable({ kind, label, headers, children }: { kind: "apim" | "gateway"; label: string; headers: string[]; children: React.ReactNode }) {
  return <ResizableGridTable className={`model-table infrastructure-resource-table ${kind}`} role="table" aria-label={label} headerSelector=".model-table-head" minWidths={resourceTableColumnMinWidths} columnGap={12} horizontalPadding={32} resizeAtAllWidths>
    <div className="model-table-head" role="row">{headers.map((header, index) => <span className="model-table-heading" role="columnheader" aria-label={header || "操作"} key={`${index}-${header}`}>{index < headers.length - 1 && <span>{header}</span>}</span>)}</div><div className="model-table-body">{children}</div>
  </ResizableGridTable>
}

function InfrastructureTableLoading({ mode }: { mode: InfrastructureTab }) {
  if (mode === "apim") return <ResourceTable kind="apim" label={`${tabLabels[mode]}加载中`} headers={resourceHeaders(mode)}>
    <section className="connection-provider-group apim-target-group infrastructure-loading-group" role="rowgroup" aria-busy="true">
      <span className="sr-only">加载资源拓扑</span>
      <div className="connection-provider-heading apim-target-heading infrastructure-loading-row" role="row">
        <div className="connection-provider-identity" role="cell"><span className="model-list-icon infrastructure-skeleton-icon" aria-hidden="true" /><span className="infrastructure-skeleton-copy" aria-hidden="true"><i className="infrastructure-skeleton-line medium" /><i className="infrastructure-skeleton-line short" /></span></div>
        <span className="connection-provider-summary infrastructure-skeleton-cell" role="cell" aria-hidden="true"><i /><i /></span><span className="connection-provider-summary infrastructure-skeleton-cell compact" role="cell" aria-hidden="true"><i /><i /></span><span className="model-status-cell infrastructure-skeleton-cell status" role="cell" aria-hidden="true"><i /><i /></span><span className="model-row-actions" role="cell" />
      </div>
      <div className="apim-gateway-children" aria-hidden="true"><div className="model-list-row connection-list-row infrastructure-resource-row gateway apim-gateway-child-row infrastructure-loading-row" role="row">
        <div className="model-primary-cell connection-primary-cell" role="cell"><span className="connection-branch" /><span className="model-list-icon gateway-child-icon infrastructure-skeleton-icon" /><div className="infrastructure-skeleton-copy"><i className="infrastructure-skeleton-line medium" /><i className="infrastructure-skeleton-line short" /></div></div>
        <div className="model-runtime-cell infrastructure-skeleton-cell" role="cell"><i /><i /></div><div className="model-pricing-cell infrastructure-skeleton-cell compact" role="cell"><i /><i /></div><div className="model-status-cell infrastructure-skeleton-cell status" role="cell"><i /><i /></div><div className="model-row-actions" role="cell" />
      </div></div>
    </section>
  </ResourceTable>
  return <ResourceTable kind="gateway" label={`${tabLabels[mode]}加载中`} headers={resourceHeaders(mode)}>
    <div className="model-list-row infrastructure-resource-row gateway infrastructure-loading-row" role="row" aria-busy="true" aria-label="加载资源拓扑">
      <div className="model-primary-cell" role="cell"><span className="model-list-icon infrastructure-skeleton-icon" aria-hidden="true" /><div className="infrastructure-skeleton-copy" aria-hidden="true"><i className="infrastructure-skeleton-line medium" /><i className="infrastructure-skeleton-line short" /></div></div>
      <div className="model-runtime-cell infrastructure-skeleton-cell" role="cell" aria-hidden="true"><i /><i /></div><div className="model-pricing-cell infrastructure-skeleton-cell compact" role="cell" aria-hidden="true"><i /><i /></div><div className="model-status-cell infrastructure-skeleton-cell status" role="cell" aria-hidden="true"><i /><i /></div><div className="model-row-actions" role="cell" />
    </div>
  </ResourceTable>
}

function ResourceReadiness({ state, label, detail, timestamp }: { state: "ready" | "warning" | "unavailable"; label: string; detail: string; timestamp?: string | null }) {
  const { locale } = useLocale()
  const { timezone } = useTimezone()
  const date = timestamp ? new Date(timestamp) : null
  const validDate = date && Number.isFinite(date.getTime()) ? date : null
  const longDate = validDate?.toLocaleString(locale, { timeZone: timezone, timeZoneName: "short" })
  return <span className={`gateway-readiness ${state}`} title={`${detail}${longDate ? ` · ${longDate}` : ""}`}><span><i />{label}</span><small>{validDate ? <time dateTime={timestamp!} data-no-localize>{validDate.toLocaleString(locale, { timeZone: timezone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })}</time> : detail}</small></span>
}
function GatewayReadiness({ entry }: { entry: GatewayEntry }) {
  const evidence = gatewayEvidence(entry)
  return <ResourceReadiness state={evidence.state} label={evidence.label} detail={evidence.detail} timestamp={evidence.probedAt} />
}
function InfrastructureEmpty({ icon, label }: { icon: React.ReactNode; label: string }) {
  return <div className="smh-empty">{icon}<b>{label}</b></div>
}

function InfrastructureNameEditor({ kind, identity, name, supported, available = true, busy, error, onClose, onSubmit }: {
  kind: "apim" | "gateway"; identity: string; name: string; supported: boolean; available?: boolean; busy: boolean; error: unknown; onClose: () => void; onSubmit: (name: string) => void
}) {
  const [value, setValue] = useState(name)
  const valid = available && gatewayNameFormValid(value, name)
  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}><DialogContent className={`registry-editor-dialog infrastructure-editor-dialog apim-name-editor-dialog${kind === "gateway" ? " gateway-name-editor-dialog" : ""}`} finalFocus={false}><form className="registry-editor" onSubmit={event => { event.preventDefault(); if (supported && !busy && valid) onSubmit(value.trim()) }}>
    <DialogHeader className="registry-editor-header"><DialogTitle>{kind === "apim" ? "编辑 APIM 名称" : "编辑网关名称"}</DialogTitle><DialogDescription data-no-localize>{identity}</DialogDescription></DialogHeader><EditorClose busy={busy} />
    <div className="registry-editor-body infrastructure-editor-body"><Field label="Turnstile 显示名称"><Input name="name" value={value} maxLength={120} disabled={busy} onChange={event => setValue(event.target.value)} autoFocus /></Field>
      {!supported && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>{kind === "apim" ? "当前后端版本不支持修改 APIM 显示名称" : "当前后端版本不支持修改网关显示名称"}</span></div>}
      {!available && <InfrastructureErrorNotice>所选网关已不可用</InfrastructureErrorNotice>}{Boolean(error) && <div className="registry-error" role="alert">{errorMessage(error)}</div>}
    </div><DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={!supported || busy || !valid}>{busy ? <RefreshCw size={14} className="spin" /> : <Save size={14} />}{busy ? "正在保存" : "保存"}</Button></DialogFooter>
  </form></DialogContent></Dialog>
}

function ApimEditor({ busy, error, onClose, onSubmit }: { busy: boolean; error: unknown; onClose: () => void; onSubmit: (value: ApimTargetWrite) => void }) {
  const [value, setValue] = useState(emptyApimForm)
  const update = (field: keyof ApimTargetWrite, next: string) => setValue(current => ({ ...current, [field]: next }))
  const valid = apimFormValid(value)
  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}><DialogContent className="registry-editor-dialog infrastructure-editor-dialog" finalFocus={false}><form className="registry-editor" onSubmit={event => { event.preventDefault(); if (valid && !busy) onSubmit(apimFormPayload(value)) }}>
    <DialogHeader className="registry-editor-header"><DialogTitle>关联已有 APIM 配置</DialogTitle><DialogDescription>验证现有 Azure API Management 实例后保存到 Turnstile。</DialogDescription></DialogHeader><EditorClose busy={busy} />
    <div className="registry-editor-body infrastructure-editor-body">
      <Field label="实例名称"><Input name="name" value={value.name} disabled={busy} onChange={event => update("name", event.target.value)} autoFocus /></Field>
      <Field label="管理地址"><Input name="management_endpoint" value={value.management_endpoint} disabled={busy} onChange={event => update("management_endpoint", event.target.value)} placeholder="https://management.azure.com/subscriptions/.../service/..." /></Field>
      <div className="infrastructure-field-grid"><Field label="租户 ID"><Input name="tenant_id" value={value.tenant_id} disabled={busy} onChange={event => update("tenant_id", event.target.value)} /></Field><Field label="订阅 ID"><Input name="subscription_id" value={value.subscription_id} disabled={busy} onChange={event => update("subscription_id", event.target.value)} /></Field></div>
      <div className="infrastructure-field-grid"><Field label="资源组"><Input name="resource_group" value={value.resource_group} disabled={busy} onChange={event => update("resource_group", event.target.value)} /></Field><Field label="APIM 服务名称"><Input name="service_name" value={value.service_name} disabled={busy} onChange={event => update("service_name", event.target.value)} /></Field></div>
      <Field label="网关地址"><Input name="gateway_endpoint" value={value.gateway_endpoint} disabled={busy} onChange={event => update("gateway_endpoint", event.target.value)} placeholder="https://example.azure-api.net/api-path" /></Field>
      <div className="infrastructure-field-grid"><Field label="API ID"><Input name="api_id" value={value.api_id} disabled={busy} onChange={event => update("api_id", event.target.value)} /></Field><Field label="管理认证方式"><Input value="系统分配的托管身份" disabled /></Field></div>
      <div className="infrastructure-field-grid"><Field label="控制台订阅"><Input name="dashboard_subscription_id" value={value.dashboard_subscription_id} disabled={busy} onChange={event => update("dashboard_subscription_id", event.target.value)} /></Field><Field label="探测订阅"><Input name="probe_subscription_id" value={value.probe_subscription_id} disabled={busy} onChange={event => update("probe_subscription_id", event.target.value)} /></Field></div>
      {Boolean(error) && <div className="registry-error">{errorMessage(error)}</div>}
    </div><DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !valid}>{busy ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}{busy ? "正在验证" : "验证并添加"}</Button></DialogFooter>
  </form></DialogContent></Dialog>
}

function GatewayEditor({ targets, initialTargetId, available, busy, error, onClose, onSubmit }: {
  targets: ApimTargetTopology[]; initialTargetId: string | null; available: boolean; busy: boolean; error: unknown; onClose: () => void; onSubmit: (targetId: string, value: SelfHostedGatewayWrite) => void
}) {
  const endpointErrorId = useId()
  const choices = targets.filter(target => target.target.capabilities.self_hosted_gateway !== "unsupported")
  const [targetId, setTargetId] = useState(initialTargetId ?? choices[0]?.target.gateway_profile_id ?? "")
  const selected = choices.find(target => target.target.gateway_profile_id === targetId)
  const [value, setValue] = useState(emptyGatewayForm)
  const update = (field: keyof SelfHostedGatewayWrite, next: string) => setValue(current => ({ ...current, [field]: next }))
  const valid = available && Boolean(selected) && gatewayFormValid(value)
  const invalidEndpoint = Boolean(value.endpoint_url.trim()) && !isGatewayEndpoint(value.endpoint_url)
  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}><DialogContent className="registry-editor-dialog infrastructure-editor-dialog gateway-attachment-dialog" finalFocus={false}><form className="registry-editor" onSubmit={event => { event.preventDefault(); if (valid && !busy) onSubmit(targetId, gatewayFormPayload(value)) }}>
    <DialogHeader className="registry-editor-header"><DialogTitle>关联已有网关</DialogTitle><DialogDescription>已有 APIM 网关配置</DialogDescription></DialogHeader><EditorClose busy={busy} />
    <div className="registry-editor-body infrastructure-editor-body"><TargetSelect targets={choices} value={targetId} onChange={setTargetId} disabled={busy || initialTargetId !== null} /><Field label="配置地址"><Input value={selected?.configuration_endpoint ?? ""} readOnly /></Field>
      <div className="infrastructure-field-grid"><Field label="网关名称"><Input name="display_name" value={value.display_name} disabled={busy} maxLength={120} onChange={event => update("display_name", event.target.value)} autoFocus /></Field><Field label="APIM 网关 ID"><Input name="native_gateway_id" value={value.native_gateway_id} disabled={busy} maxLength={80} onChange={event => update("native_gateway_id", event.target.value)} /></Field></div>
      <div><Field label="网关地址"><Input name="endpoint_url" type="url" value={value.endpoint_url} disabled={busy} aria-invalid={invalidEndpoint} aria-describedby={invalidEndpoint ? endpointErrorId : undefined} onChange={event => update("endpoint_url", event.target.value)} placeholder="https://gateway.example.com/api-path" /></Field>{invalidEndpoint && <span className="infrastructure-field-error" id={endpointErrorId}>使用 HTTPS URL，不得包含凭据、查询参数或片段。</span>}</div>
      <div className="infrastructure-field-grid"><Field label="位置（可选）"><Input name="location" value={value.location ?? ""} disabled={busy} maxLength={128} onChange={event => update("location", event.target.value)} /></Field><Field label="运行环境"><Select value={value.hosting_mode ?? "kubernetes"} disabled={busy} onValueChange={next => update("hosting_mode", next ?? "other")}><SelectTrigger className="registry-select-trigger" aria-label="运行环境"><SelectValue>{gatewayHostingLabels[value.hosting_mode ?? "kubernetes"]}</SelectValue></SelectTrigger><SelectContent>{["aks", "kubernetes", "arc", "container_apps", "docker", "other"].map(item => <SelectItem key={item} value={item}>{gatewayHostingLabels[item]}</SelectItem>)}</SelectContent></Select></Field></div>
      {!available && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>当前 API 不支持挂载自托管网关</span></div>}{available && !selected && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>所选 APIM 当前不可挂载自托管网关</span></div>}{Boolean(error) && <div className="registry-error" role="alert">{errorMessage(error)}</div>}
    </div><DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !valid}>{busy ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}{busy ? "正在保存" : "保存关联"}</Button></DialogFooter>
  </form></DialogContent></Dialog>
}

function GatewayAssociationEditor({ entry, targets, canChangeAssociation, lifecycleAvailable, busy, error, onClose, onSubmit }: {
  entry: GatewayEntry; targets: ApimTargetTopology[]; canChangeAssociation: boolean; lifecycleAvailable: boolean; busy: boolean; error: unknown; onClose: () => void; onSubmit: (value: SelfHostedGatewayAssociationChange) => void
}) {
  const currentTargetId = entry.target.target.gateway_profile_id
  const choices = targets.filter(item => item.target.gateway_profile_id === currentTargetId || item.target.capabilities.self_hosted_gateway !== "unsupported")
  const [targetId, setTargetId] = useState(currentTargetId)
  const [endpoint, setEndpoint] = useState(entry.deployment.endpoint_url ?? "")
  const selected = choices.find(target => target.target.gateway_profile_id === targetId)
  const models = gatewayPublishedModels(entry)
  const blocked = !lifecycleAvailable ? "当前 API 版本不支持资源生命周期操作"
    : entry.published_models_available === false ? "模型关系需升级 API"
    : models.length > 0 ? "请先移除或迁移已发布模型"
    : !canChangeAssociation || choices.length < 2 ? "没有其他可绑定的 APIM 实例"
    : null
  const valid = !blocked && targetId !== currentTargetId && Boolean(selected) && isGatewayEndpoint(endpoint)
  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}><DialogContent className="registry-editor-dialog infrastructure-editor-dialog" finalFocus={false}><form className="registry-editor" onSubmit={event => {
    event.preventDefault()
    if (valid && !busy) onSubmit({ apim_target_id: targetId, native_gateway_id: entry.deployment.native_gateway_id, display_name: entry.deployment.display_name, endpoint_url: endpoint.trim(), location: entry.deployment.location, hosting_mode: entry.deployment.hosting_mode === "azure_managed" ? "other" : entry.deployment.hosting_mode, release_gate: entry.deployment.release_gate })
  }}>
    <DialogHeader className="registry-editor-header"><DialogTitle>APIM 绑定</DialogTitle><DialogDescription data-no-localize>{entry.deployment.display_name}</DialogDescription></DialogHeader><EditorClose busy={busy} />
    <div className="registry-editor-body infrastructure-editor-body"><TargetSelect targets={choices} value={targetId} onChange={setTargetId} disabled={busy || Boolean(blocked)} /><Field label="APIM 网关 ID"><Input value={entry.deployment.native_gateway_id} readOnly /></Field><Field label="配置地址"><Input value={selected?.configuration_endpoint ?? entry.configuration_endpoint} readOnly /></Field><Field label="网关地址"><Input type="url" value={endpoint} readOnly={Boolean(blocked) || targetId === currentTargetId} disabled={busy} onChange={event => setEndpoint(event.target.value)} /></Field>
      {models.length > 0 && <div className="infrastructure-dependencies"><b>已发布模型</b>{models.map(model => <span key={model.id}><span title={model.display_name}>{model.display_name}</span><code title={model.model_key}>{model.model_key}</code></span>)}</div>}
      {blocked && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>{blocked}</span></div>}
      {!blocked && targetId !== currentTargetId && <div className="infrastructure-warning"><ArrowRightLeft size={15} /><span>保存后请自行更新网关运行环境中的配置地址和认证配置；Turnstile 不会远程修改或重启网关。</span></div>}
      {Boolean(error) && <div className="registry-error">{errorMessage(error)}</div>}
    </div><DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>{blocked ? "关闭" : "取消"}</DialogClose>{!blocked && <Button type="submit" disabled={busy || !valid}>{busy ? <RefreshCw className="spin" size={14} /> : <Link2 size={14} />}<span>保存新关联</span></Button>}</DialogFooter>
  </form></DialogContent></Dialog>
}

function EditorClose({ busy }: { busy: boolean }) {
  return <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
}
function TargetSelect({ targets, value, onChange, disabled = false }: { targets: ApimTargetTopology[]; value: string; onChange: (value: string) => void; disabled?: boolean }) {
  const selected = targets.find(item => item.target.gateway_profile_id === value)
  return <div className="registry-field"><span className="registry-field-label">APIM 实例</span><Select value={value} disabled={disabled} onValueChange={next => onChange(next ?? "")}><SelectTrigger className="registry-select-trigger" aria-label="APIM 实例" title={selected?.target.service_name}><SelectValue>{selected?.target.service_name ?? "选择 APIM 实例"}</SelectValue></SelectTrigger><SelectContent>{targets.map(item => <SelectItem key={item.target.gateway_profile_id} value={item.target.gateway_profile_id}><span className="registry-option"><ApimLogo size={15} /><span>{item.target.service_name}</span></span></SelectItem>)}</SelectContent></Select></div>
}
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="registry-field"><span className="registry-field-label">{label}</span>{children}</label>
}
function RemovalDialog({ target, preview, loading, busy, error, onClose, onConfirm }: { target: RemovalTarget; preview: RemovalPreview | null; loading: boolean; busy: boolean; error: unknown; onClose: () => void; onConfirm: () => void }) {
  const models = preview?.models ?? []
  const gateways = preview && "self_hosted_gateways" in preview ? preview.self_hosted_gateways : []
  return <AlertDialog open onOpenChange={open => { if (!open && !busy) onClose() }}><AlertDialogContent className="model-delete-dialog infrastructure-remove-dialog" onClick={event => event.stopPropagation()}><div className="model-delete-body">
    <AlertDialogTitle>{target.kind === "apim" ? "移除 APIM 实例" : "移除自托管网关"}</AlertDialogTitle><AlertDialogDescription>只会移除 Turnstile 中的配置和关联，不会删除或修改用户的 Azure 资源、网关或运行环境。</AlertDialogDescription>
    {loading && <div className="infrastructure-preview-loading"><RefreshCw className="spin" size={14} />检查关联资源</div>}
    {gateways.length > 0 && <div className="infrastructure-dependencies"><b>关联网关</b>{gateways.map(gateway => <span key={gateway.id}><span title={gateway.display_name} data-no-localize>{gateway.display_name}</span><code title={gateway.native_gateway_id}>{gateway.native_gateway_id}</code></span>)}</div>}
    {models.length > 0 && <div className="infrastructure-dependencies"><b>已发布模型</b>{models.map(model => <span key={model.id}><span title={model.display_name}>{model.display_name}</span><code title={model.model_key}>{model.model_key}</code></span>)}</div>}
    {preview && !preview.removal_allowed && <div className="infrastructure-warning"><CircleAlert size={15} /><span>{preview.blockers.join("；")}</span></div>}{Boolean(error) && <div className="registry-error">{errorMessage(error)}</div>}
  </div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel disabled={busy}>取消</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={busy || loading || !preview?.removal_allowed} onClick={onConfirm}>{busy ? "正在移除" : "从 Turnstile 移除"}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
}