import { useEffect, useId, useRef, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { CheckCircle2, Circle, CircleAlert, Copy, Download, Link2, Plus, RefreshCw, Server, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import type { ApimTargetTopology, GatewayReleaseOperation, SelfHostedGatewayBootstrap, SelfHostedGatewayCreate } from "../../data-sources/apim/types"
import { gatewayCreateFormPayload, gatewayCreateFormValid, isGatewayEndpoint, type GatewayCreateForm } from "../../lib/gateway-forms"
import { useLocale } from "../../locales"
import { useTimezone } from "../../providers/timezone-provider"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

export function SelfHostedGatewayCreateDialog({ targets, initialTargetId, available, onClose, onAdopt, onAccepted, onSettled }: {
  targets: ApimTargetTopology[]; initialTargetId: string | null; available: boolean
  onClose: () => void; onAdopt: (targetId: string | null) => void
  onAccepted: (operation: GatewayReleaseOperation) => void | Promise<void>; onSettled: () => void
}) {
  const prefix = useId()
  const choices = targets.filter(item => item.target.capabilities.self_hosted_gateway === "supported" && item.target.ownership_mode !== "read_only")
  const [targetId, setTargetId] = useState(initialTargetId ?? choices[0]?.target.gateway_profile_id ?? "")
  const [value, setValue] = useState<GatewayCreateForm>({ display_name: "", endpoint_url: "", location: "", hosting_mode: "docker", release_gate: "required", endpoint_control_confirmed: false })
  const [requestId] = useState(() => crypto.randomUUID())
  const request = useRef<{ targetId: string; value: SelfHostedGatewayCreate; requestId: string } | null>(null)
  const submitting = useRef(false)
  const start = useMutation({
    mutationFn: (submission: NonNullable<typeof request.current>) => dataSource.createSelfHostedGateway(submission.targetId, submission.value, submission.requestId),
    onSuccess: accepted => onAccepted(accepted.operation), onSettled,
  })
  const selected = choices.find(item => item.target.gateway_profile_id === targetId)
  const locked = start.isPending || request.current !== null
  const valid = available && Boolean(selected) && gatewayCreateFormValid(value)
  const invalidEndpoint = Boolean(value.endpoint_url.trim()) && !isGatewayEndpoint(value.endpoint_url)
  const update = (field: "display_name" | "endpoint_url" | "location", next: string) => setValue(current => ({ ...current, [field]: next }))
  return <Dialog open onOpenChange={open => { if (!open && !start.isPending && !submitting.current) onClose() }}>
    <DialogContent className="registry-editor-dialog infrastructure-editor-dialog gateway-create-dialog" finalFocus={false}>
      <form className="registry-editor" data-request-id={requestId} onSubmit={async event => {
        event.preventDefault()
        if (!valid || start.isPending || submitting.current) return
        submitting.current = true
        request.current ??= { targetId, value: gatewayCreateFormPayload(value), requestId }
        try { await start.mutateAsync(request.current) } catch {}
        finally { submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>新建 Self-hosted Gateway</DialogTitle><DialogDescription>Docker · APIM 配置与 API 关联</DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={start.isPending} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body infrastructure-editor-body">
          <div className="registry-field"><span className="registry-field-label">APIM 实例</span><Select value={targetId} disabled={locked || initialTargetId !== null} onValueChange={next => setTargetId(next ?? "")}><SelectTrigger className="registry-select-trigger" aria-label="APIM 实例"><SelectValue>{selected?.target.service_name ?? "选择 APIM 实例"}</SelectValue></SelectTrigger><SelectContent>{choices.map(item => <SelectItem key={item.target.gateway_profile_id} value={item.target.gateway_profile_id}>{item.target.service_name}</SelectItem>)}</SelectContent></Select></div>
          <label className="registry-field" htmlFor={`${prefix}-name`}><span className="registry-field-label">Gateway 名称</span><Input id={`${prefix}-name`} name="display_name" autoFocus required maxLength={120} value={value.display_name} disabled={locked} onChange={event => update("display_name", event.target.value)} /></label>
          <label className="registry-field" htmlFor={`${prefix}-endpoint`}><span className="registry-field-label">Gateway endpoint</span><Input id={`${prefix}-endpoint`} name="endpoint_url" type="url" required value={value.endpoint_url} disabled={locked} aria-invalid={invalidEndpoint} aria-describedby={invalidEndpoint ? `${prefix}-endpoint-error` : undefined} placeholder={`https://gateway.example.com/${selected?.target.api_path ?? "api-path"}`} onChange={event => update("endpoint_url", event.target.value)} /></label>
          {invalidEndpoint && <span className="infrastructure-field-error" id={`${prefix}-endpoint-error`}>使用 HTTPS URL，不得包含凭据、查询参数或片段。</span>}
          <label className="registry-field" htmlFor={`${prefix}-location`}><span className="registry-field-label">位置（可选）</span><Input id={`${prefix}-location`} name="location" maxLength={128} value={value.location ?? ""} disabled={locked} onChange={event => update("location", event.target.value)} /></label>
          <label className="gateway-endpoint-confirmation"><Checkbox checked={value.endpoint_control_confirmed} disabled={locked} onCheckedChange={checked => setValue(current => ({ ...current, endpoint_control_confirmed: checked === true }))} /><span>我控制此服务器及 HTTPS 地址，允许 Turnstile 向此地址发送网关验证和模型调用凭据。</span></label>
          {(!available || !selected) && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>{!available ? "平台网关创建能力尚未启用" : "没有已同步且支持 Self-hosted Gateway 的 APIM 实例"}</span></div>}
          {start.error && <div className="registry-error" role="alert">{errorMessage(start.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer apim-onboarding-footer"><Button type="button" variant="ghost" disabled={start.isPending} onClick={() => onAdopt(initialTargetId)}><Link2 size={14} />关联已有 Gateway</Button><DialogClose render={<Button type="button" variant="outline" disabled={start.isPending} />}>取消</DialogClose><Button type="submit" disabled={!valid || start.isPending}>{start.isPending ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}{start.isPending ? "正在提交" : request.current ? "重试提交" : "创建 Gateway"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function SelfHostedGatewayProvisionProgress({ operation, available, retrying, onRetry }: {
  operation: GatewayReleaseOperation; available: boolean; retrying: boolean; onRetry: (id: string) => void
}) {
  const configuration = operation.semantic_preview.configuration as Partial<SelfHostedGatewayCreate> | undefined
  const nativeId = typeof operation.semantic_preview.native_gateway_id === "string" ? operation.semantic_preview.native_gateway_id : ""
  const failed = operation.status === "failed"
  const complete = operation.status === "succeeded"
  const steps = [
    { label: "创建 APIM 网关配置", done: complete || operation.checkpoint.apim_gateway_created === true },
    { label: "关联 API", done: complete || operation.checkpoint.api_associated === true },
    { label: "读回确认", done: complete },
  ]
  return <section className="gateway-provision-progress" aria-label={configuration?.display_name ?? "Gateway 创建任务"}>
    <div className="gateway-provision-heading"><Server size={16} /><strong data-no-localize>{configuration?.display_name ?? nativeId}</strong><span role="status">{failed ? "创建失败" : complete ? "APIM 配置已创建，等待容器接入" : operation.worker_available ? "正在创建 Gateway" : "创建已排队，Worker 当前不可用"}</span></div>
    <code data-no-localize>{nativeId}</code>
    <ol className="apim-onboarding-steps">{steps.map(step => <li key={step.label} data-complete={step.done}>{step.done ? <CheckCircle2 size={13} /> : <Circle size={13} />}<span>{step.label}</span></li>)}</ol>
    {(failed || operation.error_message) && <div className="registry-error apim-onboarding-error"><p role="alert" data-no-localize>{operation.error_message}</p>{failed && <Button type="button" size="sm" variant="outline" disabled={!available || retrying} onClick={() => onRetry(operation.id)}><RefreshCw size={14} className={retrying ? "spin" : ""} />重试创建</Button>}</div>}
  </section>
}

export function SelfHostedGatewayBootstrapDialog({ deploymentId, name, onClose, onValidate }: {
  deploymentId: string; name: string; onClose: () => void; onValidate: () => void
}) {
  const { locale } = useLocale()
  const { timezone } = useTimezone()
  const [hours, setHours] = useState(24)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [summary, setSummary] = useState<Omit<SelfHostedGatewayBootstrap, "environment_file"> | null>(null)
  const [copied, setCopied] = useState(false)
  const [containerAction, setContainerAction] = useState("start")
  const credential = useRef<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  const downloads = useRef(new Set<string>())
  const command = summary ? containerAction === "renew" ? summary.renewal_command : summary.docker_command : ""
  useEffect(() => () => {
    controller.current?.abort()
    credential.current = null
    for (const url of downloads.current) URL.revokeObjectURL(url)
    downloads.current.clear()
  }, [])
  const load = async () => {
    if (controller.current && !controller.current.signal.aborted) return
    const current = new AbortController()
    controller.current = current
    credential.current = null
    setSummary(null); setBusy(true); setError(null); setCopied(false)
    try {
      const result = await dataSource.selfHostedGatewayBootstrap(deploymentId, hours, current.signal)
      if (current.signal.aborted) return
      const { environment_file, ...metadata } = result
      credential.current = environment_file
      setSummary(metadata)
    } catch (failure) { if (!current.signal.aborted) setError(errorMessage(failure)) }
    finally { if (!current.signal.aborted) { setBusy(false); controller.current = null } }
  }
  const download = () => {
    if (!summary || !credential.current) return
    const url = URL.createObjectURL(new Blob([credential.current], { type: "text/plain;charset=utf-8" }))
    downloads.current.add(url)
    const link = document.createElement("a")
    link.href = url; link.download = summary.environment_file_name; link.click()
    URL.revokeObjectURL(url); downloads.current.delete(url)
  }
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="registry-editor-dialog infrastructure-editor-dialog gateway-bootstrap-dialog" finalFocus={false}>
    <div className="registry-editor"><DialogHeader className="registry-editor-header"><DialogTitle>Gateway 启动配置</DialogTitle><DialogDescription data-no-localize>{name}</DialogDescription></DialogHeader>
      <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
      <div className="registry-editor-body infrastructure-editor-body">
        <div className="registry-field"><span className="registry-field-label">Token 有效期</span><Select value={String(hours)} disabled={busy} onValueChange={value => setHours(Number(value ?? 24))}><SelectTrigger className="registry-select-trigger" aria-label="Token 有效期"><SelectValue>{hours} h</SelectValue></SelectTrigger><SelectContent>{[1, 24, 168, 720].map(value => <SelectItem key={value} value={String(value)}>{value} h</SelectItem>)}</SelectContent></Select></div>
        <Button type="button" variant="outline" disabled={busy} onClick={load}><RefreshCw size={14} className={busy ? "spin" : ""} />{busy ? "正在获取" : summary ? "重新生成启动配置" : "获取启动配置"}</Button>
        {summary && <>
          <dl className="gateway-bootstrap-metadata"><dt>APIM Gateway ID</dt><dd data-no-localize><code>{summary.native_gateway_id}</code></dd><dt>Configuration endpoint</dt><dd data-no-localize><code>{summary.configuration_endpoint}</code></dd><dt>官方镜像</dt><dd data-no-localize><code>{summary.image}</code></dd><dt>到期时间</dt><dd data-no-localize><time dateTime={summary.expires_at}>{new Date(summary.expires_at).toLocaleString(locale, { timeZone: timezone, timeZoneName: "short" })}</time></dd></dl>
          <Button type="button" variant="outline" onClick={download}><Download size={14} />下载环境文件</Button>
          <div className="infrastructure-warning"><CircleAlert size={15} /><span>环境文件含短期凭据。仅保存在该服务器，不要提交到 Git 或分享。</span></div>
          <div className="registry-field"><span className="registry-field-label">容器操作</span><Select value={containerAction} onValueChange={value => { setContainerAction(value ?? "start"); setCopied(false) }}><SelectTrigger className="registry-select-trigger" aria-label="容器操作"><SelectValue>{containerAction === "start" ? "首次启动" : "续期并重建此容器"}</SelectValue></SelectTrigger><SelectContent><SelectItem value="start">首次启动</SelectItem><SelectItem value="renew">续期并重建此容器</SelectItem></SelectContent></Select></div>
          {containerAction === "renew" && <div className="infrastructure-warning"><CircleAlert size={15} /><span>此命令会停止并重建当前网关容器，期间连接将中断。</span></div>}
          <label className="registry-field"><span className="registry-field-label">Docker 启动命令</span><textarea className="gateway-bootstrap-command" readOnly rows={6} value={command} data-no-localize /></label>
          <Button type="button" variant="ghost" onClick={() => { void navigator.clipboard.writeText(command).then(() => setCopied(true)).catch(() => setError("无法访问剪贴板")) }}>{copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}{copied ? "已复制" : "复制启动命令"}</Button>
          <div className="infrastructure-warning"><CircleAlert size={15} /><span>HTTPS 入口需要服务器上的有效证书或反向代理；Docker 端口映射不会自动配置 TLS。</span></div>
        </>}
        {error && <div className="registry-error" role="alert">{error}</div>}
      </div>
      <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" />}>关闭</DialogClose><Button type="button" disabled={busy} onClick={onValidate}><CheckCircle2 size={14} />验证连接</Button></DialogFooter>
    </div>
  </DialogContent></Dialog>
}