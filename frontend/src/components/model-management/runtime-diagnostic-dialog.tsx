import { useId, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { RefreshCw, TriangleAlert } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import type { ModelRuntime, RuntimeCheckSelection, RuntimeHealth } from "../../data-sources/apim/types"
import { runtimeDiagnosticReason, runtimeHealthDescription, runtimeHealthLabels, runtimeHealthState } from "../../lib/runtime-health"
import { useAuth } from "../../providers/auth-provider"
import { Button } from "../ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"

export function RuntimeDiagnosticDialog({ runtime, supported, busy, error, result, onCheck, onClose }: {
  runtime: ModelRuntime
  supported: boolean
  busy: boolean
  error: string | null
  result: RuntimeHealth | null
  onCheck: (selection: RuntimeCheckSelection) => void
  onClose: () => void
}) {
  const { user } = useAuth()
  const owner = user?.role === "owner"
  const [selected, setSelected] = useState("")
  const [submitted, setSubmitted] = useState(false)
  const sending = useRef(false)
  const targetLabelId = useId()
  const plan = useQuery({
    queryKey: ["control-plane", "runtime-check-plan", runtime.id],
    queryFn: () => dataSource.runtimeCheckPlan(runtime.id),
    enabled: supported && owner,
    retry: false,
    staleTime: 0,
  })
  const options = plan.data?.options ?? []
  const option = options.find(item => `${item.model_id}:${item.gateway_deployment_id}` === selected)
  const access = owner && supported ? plan.data?.access : null
  const accessIssue = access?.status === "ready" ? null
    : access?.reason === "permission_missing" ? "部署权限未就绪"
    : access?.reason === "identity_unavailable" ? "未能核验后端托管身份"
    : access?.reason === "tenant_mismatch" ? "后端身份与 APIM 租户不一致"
    : access?.reason === "probe_subscription_unconfigured" ? "尚未配置系统诊断订阅"
    : "诊断权限尚未核验"
  const unavailable = !owner ? "仅 Owner 可执行连接诊断"
    : !supported ? "当前后端尚未支持受限连接诊断"
    : plan.error ? "诊断计划加载失败"
    : runtimeDiagnosticReason(plan.data?.unavailable_reason) ?? (plan.data ? accessIssue : null)
  const canRun = Boolean(option) && access?.status === "ready" && !busy && !submitted
    && owner && supported && !unavailable && !plan.isFetching
  const resultState = result ? runtimeHealthState({ ...runtime, diagnostic: result }) : "unchecked"
  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog registry-editor runtime-diagnostic-dialog">
      <DialogHeader className="registry-editor-header">
        <DialogTitle>连接诊断</DialogTitle>
        <DialogDescription data-no-localize>{runtime.name}</DialogDescription>
      </DialogHeader>
      <div className="registry-editor-body runtime-diagnostic-body">
        <dl className="runtime-diagnostic-facts">
          <div><dt>执行身份</dt><dd>系统诊断订阅</dd></div>
          <div><dt>请求上限</dt><dd>1 次 · 最多 64 输出 Tokens</dd></div>
          <div><dt>费用</dt><dd>实际模型用量计入系统探测，不占用个人预算</dd></div>
        </dl>
        {plan.isLoading && supported && owner && <div className="runtime-diagnostic-loading" role="status"><RefreshCw size={14} className="spin" aria-hidden="true" /><span>正在加载诊断计划</span></div>}
        {unavailable && <div className="smh-notice infrastructure-error-notice" role="alert"><TriangleAlert size={14} /><div className="smh-notice-copy">{unavailable}</div></div>}
        {owner && supported && !result && <div className="runtime-diagnostic-access-header">
          <span>{access?.status === "ready" ? "诊断权限已核验" : "诊断权限尚未核验"}</span>
          <Button type="button" variant="ghost" size="icon-sm" title="重新核验权限" aria-label="重新核验权限" disabled={busy || submitted || plan.isFetching} onClick={() => {
            if (!busy && !submitted && !plan.isFetching) void plan.refetch()
          }}><RefreshCw size={14} className={plan.isFetching ? "spin" : undefined} /></Button>
        </div>}
        {access && <details className="runtime-diagnostic-access">
          <summary>部署授权详情</summary>
          <dl className="runtime-diagnostic-facts">
            <div><dt>API 托管身份</dt><dd data-no-localize>{access.api_principal_id ?? "-"}</dd></div>
            <div><dt>租户 ID</dt><dd data-no-localize>{access.tenant_id}</dd></div>
            <div><dt>订阅 ID</dt><dd data-no-localize>{access.subscription_id}</dd></div>
            <div><dt>资源组</dt><dd data-no-localize>{access.resource_group}</dd></div>
            <div><dt>授权作用域</dt><dd data-no-localize>{access.scope}</dd></div>
            <div><dt>{access.missing_actions.length ? "缺少权限" : "所需权限"}</dt><dd>{(access.missing_actions.length ? access.missing_actions : access.required_actions).map(action => <code key={action} data-no-localize>{action}</code>)}</dd></div>
          </dl>
        </details>}
        <div className="runtime-diagnostic-field">
          <label id={targetLabelId}>被测模型 / 网关</label>
          <Select value={selected} onValueChange={value => setSelected(value ?? "")} disabled={busy || Boolean(result) || !owner || !supported || !options.length}>
            <SelectTrigger className="registry-select-trigger" aria-labelledby={targetLabelId}><SelectValue placeholder="选择被测模型和网关">{option ? <span data-no-localize>{option.model_name} · {option.gateway_name}</span> : "选择被测模型和网关"}</SelectValue></SelectTrigger>
            <SelectContent>{options.map(item => <SelectItem key={`${item.model_id}:${item.gateway_deployment_id}`} value={`${item.model_id}:${item.gateway_deployment_id}`}>
              <span className="runtime-diagnostic-option"><b data-no-localize>{item.model_name}</b><small data-no-localize>{item.model_key} · {item.gateway_name} ({item.native_gateway_id})</small></span>
            </SelectItem>)}</SelectContent>
          </Select>
        </div>
        {option && <dl className="runtime-diagnostic-facts"><div><dt>模型标识</dt><dd data-no-localize>{option.model_key}</dd></div><div><dt>网关</dt><dd data-no-localize>{option.gateway_name} · {option.native_gateway_id}</dd></div></dl>}
        {error && <div className="smh-notice infrastructure-error-notice" role="alert"><TriangleAlert size={14} /><div className="smh-notice-copy">{error}</div></div>}
        {result && <div className="runtime-diagnostic-result" data-state={resultState} role="status">
          <strong>{runtimeHealthLabels[resultState]}</strong>
          <p>{runtimeHealthDescription({ ...runtime, diagnostic: result })}</p>
          {result.request_id && <code data-no-localize>{result.request_id}</code>}
        </div>}
      </div>
      <DialogFooter className="registry-editor-footer">
        <Button variant="outline" disabled={busy} onClick={onClose}>{result ? "关闭" : "取消"}</Button>
        {!result && <Button disabled={!canRun} onClick={() => {
          if (!canRun || !option || sending.current) return
          sending.current = true
          setSubmitted(true)
          onCheck({ model_id: option.model_id, gateway_deployment_id: option.gateway_deployment_id })
        }}><RefreshCw size={14} className={busy ? "spin" : undefined} />{busy ? "正在检查" : "运行检查"}</Button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>
}