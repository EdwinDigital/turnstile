import { useRef } from "react"
import { useMutation, useMutationState, useQueryClient } from "@tanstack/react-query"
import { CircleHelp, RefreshCw, TriangleAlert, X } from "lucide-react"

import { ApiError, dataSource } from "../../data-sources/apim/api"
import { finopsKeys } from "../../data-sources/apim/queries"
import type {
  ManagedModel, ModelAvailabilityCommand, ModelAvailabilityList, ModelAvailabilityReadback, ModelRegistry,
} from "../../data-sources/apim/types"
import { availabilityPresentation } from "../../lib/model-availability"
import { runtimeHealthDescription, runtimeHealthLabels, runtimeHealthState } from "../../lib/runtime-health"
import { getIntlLocale } from "../../locales"
import { useAuth } from "../../providers/auth-provider"
import { useTimezone } from "../../providers/timezone-provider"
import { Button } from "../ui/button"
import { RegistryNoticeBanner } from "../ui/registry-notice-banner"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Switch } from "../ui/switch"

type AvailabilityRequest =
  | { action: "change"; command: ModelAvailabilityCommand }
  | { action: "retry"; operationId: string; expectedRevision: number; enabled: boolean }

const ownerOnlyMessage = "仅 Owner 可修改模型状态"

export function useModelAvailabilityCommand(modelId: string, readback?: ModelAvailabilityReadback) {
  const client = useQueryClient()
  const canManage = useAuth().user?.role === "owner"
  const mutationKey = ["model-availability-command", modelId]
  const submissions = useMutationState({
    filters: { mutationKey, exact: true, status: "pending" },
    select: mutation => mutation.state.variables as AvailabilityRequest,
  })
  const submission = submissions[submissions.length - 1]
  const pending = submissions.length > 0
  const requestedEnabled = submission?.action === "change" ? submission.command.enabled : submission?.enabled
  const command = useRef<ModelAvailabilityCommand | null>(null)
  const mutation = useMutation({
    mutationKey,
    retry: false,
    mutationFn: (request: AvailabilityRequest) => {
      if (!canManage) throw new Error(ownerOnlyMessage)
      if (!readback || readback.model_id !== modelId) throw new Error("模型状态尚未验证")
      if (request.action === "retry") {
        return dataSource.retryModelAvailability(request.operationId, request.expectedRevision)
      }
      return dataSource.changeModelAvailability(modelId, request.command)
    },
    onSuccess: async ({ operation }) => {
      command.current = null
      await client.cancelQueries({ queryKey: finopsKeys.modelAvailability })
      client.setQueryData<ModelAvailabilityList>(finopsKeys.modelAvailability, (current) => current ? {
        items: current.items.map((item) => item.model_id === modelId && item.desired_revision <= operation.desired_revision ? {
          ...item,
          desired_enabled: operation.desired_enabled,
          desired_revision: operation.desired_revision,
          status: operation.status,
          command_ready: false,
          operation,
        } : item),
      } : current)
      void client.invalidateQueries({ queryKey: finopsKeys.registry })

    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) command.current = null
    },
    onSettled: () => { void client.invalidateQueries({ queryKey: finopsKeys.modelAvailability }) },
  })
  const presentation = availabilityPresentation(readback, pending, false, requestedEnabled)
  return {
    ...presentation,
    canChange: canManage && presentation.canChange,
    canRetry: canManage && presentation.canRetry,
    submitting: pending,
    requestedEnabled,
    error: !pending && mutation.error ? mutation.error instanceof ApiError && mutation.error.status === 409
      ? "模型状态已变化，请刷新后重试。" : "请求结果未确认，请刷新状态。" : null,
    change: () => {
      if (!canManage || !readback || !presentation.canChange || client.isMutating({ mutationKey, exact: true }) > 0) return
      if (!command.current || command.current.expected_revision !== readback.desired_revision) {
        command.current = {
          enabled: !readback.desired_enabled,
          expected_revision: readback.desired_revision,
          idempotency_key: crypto.randomUUID(),
        }
      }
      mutation.mutate({ action: "change", command: command.current })
    },
    retry: () => {
      if (!canManage || !readback?.operation || !presentation.canRetry || client.isMutating({ mutationKey, exact: true }) > 0) return
      mutation.mutate({ action: "retry", operationId: readback.operation.id,
        expectedRevision: readback.desired_revision, enabled: readback.desired_enabled })
    },
  }
}

export function ModelAvailabilitySwitch({ readback, registryEnabled, disabled, loading, pending, requestedEnabled, onChange }: {
  readback?: ModelAvailabilityReadback
  registryEnabled: boolean
  disabled?: boolean
  loading?: boolean
  pending?: boolean
  requestedEnabled?: boolean
  onChange: () => void
}) {
  const canManage = useAuth().user?.role === "owner"
  const state = availabilityPresentation(readback, pending, registryEnabled, requestedEnabled)
  const reading = loading === true && !readback
  const progressLabel = state.direction === "enabling" ? "正在启用" : "正在停用"
  const unavailableLabel = reasonLabels[readback?.reason ?? ""] ?? "模型状态尚未验证"
  return <Switch
    className="toggle model-availability-toggle"
    checked={state.checked}
    disabled={disabled || !canManage || !state.canChange}
    aria-label={!canManage ? ownerOnlyMessage : state.inFlight ? progressLabel : state.checked ? "停用此模型" : "启用此模型"}
    aria-busy={reading || state.inFlight || undefined}
    title={!canManage ? ownerOnlyMessage : reading ? "正在读取状态" : state.inFlight ? progressLabel : !readback?.command_ready ? unavailableLabel : readback.desired_enabled ? "停用此模型" : "启用此模型"}
    onCheckedChange={() => { if (canManage && !disabled && state.canChange) onChange() }}
  />
}

export function ModelAvailabilityStatus({ readback, loading, pending, requestedEnabled, error, onOpen }: {
  readback?: ModelAvailabilityReadback
  loading?: boolean
  pending?: boolean
  requestedEnabled?: boolean
  error?: string | null
  onOpen?: () => void
}) {
  const state = availabilityPresentation(readback, pending, false, requestedEnabled)
  const reading = loading === true && !readback && !error
  const label = error ? "请求未确认"
    : reading ? "正在读取状态"
    : state.state === "succeeded" ? readback?.effective_enabled ? "已启用" : "已停用"
    : state.state === "partial" ? "部分生效"
    : state.state === "failed" ? "同步失败"
    : state.inFlight ? state.direction === "enabling" ? "正在启用" : "正在停用"
    : "未验证"
  const content = <>{error || state.state === "failed" || state.state === "partial" ? <TriangleAlert size={12} />
    : reading || state.inFlight ? <RefreshCw className="spin" size={12} />
    : state.verified ? <i aria-hidden="true" /> : <CircleHelp size={12} />}<span>{label}</span></>
  const className = `model-availability-status ${error ? "failed" : reading ? "pending" : state.state}${!error && state.verified
    ? ` registry-health ${readback?.effective_enabled ? "available" : "unavailable"}` : ""}`
  return onOpen
    ? <button type="button" className={className} title={error ?? label} onClick={onOpen}>{content}</button>
    : <span className={className}>{content}</span>
}

const targetStatus = { pending: "等待验证", verified: "已验证", failed: "验证失败", unknown: "未验证" }
const reasonLabels: Record<string, string> = {
  self_hosted_node_coverage_unverified: "自托管节点覆盖尚未验证",
  model_availability_unavailable: "模型状态尚未验证",
  model_availability_capability_unverified: "网关覆盖尚未验证",
  model_availability_initialization_failed: "初始启停确认失败，模型发布状态不受影响",
}

export function ModelAvailabilityDialog({ model, registry, readback, loading, readError, onRefresh, onClose }: {
  model: ManagedModel
  registry: ModelRegistry
  readback?: ModelAvailabilityReadback
  loading?: boolean
  readError: boolean
  onRefresh: () => void
  onClose: () => void
}) {
  const action = useModelAvailabilityCommand(model.id, readback)
  const { timezone } = useTimezone()
  const runtime = registry.runtimes.find((item) => item.id === model.runtime_id)
  const reading = loading === true && !readback && !readError
  const timestamp = (value: string | null | undefined) => value
    ? new Date(value).toLocaleString(getIntlLocale(), { timeZone: timezone }) : reading ? "正在读取状态" : "未验证"
  return <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
    <DialogContent className="registry-editor-dialog model-availability-dialog" finalFocus={false}>
      <DialogHeader className="registry-editor-header">
        <DialogTitle>模型启停状态</DialogTitle>
        <DialogDescription><span data-no-localize>{model.display_name}</span></DialogDescription>
      </DialogHeader>
      <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
      <div className="registry-editor-body model-availability-body">
        <div className="model-availability-summary"><ModelAvailabilityStatus readback={readback} loading={loading} pending={action.submitting} requestedEnabled={action.requestedEnabled} error={action.error} />
          <ModelAvailabilitySwitch readback={readback} loading={loading} registryEnabled={model.enabled} pending={action.submitting} requestedEnabled={action.requestedEnabled} onChange={action.change} />
        </div>
        <dl className="model-availability-facts">
          <div><dt>期望状态</dt><dd>{(readback?.desired_enabled ?? model.enabled) ? "已启用" : "已停用"}</dd></div>
          <div><dt>期望版本</dt><dd data-no-localize>{readback?.desired_revision ?? "--"}</dd></div>
          <div><dt>最后验证状态</dt><dd>{readback?.effective_enabled == null ? reading ? "正在读取状态" : "未验证" : readback.effective_enabled ? "已启用" : "已停用"}</dd></div>
          <div><dt>最后验证版本</dt><dd data-no-localize>{readback?.effective_revision ?? "--"}</dd></div>
          <div><dt>验证时间</dt><dd>{timestamp(readback?.verified_at)}</dd></div>
          <div><dt>连接健康</dt><dd title={runtime ? runtimeHealthDescription(runtime) : undefined}>{runtime ? runtimeHealthLabels[runtimeHealthState(runtime)] : "未验证"}</dd></div>
        </dl>
        {(readError || !readback || readback.reason) && <RegistryNoticeBanner notice={{
          text: reading ? "正在读取状态" : reasonLabels[readback?.reason ?? ""] ?? "网关覆盖尚未验证",
          ok: false,
          pending: reading,
          actionLabel: "刷新状态",
        }} onAction={onRefresh} />}
        {readback?.operation && <>
          <div className="model-availability-operation"><span>操作 ID</span><code data-no-localize>{readback.operation.id}</code></div>
          <ul className="model-availability-targets">
            {readback.operation.targets.map((target) => <li key={target.target_id}>
              <div><b data-no-localize>{registry.gateways.find((item) => item.id === target.target_id)?.name ?? target.target_id}</b><span>{targetStatus[target.status]}</span></div>
              <small>{timestamp(target.verified_at)}</small>
              {target.reason && <p>{reasonLabels[target.reason] ?? target.reason}</p>}
            </li>)}
          </ul>
        </>}
        {action.error && <div className="registry-error" role="alert">{action.error}</div>}
      </div>
      <DialogFooter className="registry-editor-footer">
        <Button type="button" variant="outline" onClick={onRefresh}><RefreshCw size={14} />刷新状态</Button>
        {action.canRetry && <Button type="button" onClick={action.retry}><RefreshCw size={14} />重试同步</Button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>
}