import { useRef } from "react"
import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query"
import { Check, CircleHelp, RefreshCw, TriangleAlert, X } from "lucide-react"

import { ApiError, dataSource } from "../../data-sources/apim/api"
import { finopsKeys } from "../../data-sources/apim/queries"
import type {
  ManagedModel, ModelAvailabilityCommand, ModelAvailabilityList, ModelAvailabilityReadback, ModelRegistry,
} from "../../data-sources/apim/types"
import { availabilityPresentation } from "../../lib/model-availability"
import { getIntlLocale } from "../../locales"
import { assistantSettingsKey } from "../assistant/queries"
import { Button } from "../ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Switch } from "../ui/switch"

export function useModelAvailabilityCommand(modelId: string, readback?: ModelAvailabilityReadback) {
  const client = useQueryClient()
  const mutationKey = ["model-availability-command", modelId]
  const pending = useIsMutating({ mutationKey }) > 0
  const command = useRef<ModelAvailabilityCommand | null>(null)
  const mutation = useMutation({
    mutationKey,
    retry: false,
    mutationFn: (action: "change" | "retry") => {
      if (!readback || readback.model_id !== modelId) throw new Error("模型状态尚未验证")
      if (action === "retry") {
        if (!readback.operation) throw new Error("模型状态尚未验证")
        return dataSource.retryModelAvailability(readback.operation.id, readback.desired_revision)
      }
      if (!command.current || command.current.expected_revision !== readback.desired_revision) {
        command.current = {
          enabled: !readback.desired_enabled,
          expected_revision: readback.desired_revision,
          idempotency_key: crypto.randomUUID(),
        }
      }
      return dataSource.changeModelAvailability(modelId, command.current)
    },
    onSuccess: ({ operation }) => {
      command.current = null
      client.setQueryData<ModelAvailabilityList>(finopsKeys.modelAvailability, (current) => current ? {
        items: current.items.map((item) => item.model_id === modelId ? {
          ...item,
          desired_enabled: operation.desired_enabled,
          desired_revision: operation.desired_revision,
          status: operation.status,
          command_ready: false,
          operation,
        } : item),
      } : current)
      void client.invalidateQueries({ queryKey: finopsKeys.registry })
      void client.invalidateQueries({ queryKey: assistantSettingsKey })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) command.current = null
    },
    onSettled: () => { void client.invalidateQueries({ queryKey: finopsKeys.modelAvailability }) },
  })
  const presentation = availabilityPresentation(readback, pending)
  return {
    ...presentation,
    error: mutation.error ? mutation.error instanceof ApiError && mutation.error.status === 409
      ? "模型状态已变化，请刷新后重试。" : "请求结果未确认，请刷新状态。" : null,
    change: () => {
      if (presentation.canChange && client.isMutating({ mutationKey }) === 0) mutation.mutate("change")
    },
    retry: () => {
      if (presentation.canRetry && client.isMutating({ mutationKey }) === 0) mutation.mutate("retry")
    },
  }
}

export function ModelAvailabilitySwitch({ readback, registryEnabled, disabled, pending, onChange }: {
  readback?: ModelAvailabilityReadback
  registryEnabled: boolean
  disabled?: boolean
  pending?: boolean
  onChange: () => void
}) {
  const state = availabilityPresentation(readback, pending, registryEnabled)
  return <Switch
    className="toggle model-availability-toggle"
    checked={state.checked}
    disabled={disabled || !state.canChange}
    aria-label={state.checked ? "停用此模型" : "启用此模型"}
    aria-busy={state.inFlight || undefined}
    title={!readback?.command_ready ? "模型状态尚未验证" : readback.desired_enabled ? "停用此模型" : "启用此模型"}
    onCheckedChange={onChange}
  />
}

export function ModelAvailabilityStatus({ readback, pending, error, onOpen }: {
  readback?: ModelAvailabilityReadback
  pending?: boolean
  error?: string | null
  onOpen?: () => void
}) {
  const state = availabilityPresentation(readback, pending)
  const label = error ? "请求未确认"
    : state.state === "succeeded" ? readback?.effective_enabled ? "已启用" : "已停用"
    : state.state === "partial" ? "部分生效"
    : state.state === "failed" ? "同步失败"
    : state.inFlight ? readback?.desired_enabled ? "正在启用" : "正在停用"
    : "未验证"
  const content = <>{error || state.state === "failed" || state.state === "partial" ? <TriangleAlert size={12} />
    : state.inFlight ? <RefreshCw className="spin" size={12} />
    : state.verified ? <Check size={12} /> : <CircleHelp size={12} />}<span>{label}</span></>
  const className = `model-availability-status ${error ? "failed" : state.state}`
  return onOpen
    ? <button type="button" className={className} title={error ?? label} onClick={onOpen}>{content}</button>
    : <span className={className}>{content}</span>
}

const targetStatus = { pending: "等待验证", verified: "已验证", failed: "验证失败", unknown: "未验证" }
const reasonLabels: Record<string, string> = {
  self_hosted_node_coverage_unverified: "自托管节点覆盖尚未验证",
  model_availability_unavailable: "模型状态尚未验证",
  model_availability_capability_unverified: "网关覆盖尚未验证",
}

export function ModelAvailabilityDialog({ model, registry, readback, readError, onRefresh, onClose }: {
  model: ManagedModel
  registry: ModelRegistry
  readback?: ModelAvailabilityReadback
  readError: boolean
  onRefresh: () => void
  onClose: () => void
}) {
  const action = useModelAvailabilityCommand(model.id, readback)
  const runtime = registry.runtimes.find((item) => item.id === model.runtime_id)
  const timestamp = (value: string | null | undefined) => value
    ? new Date(value).toLocaleString(getIntlLocale()) : "未验证"
  return <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
    <DialogContent className="registry-editor-dialog model-availability-dialog" finalFocus={false}>
      <DialogHeader className="registry-editor-header">
        <DialogTitle>模型启停状态</DialogTitle>
        <DialogDescription><span data-no-localize>{model.display_name}</span></DialogDescription>
      </DialogHeader>
      <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
      <div className="registry-editor-body model-availability-body">
        <div className="model-availability-summary"><ModelAvailabilityStatus readback={readback} pending={action.inFlight} error={action.error} />
          <ModelAvailabilitySwitch readback={readback} registryEnabled={model.enabled} pending={action.inFlight} onChange={action.change} />
        </div>
        <dl className="model-availability-facts">
          <div><dt>期望状态</dt><dd>{(readback?.desired_enabled ?? model.enabled) ? "已启用" : "已停用"}</dd></div>
          <div><dt>期望版本</dt><dd data-no-localize>{readback?.desired_revision ?? "--"}</dd></div>
          <div><dt>最后验证状态</dt><dd>{readback?.effective_enabled == null ? "未验证" : readback.effective_enabled ? "已启用" : "已停用"}</dd></div>
          <div><dt>最后验证版本</dt><dd data-no-localize>{readback?.effective_revision ?? "--"}</dd></div>
          <div><dt>验证时间</dt><dd>{timestamp(readback?.verified_at)}</dd></div>
          <div><dt>连接健康</dt><dd>{runtime?.health_status === "available" ? "在线" : runtime?.health_status === "unavailable" ? "离线" : "未检查"}</dd></div>
        </dl>
        {(readError || !readback || readback.reason) && <div className="registry-progress" role="status">{reasonLabels[readback?.reason ?? ""] ?? "网关覆盖尚未验证"}</div>}
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