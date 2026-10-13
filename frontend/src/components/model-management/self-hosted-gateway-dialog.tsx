import { useEffect, useId, useRef, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { CheckCircle2, Circle, CircleAlert, Copy, Download, Link2, Plus, RefreshCw, Server, X } from "lucide-react"

import { dataSource, requestGatewayBootstrap } from "../../data-sources/apim/api"
import type { ApimTargetTopology, GatewayReleaseOperation, SelfHostedGatewayCreate } from "../../data-sources/apim/types"
import { consumeGatewayBootstrap, downloadGatewayEnvironment, type GatewayBootstrapMetadata } from "../../lib/gateway-bootstrap"
import { gatewayCreateFormPayload, gatewayCreateFormValid, isGatewayEndpoint } from "../../lib/gateway-forms"
import { gatewayProvisionDetails, type GatewayProvisionItem } from "../../lib/gateway-topology"
import { useLocale } from "../../locales"
import { useTimezone } from "../../providers/timezone-provider"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"

const hostingModes = { docker: "Docker", aks: "Azure Kubernetes 服务", kubernetes: "Kubernetes", arc: "Azure Arc", container_apps: "Azure 容器应用", other: "其他" } as const

export function SelfHostedGatewayCreateDialog({ targets, initialTargetId, available, onClose, onAdopt, onAccepted, onSettled }: {
  targets: ApimTargetTopology[]; initialTargetId: string | null; available: boolean
  onClose: () => void; onAdopt: (targetId: string | null) => void
  onAccepted: (operation: GatewayReleaseOperation) => void | Promise<void>; onSettled: () => void
}) {
  const prefix = useId()
  const choices = targets.filter(item => item.target.capabilities.self_hosted_gateway === "supported"
    && item.target.ownership_mode !== "read_only" && item.target.drift_state === "in_sync")
  const [targetId, setTargetId] = useState(initialTargetId ?? choices[0]?.target.gateway_profile_id ?? "")
  const [value, setValue] = useState<SelfHostedGatewayCreate>({ display_name: "", endpoint_url: "", location: "",
    hosting_mode: "docker", release_gate: "required", endpoint_control_confirmed: false })
  const [requestId] = useState(() => crypto.randomUUID())
  const submission = useRef<{ targetId: string; value: SelfHostedGatewayCreate; requestId: string } | null>(null)
  const submitting = useRef(false)
  const start = useMutation({
    mutationFn: (intent: NonNullable<typeof submission.current>) => dataSource.createSelfHostedGateway(intent.targetId, intent.value, intent.requestId),
    onSuccess: accepted => onAccepted(accepted.operation), onSettled, retry: false,
  })
  const selected = choices.find(item => item.target.gateway_profile_id === targetId)
  const locked = start.isPending || submission.current !== null
  const valid = available && Boolean(selected) && gatewayCreateFormValid(value)
  const invalidEndpoint = Boolean(value.endpoint_url.trim()) && (!isGatewayEndpoint(value.endpoint_url)
    || new URL(value.endpoint_url).port !== "")
  const update = (field: "display_name" | "endpoint_url" | "location", next: string) => setValue(current => ({ ...current, [field]: next }))
  return <Dialog open onOpenChange={open => { if (!open && !submitting.current) onClose() }}>
    <DialogContent className="registry-editor-dialog infrastructure-editor-dialog gateway-create-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={async event => {
        event.preventDefault()
        if (!valid || submitting.current) return
        submitting.current = true
        submission.current ??= { targetId, value: gatewayCreateFormPayload(value), requestId }
        try { await start.mutateAsync(submission.current) } catch {} finally { submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>新建自托管网关</DialogTitle><DialogDescription>APIM 网关配置</DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={start.isPending} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body infrastructure-editor-body">
          <div className="registry-field"><span className="registry-field-label">APIM 实例</span><Select value={targetId} disabled={locked || initialTargetId !== null} onValueChange={next => setTargetId(next ?? "")}><SelectTrigger className="registry-select-trigger" aria-label="APIM 实例"><SelectValue><span data-no-localize>{selected?.target.service_name ?? ""}</span>{!selected && "选择 APIM 实例"}</SelectValue></SelectTrigger><SelectContent>{choices.map(item => <SelectItem key={item.target.gateway_profile_id} value={item.target.gateway_profile_id}><span data-no-localize>{item.target.service_name}</span></SelectItem>)}</SelectContent></Select></div>
          <label className="registry-field" htmlFor={`${prefix}-name`}><span className="registry-field-label">网关名称</span><Input id={`${prefix}-name`} name="display_name" autoFocus required maxLength={120} value={value.display_name} disabled={locked} onChange={event => update("display_name", event.target.value)} /></label>
          <label className="registry-field" htmlFor={`${prefix}-endpoint`}><span className="registry-field-label">网关地址</span><Input id={`${prefix}-endpoint`} name="endpoint_url" type="url" required value={value.endpoint_url} disabled={locked} aria-invalid={invalidEndpoint} aria-describedby={invalidEndpoint ? `${prefix}-endpoint-error` : undefined} placeholder={`https://gateway.example.com/${selected?.target.api_path ?? "api-path"}`} onChange={event => update("endpoint_url", event.target.value)} /></label>
          {invalidEndpoint && <span className="infrastructure-field-error" id={`${prefix}-endpoint-error`}>使用标准 HTTPS 地址，不得包含凭据、查询参数或片段。</span>}
          <label className="registry-field" htmlFor={`${prefix}-location`}><span className="registry-field-label">位置（可选）</span><Input id={`${prefix}-location`} name="location" maxLength={128} value={value.location ?? ""} disabled={locked} onChange={event => update("location", event.target.value)} /></label>
          <div className="registry-field"><span className="registry-field-label">托管方式</span><Select value={value.hosting_mode} disabled={locked} onValueChange={next => { if (next && next in hostingModes) setValue(current => ({ ...current, hosting_mode: next as SelfHostedGatewayCreate["hosting_mode"] })) }}><SelectTrigger className="registry-select-trigger" aria-label="托管方式"><SelectValue>{hostingModes[value.hosting_mode]}</SelectValue></SelectTrigger><SelectContent>{Object.entries(hostingModes).map(([mode, label]) => <SelectItem key={mode} value={mode}>{label}</SelectItem>)}</SelectContent></Select></div>
          <label className="gateway-endpoint-confirmation"><Checkbox checked={value.endpoint_control_confirmed} disabled={locked} onCheckedChange={checked => setValue(current => ({ ...current, endpoint_control_confirmed: checked === true }))} /><span>我控制此服务器及 HTTPS 地址，允许 Turnstile 向此地址发送网关验证和模型调用凭据。</span></label>
          {(!available || !selected) && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>{!available ? "平台网关创建能力尚未启用" : "没有已同步且支持自托管网关的 APIM 实例"}</span></div>}
          {start.error && <div className="registry-error" role="alert">{start.error.message}</div>}
        </div>
        <DialogFooter className="registry-editor-footer apim-onboarding-footer"><Button type="button" variant="ghost" disabled={start.isPending} onClick={() => onAdopt(targetId || null)}><Link2 size={14} />关联已有网关</Button><DialogClose render={<Button type="button" variant="outline" disabled={start.isPending} />}>取消</DialogClose><Button type="submit" disabled={!valid || start.isPending}>{start.isPending ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}{start.isPending ? "正在提交" : submission.current ? "重试提交" : "创建网关"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function SelfHostedGatewayProvisionProgress({ item, available, onAccepted, onSettled }: {
  item: GatewayProvisionItem; available: boolean
  onAccepted?: (operation: GatewayReleaseOperation) => void | Promise<void>; onSettled?: () => void
}) {
  const { operation, target } = item
  const details = gatewayProvisionDetails(operation)
  const submitting = useRef(false)
  const retry = useMutation({ mutationFn: () => dataSource.retrySelfHostedGatewayProvision(operation.id),
    onSuccess: accepted => onAccepted?.(accepted.operation), onSettled, retry: false })
  const failed = operation.status === "failed"
  const status = failed ? "创建失败" : operation.worker_available ? "正在创建网关" : "创建已排队，工作进程当前不可用"
  const steps = [{ label: "创建 APIM 网关配置", done: operation.checkpoint.apim_gateway_created === true },
    { label: "关联 API", done: operation.checkpoint.api_associated === true },
    { label: "读回确认", done: false }]
  return <section className="connection-provider-group gateway-provision-item" role="rowgroup" aria-label={details.name || "网关创建任务"} data-operation-id={operation.id}>
    <article className="model-list-row infrastructure-resource-row gateway gateway-provision-row" role="row">
      <div className="model-primary-cell" role="cell"><span className="model-list-icon gateway-child-icon" data-model-family="apim"><Server size={15} /></span><div><b data-no-localize title={details.name}>{details.name || details.nativeId}</b><code data-no-localize title={details.nativeId}>{details.nativeId}</code></div></div>
      <div className="model-runtime-cell" role="cell"><b data-no-localize title={target.target.service_name}>{target.target.service_name}</b><span data-no-localize>{target.target.api_id}</span></div>
      <div className="model-pricing-cell" role="cell"><b data-no-localize title={details.endpoint}>{details.endpoint}</b><span title={target.configuration_endpoint}><span>配置端点</span> · <span data-no-localize>{target.configuration_endpoint}</span></span></div>
      <div className="model-status-cell" role="cell"><span className={`gateway-readiness gateway-provision-status ${failed ? "unavailable" : "warning"}`} role="status"><span>{failed ? <CircleAlert size={13} /> : <RefreshCw size={13} className={operation.worker_available ? "spin" : ""} />}{status}</span></span></div>
      <div className="model-row-actions infrastructure-row-actions" role="cell">{failed && <button type="button" className="gateway-provision-action" title="重试创建" aria-label="重试创建" disabled={!available || retry.isPending} onClick={async () => {
        if (!available || submitting.current) return
        submitting.current = true
        try { await retry.mutateAsync() } catch {} finally { submitting.current = false }
      }}><RefreshCw size={14} className={retry.isPending ? "spin" : ""} /></button>}</div>
    </article>
    <div className="gateway-provision-details" role="row"><div role="cell" aria-colspan={5}>
      <ol className="apim-onboarding-steps">{steps.map(step => <li key={step.label} data-complete={step.done}>{step.done ? <CheckCircle2 size={13} /> : <Circle size={13} />}<span>{step.label}</span></li>)}</ol>
      {(retry.error || operation.error_message) && <p className="gateway-provision-error" role="alert">{retry.error?.message ?? operation.error_message}</p>}
    </div></div>
  </section>
}

export function SelfHostedGatewayBootstrapDialog({ deploymentId, name, onClose, onValidate }: {
  deploymentId: string; name: string; onClose: () => void; onValidate: () => void
}) {
  const { locale } = useLocale()
  const { timezone } = useTimezone()
  const [hours, setHours] = useState(1)
  const [confirmed, setConfirmed] = useState(false)
  const [trustedProxy, setTrustedProxy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [summary, setSummary] = useState<GatewayBootstrapMetadata | null>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => { controller.current?.abort(); controller.current = null }, [])
  const close = () => { controller.current?.abort(); controller.current = null; setSummary(null); setError(null); onClose() }
  const generate = async () => {
    if (controller.current || !confirmed || !Number.isInteger(hours) || hours < 1 || hours > 24) return
    const current = new AbortController()
    controller.current = current
    setBusy(true); setError(null); setSummary(null)
    try {
      const result = await requestGatewayBootstrap(deploymentId, { endpoint_control_confirmed: confirmed,
        expires_in_hours: hours, trusted_reverse_proxy: trustedProxy }, AbortSignal.any([current.signal, AbortSignal.timeout(30000)]))
      if (current.signal.aborted || controller.current !== current) { result.environment_file = ""; return }
      setSummary(consumeGatewayBootstrap(result, deploymentId, downloadGatewayEnvironment))
    } catch {
      if (!current.signal.aborted && controller.current === current) setError("启动配置暂不可用")
    } finally {
      if (controller.current === current) { controller.current = null; setBusy(false) }
    }
  }
  const copy = async (command: string) => { try { await navigator.clipboard.writeText(command) } catch { setError("复制失败") } }
  return <Dialog open onOpenChange={open => { if (!open) close() }}>
    <DialogContent className="registry-editor-dialog infrastructure-editor-dialog gateway-bootstrap-dialog" finalFocus={false}>
      <div className="registry-editor">
        <DialogHeader className="registry-editor-header"><DialogTitle>启动配置与续期</DialogTitle><DialogDescription><span data-no-localize>{name}</span></DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body infrastructure-editor-body">
          <label className="registry-field"><span className="registry-field-label">令牌有效期（小时）</span><Input type="number" min={1} max={24} step={1} value={hours || ""} disabled={busy} onChange={event => setHours(Number(event.target.value))} /></label>
          <label className="gateway-endpoint-confirmation"><Checkbox checked={confirmed} disabled={busy} onCheckedChange={setConfirmed} /><span>我控制此服务器及 HTTPS 地址，允许 Turnstile 向此地址发送网关验证和模型调用凭据。</span></label>
          <label className="gateway-endpoint-confirmation"><Checkbox checked={trustedProxy} disabled={busy} onCheckedChange={setTrustedProxy} /><span>仅允许同机受信任且隔离的 TLS 反向代理访问容器 HTTP 端口</span></label>
          {trustedProxy && <div className="infrastructure-warning" role="note"><CircleAlert size={15} /><span><code data-no-localize>net.server.http.forwarded.proto.enabled=true</code><br />代理必须覆盖转发协议头，禁止不可信来源直连。公网入口仍须使用 HTTPS。</span></div>}
          <div className="infrastructure-warning" role="note"><CircleAlert size={15} /><span>令牌不会自动续期；容器运行或健康检查成功不等于连接已验证。</span></div>
          {summary && <>
            <dl className="gateway-bootstrap-metadata"><dt>官方镜像</dt><dd><code data-no-localize>{summary.image}</code></dd><dt>配置地址</dt><dd><code data-no-localize>{summary.configuration_endpoint}</code></dd><dt>到期时间</dt><dd data-no-localize>{new Date(summary.expires_at).toLocaleString(locale, { timeZone: timezone })}</dd><dt>已下载环境文件</dt><dd><code data-no-localize>{summary.environment_file_name}</code></dd></dl>
            {[{ label: "启动命令", value: summary.docker_command }, { label: "更新令牌后重建容器", value: summary.renewal_command }].map(command => <section className="gateway-bootstrap-command" key={command.label}><div><b>{command.label}</b><Button type="button" variant="ghost" size="icon-sm" title="复制命令" aria-label="复制命令" onClick={() => void copy(command.value)}><Copy size={14} /></Button></div><pre data-no-localize>{command.value}</pre></section>)}
          </>}
          {error && <div className="registry-error" role="alert">{error}</div>}
        </div>
        <DialogFooter className="registry-editor-footer apim-onboarding-footer"><Button type="button" variant="outline" onClick={close}>关闭</Button><Button type="button" variant="outline" disabled={busy} onClick={onValidate}><RefreshCw size={14} />验证连接</Button><Button type="button" disabled={busy || !confirmed || !Number.isInteger(hours) || hours < 1 || hours > 24} onClick={() => void generate()}>{busy ? <RefreshCw size={14} className="spin" /> : <Download size={14} />}{busy ? "正在签发" : "签发并下载配置"}</Button></DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
}