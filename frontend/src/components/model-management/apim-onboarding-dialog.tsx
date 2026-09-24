import { useId, useRef, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { ArrowLeft, CheckCircle2, ChevronRight, Circle, CircleAlert, Play, RefreshCw } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import type { ApimOnboardingPlan, ApimOnboardingRequest, ApimOnboardingStart, GatewayReleaseOperation } from "../../data-sources/apim/types"
import { GatewayBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"
import { ManagementCredentialDialog, ManagementCredentialField } from "./management-credential"

const steps: Record<string, string> = {
  validate_target: "Validate instance",
  create_api: "Create dedicated API",
  configure_governance: "Configure governance",
  create_subscriptions: "Create system subscriptions",
  project_access: "Prepare budget",
  verify_managed_gateway: "Verify Managed Gateway",
  activate_target: "Activate target",
}
const blockers: Record<string, string> = {
  apim_not_ready: "APIM is not ready",
  managed_gateway_tier_unsupported: "The APIM tier does not support the required policies",
  dedicated_api_conflict: "The dedicated API ID is already in use",
  dedicated_api_path_conflict: "The dedicated API path is already in use",
  system_subscription_conflict: "A system subscription ID is already in use",
  budget_execution_not_ready: "Budget execution is not ready",
  apim_already_registered: "This APIM is already registered",
}
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const resourcePattern = /^\/subscriptions\/([0-9a-f-]+)\/resourceGroups\/[^/?#%\\]+\/providers\/Microsoft\.ApiManagement\/service\/[^/?#%\\]+\/?$/i

export function apimOnboardingOperationName(operation: GatewayReleaseOperation): string {
  const plan = operation.semantic_preview
  const request = plan.request && typeof plan.request === "object" ? plan.request as Record<string, unknown> : {}
  return typeof request.name === "string" && request.name.trim() ? request.name
    : typeof plan.service_name === "string" ? plan.service_name : "APIM initialization"
}

export function ApimOnboardingDialog({ available, onClose, onAccepted, onSettled }: {
  available: boolean
  onClose: () => void
  onAccepted: (operation: GatewayReleaseOperation) => void | Promise<void>
  onSettled: () => void
}) {
  const prefix = useId()
  const submitting = useRef(false)
  const credentialForRequest = useRef("")
  const [managementCredential, setManagementCredential] = useState("")
  const [value, setValue] = useState<ApimOnboardingRequest>({ tenant_id: "", resource_id: "", name: "" })
  const [plan, setPlan] = useState<ApimOnboardingPlan | null>(null)
  const preview = useMutation({ mutationFn: (request: ApimOnboardingRequest) => dataSource.previewApimOnboarding(request, credentialForRequest.current), onSuccess: setPlan })
  const start = useMutation({
    mutationFn: (request: ApimOnboardingStart) => dataSource.startApimOnboarding(request, credentialForRequest.current),
    onSuccess: accepted => onAccepted(accepted.operation),
    onSettled,
  })
  const busy = preview.isPending || start.isPending
  const resource = resourcePattern.exec(value.resource_id.trim())
  const valid = uuidPattern.test(value.tenant_id.trim()) && Boolean(resource && uuidPattern.test(resource[1]))
  const update = (field: keyof ApimOnboardingRequest, next: string) => {
    setValue(current => ({ ...current, [field]: next }))
    setPlan(null)
    preview.reset()
    start.reset()
  }
  const error = preview.error ?? start.error
  return <Dialog open onOpenChange={open => { if (!open && !busy && !submitting.current) onClose() }}>
    <DialogContent className="registry-editor-dialog apim-onboarding-dialog">
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy || submitting.current || !available || !valid) return
        submitting.current = true
        credentialForRequest.current = managementCredential
        try {
          if (plan?.ready) {
            await start.mutateAsync({ request: plan.request, confirmation_sha256: plan.confirmation_sha256 })
            setManagementCredential("")
          } else {
            await preview.mutateAsync({
              tenant_id: value.tenant_id.trim(), resource_id: value.resource_id.trim(),
              ...(value.name?.trim() ? { name: value.name.trim() } : {}),
            })
          }
        } catch { setManagementCredential("") }
        finally { credentialForRequest.current = ""; submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>Initialize APIM</DialogTitle><DialogDescription>Managed Gateway initialization</DialogDescription></DialogHeader>
        <div className="registry-editor-body">
          {!plan && <div className="apim-onboarding-fields">
            <label className="registry-field" htmlFor={`${prefix}-name`}><span className="registry-field-label">Name (optional)</span><Input id={`${prefix}-name`} name="name" value={value.name ?? ""} maxLength={120} disabled={busy} autoFocus onChange={event => update("name", event.target.value)} /></label>
            <label className="registry-field" htmlFor={`${prefix}-tenant`}><span className="registry-field-label">Tenant ID</span><Input id={`${prefix}-tenant`} name="tenant_id" value={value.tenant_id} required maxLength={36} disabled={busy} onChange={event => update("tenant_id", event.target.value)} /></label>
            <label className="registry-field" htmlFor={`${prefix}-resource`}><span className="registry-field-label">APIM resource ID</span><Input id={`${prefix}-resource`} name="resource_id" value={value.resource_id} required maxLength={1024} disabled={busy} onChange={event => update("resource_id", event.target.value)} /></label>
          </div>}
          {plan && <section className="apim-onboarding-plan" aria-label="Initialization plan">
            <div className="apim-onboarding-target"><GatewayBrandLogo brand="apim" /><b>{plan.request.name ?? plan.service_name}</b><span>{plan.service_tier} / {plan.location}</span></div>
            <dl className="gateway-inventory-fields">
              <div><dt>APIM service</dt><dd>{plan.service_name}</dd></div>
              <div><dt>Dedicated API</dt><dd>{plan.api_id}</dd></div>
              <div className="apim-onboarding-endpoint"><dt>Gateway endpoint</dt><dd>{plan.managed_gateway_origin.replace(/\/$/, "")}/{plan.api_path}</dd></div>
              <div><dt>Dashboard subscription</dt><dd>{plan.dashboard_subscription_id}</dd></div>
              <div><dt>Probe subscription</dt><dd>{plan.probe_subscription_id}</dd></div>
              <div><dt>Monthly tokens</dt><dd>{plan.initial_monthly_token_limit.toLocaleString()}</dd></div>
              <div><dt>Tokens per minute</dt><dd>{plan.initial_tokens_per_minute.toLocaleString()}</dd></div>
            </dl>
            <ol className="apim-onboarding-steps">{plan.steps.map(step => <li key={step}><Circle size={13} /><span>{steps[step] ?? step}</span></li>)}</ol>
            {plan.blockers.length > 0 && <ul className="registry-error apim-onboarding-blockers" role="alert">{plan.blockers.map(blocker => <li key={blocker}>{blockers[blocker] ?? blocker}</li>)}</ul>}
          </section>}
          <ManagementCredentialField value={managementCredential} disabled={busy} onChange={setManagementCredential} />
          {!available && <div className="registry-error" role="status">Initialization is unavailable</div>}
        </div>
        {error && <div className="registry-error gateway-registration-error" role="alert">{error.message}</div>}
        <DialogFooter className="registry-editor-footer apim-onboarding-footer">
          {plan && <Button type="button" variant="ghost" disabled={busy} onClick={() => { if (!submitting.current) { setPlan(null); preview.reset(); start.reset() } }}><ArrowLeft size={14} />Back</Button>}
          <Button type="button" variant="outline" disabled={busy} onClick={() => { if (!submitting.current) onClose() }}>Cancel</Button>
          <Button type="submit" disabled={busy || !available || !valid}>{busy ? <RefreshCw size={14} className="spin" /> : plan?.ready ? <Play size={14} /> : <CheckCircle2 size={14} />}{start.isPending ? "Submitting" : preview.isPending ? "Checking" : plan?.ready ? "Start initialization" : plan ? "Check again" : "Check resource"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function ApimOnboardingRow({ operation, available, retryingId, retryError = null, onRetry }: {
  operation: GatewayReleaseOperation
  available: boolean
  retryingId: string | null
  retryError?: string | null
  onRetry: (id: string, credential: string) => Promise<unknown>
}) {
  const [expanded, setExpanded] = useState(true)
  const [confirmRetry, setConfirmRetry] = useState(false)
  const detailsId = useId()
  const failed = operation.status === "failed"
  const succeeded = operation.status === "succeeded"
  const completed = new Set(succeeded ? Object.keys(steps) : Array.isArray(operation.checkpoint.completed_steps)
    ? operation.checkpoint.completed_steps.filter((item): item is string => typeof item === "string") : [])
  const activeStep = Object.keys(steps).find(step => !completed.has(step))
  const plan = operation.semantic_preview
  const status = failed ? "Initialization failed" : succeeded ? "Initialization complete"
    : operation.worker_available ? "Initializing" : "Queued: Worker unavailable"
  return <section className="gateway-target-group apim-onboarding-row" role="rowgroup" aria-label={apimOnboardingOperationName(operation)} data-status={operation.status}>
    <article className="model-list-row gateway-target-heading" role="row">
      <button type="button" className="gateway-target-expand model-primary-cell" aria-label="Initialization progress" aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpanded(!expanded)}>
        <ChevronRight size={14} className={expanded ? "gateway-expanded" : ""} /><span className="model-list-icon"><GatewayBrandLogo brand="apim" /></span><span><b>{apimOnboardingOperationName(operation)}</b><small>{typeof plan.service_name === "string" ? plan.service_name : "APIM"}</small></span>
      </button>
      <div className="model-runtime-cell"><b>Managed Gateway</b><span>{typeof plan.api_id === "string" ? plan.api_id : ""}</span></div>
      <div className="model-pricing-cell"><b>{completed.size} / {Object.keys(steps).length}</b><span>{activeStep ? steps[activeStep] : "Complete"}</span></div>
      <div className="model-status-cell"><span className={`registry-health ${failed ? "unavailable" : succeeded ? "available" : "unchecked"}`} role="status"><i />{status}</span></div>
      <div className="model-row-actions">{failed && <button type="button" title="Retry initialization" aria-label="Retry initialization" disabled={!available || retryingId !== null} onClick={() => setConfirmRetry(true)}><RefreshCw size={14} className={retryingId === operation.id ? "spin" : ""} /></button>}</div>
    </article>
    <div id={detailsId} hidden={!expanded} className="apim-onboarding-progress">
      <ol className="apim-onboarding-steps">{Object.entries(steps).map(([step, label]) => <li key={step} data-complete={completed.has(step)} data-failed={failed && step === activeStep} aria-current={step === activeStep ? "step" : undefined}>{completed.has(step) ? <CheckCircle2 size={13} /> : failed && step === activeStep ? <CircleAlert size={13} /> : <Circle size={13} />}<span>{label}</span></li>)}</ol>
      {typeof operation.checkpoint.retry_of === "string" && <p className="apim-onboarding-retry-link">Retry of {operation.checkpoint.retry_of}</p>}
    </div>
    {(failed || operation.error_message) && <div className="registry-error gateway-operation-state" role="alert">{operation.error_message ?? "Initialization failed"}</div>}
    {confirmRetry && <ManagementCredentialDialog title="Retry initialization" targetName={apimOnboardingOperationName(operation)} submitLabel="Retry" busy={retryingId !== null} available={available && failed} error={retryError} onClose={() => setConfirmRetry(false)} onConfirm={async credential => {
      await onRetry(operation.id, credential)
      setConfirmRetry(false)
    }} />}
  </section>
}