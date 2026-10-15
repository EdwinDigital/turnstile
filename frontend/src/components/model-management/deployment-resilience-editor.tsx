import {
  CheckCircle2,
  CircleAlert,
  LoaderCircle,
  PowerOff,
  Send,
  ShieldCheck,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { dataSource } from "../../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../../data-sources/apim/queries"
import type {
  GatewayBackendPoolConfig,
  GatewayBackendPoolWrite,
  GatewayPublicationStatus,
  ManagedModel,
  ModelRuntime,
} from "../../data-sources/apim/types"
import { useAuth } from "../../providers/auth-provider"
import { equivalentNativeRouteRuntimes } from "../model-management/apim-native-route-eligibility"
import { FieldHelp } from "../model-management/field-help"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from "../ui/alert-dialog"
import { Checkbox } from "../ui/checkbox"
import { Button } from "../ui/button"
import { ButtonGroup } from "../ui/button-group"
import { Input } from "../ui/input"
import { poolPublicationPayload } from "./pool-session-affinity"

const DEFAULT_RATE_LIMIT: GatewayBackendPoolWrite["rate_limit"] = {
  max_attempts_per_request: 2,
  retry_interval_seconds: 1,
  first_fast_retry: true,
  backend_timeout_seconds: 120,
  circuit_breaker: {
    failure_count: 1,
    interval_seconds: 60,
    trip_duration_seconds: 60,
    accept_retry_after: true,
    status_code_ranges: [
      { minimum: 429, maximum: 429 },
      { minimum: 408, maximum: 408 },
      { minimum: 500, maximum: 599 },
    ],
    error_reasons: ["BackendConnectionFailure", "Timeout"],
  },
}

const TERMINAL_PUBLICATION_STATUSES = new Set<GatewayPublicationStatus>([
  "active",
  "failed",
  "awaiting_authorization",
  "rolled_back",
  "superseded",
])

const PUBLICATION_LABELS: Record<GatewayPublicationStatus, string> = {
  queued: "等待发布",
  validating: "正在创建 APIM 后端",
  provisioning: "正在创建候选 Revision",
  building_revision: "正在写入 Pool 策略",
  verifying: "正在逐成员验证",
  awaiting_authorization: "等待提供方授权",
  promoting: "正在切换生效 Revision",
  active: "配置已生效",
  failed: "发布失败",
  superseded: "已被后续发布替代",
  rolling_back: "正在回滚",
  rolled_back: "已回滚",
}

type PublicationNotice = {
  status: GatewayPublicationStatus
  message: string
}

type PublicationAction = "configure" | "remove"
export type DeploymentMode = "failover" | "balanced" | "weighted" | "custom"

function draftFromPool(
  pool: GatewayBackendPoolConfig | null,
  primaryRuntimeId: string,
): GatewayBackendPoolWrite {
  if (pool) {
    return {
      members: pool.members.map(({ runtime_id, priority, weight }) => ({
        runtime_id,
        priority,
        weight,
      })),
      rate_limit: pool.rate_limit,
      session_affinity: pool.session_affinity === true,
    }
  }
  return {
    members: primaryRuntimeId
      ? [{ runtime_id: primaryRuntimeId, priority: 0, weight: 1 }]
      : [],
    rate_limit: structuredClone(DEFAULT_RATE_LIMIT),
    session_affinity: false,
  }
}

function draftSignature(value: GatewayBackendPoolWrite) {
  return JSON.stringify(value)
}

export function deploymentMode(
  members: GatewayBackendPoolWrite["members"],
  primaryRuntimeId: string,
): DeploymentMode {
  const primary = members.find((member) => member.runtime_id === primaryRuntimeId)
  const replicas = members.filter((member) => member.runtime_id !== primaryRuntimeId)
  if (!primary || !replicas.length) return "failover"
  const equalWeights = new Set(members.map((member) => member.weight)).size === 1
  if (new Set(members.map((member) => member.priority)).size === 1) {
    return equalWeights ? "balanced" : "weighted"
  }
  if (
    equalWeights
    && replicas.every((member) => member.priority > primary.priority)
    && new Set(replicas.map((member) => member.priority)).size === 1
  ) return "failover"
  return "custom"
}

function membersForMode(
  members: GatewayBackendPoolWrite["members"],
  mode: Exclude<DeploymentMode, "custom">,
  primaryRuntimeId: string,
) {
  const hasCustomWeights = new Set(members.map((member) => member.weight)).size > 1
  return members.map((member) => ({
    ...member,
    priority: mode === "failover" && member.runtime_id !== primaryRuntimeId ? 1 : 0,
    weight: mode === "weighted"
      ? hasCustomWeights ? Math.max(1, member.weight) : member.runtime_id === primaryRuntimeId ? 2 : 1
      : 1,
  }))
}

export function DeploymentResilienceEditor({
  model,
  onActivated,
  renderFrame,
}: {
  model: ManagedModel
  onActivated?: () => void
  renderFrame?: (editor: ReactNode, controls: ReactNode) => ReactNode
}) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const registry = useQuery(finopsQueries.registry())
  const affinitySupported = registry.data?.backend_pool_session_affinity_supported === true
  const pool = useQuery(finopsQueries.modelBackendPool(model.id))
  const primaryRuntime = registry.data?.runtimes.find(
    (runtime) => runtime.id === model.runtime_id,
  )
  const compatibleRuntimes = useMemo(
    () => registry.data ? equivalentNativeRouteRuntimes(model, registry.data) : [],
    [model, registry.data],
  )
  const initialDraft = draftFromPool(null, model.runtime_id)
  const baselineRef = useRef(initialDraft)
  const [baselineSignature, setBaselineSignature] = useState(() =>
    draftSignature(initialDraft),
  )
  const [draft, setDraft] = useState<GatewayBackendPoolWrite>(initialDraft)
  const [mode, setMode] = useState<DeploymentMode | null>(null)
  const [publicationId, setPublicationId] = useState<string | null>(null)
  const [publicationAction, setPublicationAction] = useState<PublicationAction | null>(null)
  const [notice, setNotice] = useState<PublicationNotice | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)
  const publication = useQuery(finopsQueries.gatewayPublication(publicationId))

  useEffect(() => {
    if (!pool.isSuccess) return
    const next = draftFromPool(pool.data, model.runtime_id)
    const previous = baselineRef.current
    baselineRef.current = next
    setBaselineSignature(draftSignature(next))
    setMode(pool.data ? deploymentMode(next.members, model.runtime_id) : null)
    setDraft((current) => draftSignature(current) === draftSignature(previous)
      ? next
      : current)
  }, [model.id, model.runtime_id, pool.data, pool.dataUpdatedAt, pool.isSuccess])

  useEffect(() => {
    const status = publication.data?.status
    if (!publicationId || !status || !TERMINAL_PUBLICATION_STATUSES.has(status)) return
    if (status === "active") {
      setNotice({
        status,
        message: publicationAction === "remove"
          ? "APIM 后端池已移除，直接后端已恢复"
          : "APIM 部署韧性配置已生效",
      })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPool(model.id) })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
      if (publicationAction === "configure") onActivated?.()
    } else if (status === "failed" || status === "rolled_back") {
      setNotice({
        status,
        message: publication.data?.error_message ?? "APIM 后端池发布失败",
      })
    } else {
      setNotice({ status, message: PUBLICATION_LABELS[status] })
    }
    setPublicationId(null)
    setPublicationAction(null)
  }, [model.id, onActivated, publication.data, publicationAction, publicationId, queryClient])

  const save = useMutation({
    mutationFn: () => dataSource.saveModelBackendPool(
      model.id, poolPublicationPayload(draft, affinitySupported),
    ),
    onSuccess: (accepted) => {
      setNotice(null)
      setPublicationAction("configure")
      setPublicationId(accepted.publication.id)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
    },
  })
  const remove = useMutation({
    mutationFn: () => dataSource.deleteModelBackendPool(model.id),
    onSuccess: (accepted) => {
      setRemoveOpen(false)
      setNotice(null)
      setPublicationAction("remove")
      setPublicationId(accepted.publication.id)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
    },
  })

  const publicationStatus = publication.data?.status
  const publishing = Boolean(
    publicationId
    && publicationStatus
    && !TERMINAL_PUBLICATION_STATUSES.has(publicationStatus),
  )
  const configuring = save.isPending || (publishing && publicationAction === "configure")
  const removing = remove.isPending || (publishing && publicationAction === "remove")
  const busy = configuring || removing
  const dirty = draftSignature(draft) !== baselineSignature
  const validMemberCount = draft.members.length >= 2 && draft.members.length <= 30
  const selectedWeights = draft.members.map((member) => member.weight)
  const weightedIsDistinct = mode !== "weighted" || new Set(selectedWeights).size > 1
  const validDraft = validMemberCount && weightedIsDistinct
  const hasEffectivePool = Boolean(pool.data)
  const hasPendingPool = !hasEffectivePool && validMemberCount
  const visiblePublicationStatus = publicationStatus ?? notice?.status
  const visiblePublicationMessage = publicationStatus
    ? PUBLICATION_LABELS[publicationStatus]
    : notice?.message
  const publicationHasError = visiblePublicationStatus === "failed"
    || visiblePublicationStatus === "rolled_back"
  const selectedIds = new Set(draft.members.map((member) => member.runtime_id))
  const effectiveMemberIds = new Set(pool.data?.members.map((member) => member.runtime_id))
  const visibleRuntimes = (registry.data?.runtimes ?? [])
    .filter((runtime) =>
      compatibleRuntimes.some((candidate) => candidate.id === runtime.id)
        || effectiveMemberIds.has(runtime.id),
    )
    .sort((left, right) => {
      if (left.id === model.runtime_id) return -1
      if (right.id === model.runtime_id) return 1
      const selectedDifference = Number(selectedIds.has(right.id)) - Number(selectedIds.has(left.id))
      return selectedDifference || left.name.localeCompare(right.name)
    })
  const toggleRuntime = (runtimeId: string, selected: boolean) => {
    const nextMode = mode === "balanced" || mode === "weighted" ? mode : "failover"
    if (selected && mode === null) setMode(nextMode)
    setDraft((current) => ({
      ...current,
      members: selected
        ? [
            ...current.members,
            {
              runtime_id: runtimeId,
              priority: nextMode !== "failover" || runtimeId === model.runtime_id ? 0 : 1,
              weight: 1,
            },
          ]
        : current.members.filter((member) => member.runtime_id !== runtimeId),
    }))
  }
  const changeMode = (nextMode: Exclude<DeploymentMode, "custom">) => {
    setMode(nextMode)
    setDraft((current) => ({
      ...current,
      members: membersForMode(current.members, nextMode, model.runtime_id),
    }))
  }
  const changeWeight = (runtimeId: string, weight: number) => {
    setDraft((current) => ({
      ...current,
      members: current.members.map((member) => member.runtime_id === runtimeId
        ? { ...member, weight: Math.min(100, Math.max(1, weight || 1)) }
        : member),
    }))
  }
  const totalWeight = draft.members.reduce((sum, member) => sum + member.weight, 0)

  const error = save.error ?? remove.error ?? pool.error ?? publication.error
  const canConfigure = canManage && (hasEffectivePool || compatibleRuntimes.length >= 2)
  const changeActions = canConfigure && (dirty || configuring) ? <span className="apim-native-route-change-actions">
    {dirty && !configuring && <span className="apim-native-route-dirty">未保存</span>}
    <Button className="router-action-primary" type="button" size="sm" disabled={busy || !validDraft} onClick={() => save.mutate()}>{configuring ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{configuring ? "正在发布" : "发布到 APIM"}</Button>
  </span> : null
  const disableAction = canConfigure && pool.data
    ? <Button className="apim-native-route-disable" type="button" variant="destructive" size="sm" disabled={busy} onClick={() => setRemoveOpen(true)}><PowerOff size={13} />停用 Pool</Button>
    : null
  const externalControls = pool.isLoading || registry.isLoading || !primaryRuntime || primaryRuntime.config.control_plane_managed !== true ? null : <>
    <span className="apim-native-route-status" data-active={hasEffectivePool} data-pending={hasPendingPool}>{hasEffectivePool ? "已生效" : hasPendingPool ? "待发布" : "未启用"}</span>
    {changeActions}
    {disableAction}
  </>
  const editor = <section className="router-deployment-resilience">
    {pool.isLoading || registry.isLoading ? <div className="router-resilience-state"><LoaderCircle className="spin" size={14} />加载 APIM 后端池</div> : !primaryRuntime || primaryRuntime.config.control_plane_managed !== true ? <div className="router-resilience-state warning"><CircleAlert size={14} />此模型不支持 APIM 后端池</div> : <>
      <div className="router-resilience-toolbar">
        <span><b>部署模式</b><small>选择同一模型的物理 Deployment 如何接收流量。</small></span>
        {!renderFrame && <i data-active={hasEffectivePool} data-pending={hasPendingPool}>{hasEffectivePool ? "已生效" : hasPendingPool ? "待发布" : "未启用"}</i>}
      </div>
      <ButtonGroup className="usage-metric-segment router-deployment-mode" aria-label="切换 APIM 部署模式">
        <button type="button" className={validMemberCount && mode === "failover" ? "active" : ""} aria-pressed={validMemberCount && mode === "failover"} onClick={() => changeMode("failover")} disabled={!canManage || busy || !validMemberCount}><span className="router-mode-label-full">主备切换</span><span className="router-mode-label-short">主备</span></button>
        <button type="button" className={validMemberCount && mode === "balanced" ? "active" : ""} aria-pressed={validMemberCount && mode === "balanced"} onClick={() => changeMode("balanced")} disabled={!canManage || busy || !validMemberCount}><span className="router-mode-label-full">均衡分流</span><span className="router-mode-label-short">均衡</span></button>
        <button type="button" className={validMemberCount && mode === "weighted" ? "active" : ""} aria-pressed={validMemberCount && mode === "weighted"} onClick={() => changeMode("weighted")} disabled={!canManage || busy || !validMemberCount}><span className="router-mode-label-full">加权分流</span><span className="router-mode-label-short">加权</span></button>
      </ButtonGroup>
      {mode === "custom" && <div className="router-resilience-state warning"><CircleAlert size={14} />历史自定义配置只读</div>}
      {mode === "weighted" && !weightedIsDistinct && <div className="router-resilience-state warning"><CircleAlert size={14} />请设置至少两个不同权重；相同权重请使用均衡分流。</div>}
      <div className="router-resilience-runtime-list" role="list" aria-label={`${model.display_name} APIM Backend Pool Runtime`}>
        {visibleRuntimes.map((runtime) => {
          const selected = selectedIds.has(runtime.id)
          const member = draft.members.find((candidate) => candidate.runtime_id === runtime.id)
          const primary = runtime.id === model.runtime_id
          const compatible = compatibleRuntimes.some(
            (candidate) => candidate.id === runtime.id,
          )
          const shownSelected = selected
          const membershipState = effectiveMemberIds.has(runtime.id) ? "已生效" : selected ? "待发布" : "可选副本"
          const role = primary ? "主部署" : selected ? mode === "failover" ? "备用部署" : "成员" : "可选副本"
          const trafficShare = member && totalWeight > 0
            ? Math.round(member.weight / totalWeight * 100)
            : 0
          const authStrategy = String(runtime.config.auth_strategy ?? "none")
          const authLabel = authStrategy === "managed_identity" ? "Managed identity" : authStrategy.includes("api_key") ? "API key" : authStrategy.includes("bearer") ? "Bearer token" : "No auth"
          return <div className="router-resilience-runtime" role="listitem" data-selected={shownSelected} key={runtime.id}>
            <label><Checkbox checked={shownSelected} disabled={!canManage || busy || primary || (!selected && draft.members.length >= 30)} onCheckedChange={(checked) => toggleRuntime(runtime.id, checked === true)} /><span><b data-no-localize title={runtime.name}>{runtime.name}</b><small><span data-no-localize>{runtime.gateway_name ?? "APIM Runtime"}</span><i>·</i><span data-no-localize>{authLabel}</span>{!compatible && <><i>·</i><span>不再兼容</span></>}<i>·</i><span>{membershipState}</span></small></span></label>
            <div className="router-resilience-member-meta">
              <span className="router-resilience-member-role" data-primary={primary} data-selected={selected}>{role}</span>
              {member && <span className="router-resilience-member-priority"><small>APIM</small><b>{`P${member.priority + 1}`}</b></span>}
              {mode === "weighted" && member ? <label className="router-resilience-member-weight"><small>权重</small><Input aria-label={`${runtime.name} 权重`} type="number" min="1" max="100" value={member.weight} disabled={!canManage || busy} onChange={(event) => changeWeight(runtime.id, Number(event.target.value))} /><em>{trafficShare}%</em></label> : member ? <span className="router-resilience-member-weight-readonly"><small>权重</small><b>{member.weight}</b>{mode !== "failover" && <em>{trafficShare}%</em>}</span> : null}
            </div>
          </div>
        })}
      </div>

      <div className="router-resilience-affinity">
        <div className="router-resilience-affinity-control">
          <label><Checkbox checked={draft.session_affinity === true} disabled={!canManage || busy || !affinitySupported || !validMemberCount} onCheckedChange={(checked) => setDraft((current) => ({ ...current, session_affinity: checked === true }))} /><span>会话黏着</span></label>
          <FieldHelp>客户端须按对话保留 APIM Cookie。故障时可切换部署；调整池成员或配置可能重新分配会话。不会锁定 Azure 实际处理 Region。</FieldHelp>
          <span className="router-resilience-affinity-state">{pool.data?.session_affinity ? "黏着已生效" : draft.session_affinity ? "黏着待发布" : "黏着已关闭"}</span>
        </div>
        {!affinitySupported && <div className="router-resilience-state warning"><CircleAlert size={14} />当前后端未发布会话黏着能力。</div>}
        {draft.session_affinity && <dl><div><dt>绑定范围</dt><dd>Pool 后端端点</dd></div><div><dt>会话传递</dt><dd>Cookie</dd></div><div><dt>故障行为</dt><dd>允许故障切换</dd></div></dl>}
      </div>

      <div className="router-resilience-policy" data-active={hasEffectivePool} data-pending={hasPendingPool}>
        <div><ShieldCheck size={14} /><span><b>{hasEffectivePool ? "自动故障隔离已启用" : hasPendingPool ? "自动故障隔离待发布" : "自动故障隔离未启用"}</b><small>平台管理的 Circuit Breaker；仅 429 重试当前请求</small></span></div>
        <dl><div><dt>故障信号</dt><dd>429 · 408 · 5xx · Connection · Timeout</dd></div><div><dt>熔断时间</dt><dd>{draft.rate_limit.circuit_breaker.trip_duration_seconds}s</dd></div><div><dt>最大尝试</dt><dd>{draft.rate_limit.max_attempts_per_request}</dd></div><div><dt>重试间隔</dt><dd>{draft.rate_limit.retry_interval_seconds}s</dd></div><div><dt>首次快速重试</dt><dd>{draft.rate_limit.first_fast_retry ? "启用" : "关闭"}</dd></div><div><dt>后端超时</dt><dd>{draft.rate_limit.backend_timeout_seconds}s</dd></div><div><dt>后端 Retry-After</dt><dd>{draft.rate_limit.circuit_breaker.accept_retry_after ? "采用" : "忽略"}</dd></div></dl>
      </div>

      {!canManage && <div className="router-resilience-state"><ShieldCheck size={14} />只读</div>}
      {compatibleRuntimes.length < 2 && <div className="router-resilience-state warning"><CircleAlert size={14} />当前只有一个 Runtime，APIM 后端池未启用</div>}
      {compatibleRuntimes.length >= 2 && draft.members.length < 2 && <div className="router-resilience-state warning"><CircleAlert size={14} />请选择至少 1 个备用 Runtime</div>}
      {visiblePublicationStatus && visiblePublicationMessage && <div className="router-resilience-state" data-status={visiblePublicationStatus}>{publishing ? <LoaderCircle className="spin" size={14} /> : publicationHasError || visiblePublicationStatus === "awaiting_authorization" ? <CircleAlert size={14} /> : <CheckCircle2 size={14} />}<span>{visiblePublicationMessage}</span></div>}
      {error && <div className="router-resilience-state error"><CircleAlert size={14} /><span data-no-localize>{String(error)}</span></div>}
      {!renderFrame && (disableAction || changeActions) && <footer>{disableAction}{changeActions}</footer>}
      <AlertDialog open={removeOpen} onOpenChange={(open) => { if (!busy) setRemoveOpen(open) }}>
        <AlertDialogContent className="model-delete-dialog router-confirm-dialog">
          <div className="model-delete-body">
            <AlertDialogTitle>停用 APIM 后端池？</AlertDialogTitle>
            <AlertDialogDescription>恢复单一后端，并影响该模型的所有调用。</AlertDialogDescription>
          </div>
          <AlertDialogFooter className="model-delete-footer">
            <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
            <AlertDialogAction variant="destructive" disabled={busy} onClick={() => remove.mutate()}>{remove.isPending ? "正在提交" : "停用"}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>}
  </section>
  return renderFrame ? renderFrame(editor, externalControls) : editor
}