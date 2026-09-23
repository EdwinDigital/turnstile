import { useEffect, useId, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronRight, Cloud, Eye, Plus, RefreshCw, Save, Server, TextCursorInput, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import { finopsKeys, finopsQueries, storeApimTarget } from "../../data-sources/apim/queries"
import type { ApimTargetTopology, ApimTargetWrite, GatewayProfile } from "../../data-sources/apim/types"
import { apimGatewayEntries, apimTargetDisplayName, gatewayEvidence, type GatewayEntry } from "../../lib/gateway-topology"
import { GatewayBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"

const terminalStatuses = new Set(["succeeded", "failed", "restored"])

export function GatewayTargetGroup({ target, profile, canManage, syncAvailable, onRename }: {
  target: ApimTargetTopology
  profile: GatewayProfile
  canManage: boolean
  syncAvailable: boolean
  onRename?: () => void
}) {
  const [collapsed, setCollapsed] = useState(false)
  const [details, setDetails] = useState<GatewayEntry | null>(null)
  const [operationId, setOperationId] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const operation = useQuery(finopsQueries.gatewayReleaseOperation(operationId))
  const sync = useMutation({
    mutationFn: () => dataSource.syncApimTarget(profile.id),
    onSuccess: result => {
      queryClient.setQueryData(finopsKeys.gatewayReleaseOperation(result.operation.id), result.operation)
      setOperationId(result.operation.id)
    },
  })
  useEffect(() => {
    if (operation.data?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets })
    }
  }, [operation.data?.status, queryClient])
  const running = sync.isPending || Boolean(operationId && (!operation.data || !terminalStatuses.has(operation.data.status)))
  const failure = sync.error?.message ?? operation.error?.message
    ?? (operation.data?.status === "failed" ? operation.data.error_message ?? "Inventory synchronization failed" : null)
  const name = apimTargetDisplayName(target, [profile])
  return <section className="gateway-target-group" role="rowgroup" aria-label={name}>
    <div className="model-list-row gateway-target-heading" role="row">
      <button type="button" className="gateway-target-expand model-primary-cell" aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)}>
        <ChevronRight size={14} className={collapsed ? "" : "gateway-expanded"} />
        <span className="model-list-icon"><GatewayBrandLogo brand="apim" /></span>
        <span><b>{name}</b><small>{target.target.service_name}</small></span>
      </button>
      <div className="model-runtime-cell"><b>APIM</b><span>{target.target.service_tier} / {target.target.service_location}</span></div>
      <div className="model-pricing-cell"><b>{target.published_model_count} models</b><span>{target.self_hosted_gateway_count} self-hosted Gateways</span></div>
      <div className="model-status-cell"><span className={`registry-health ${target.connection_state === "connected" ? "available" : target.connection_state === "unavailable" ? "unavailable" : "unchecked"}`} title={target.target.last_inventory_sync_at ?? "No inventory evidence"}><i />{target.connection_state === "connected" ? "Synchronized" : target.connection_state === "unavailable" ? "Drifted" : "Unknown"}</span></div>
      <div className="model-row-actions">
        {onRename && <button type="button" title="Name" aria-label="Name" onClick={onRename} disabled={running}><TextCursorInput size={14} /></button>}
        {canManage && <button type="button" title={running ? "Synchronizing" : "Synchronize inventory"} aria-label="Synchronize inventory" disabled={running || !syncAvailable} onClick={() => sync.mutate()}><RefreshCw size={14} className={running ? "spin" : ""} /></button>}
      </div>
    </div>
    {operationId && <div className="gateway-operation-state" role="status">
      <span>{operation.data?.status.replaceAll("_", " ") ?? "Queued"}</span>
      {operation.error && <Button variant="ghost" size="icon-sm" title="Refresh operation" aria-label="Refresh operation" onClick={() => void operation.refetch()}><RefreshCw size={14} /></Button>}
    </div>}
    {failure && <div className="registry-error gateway-operation-state" role="alert">{failure}</div>}
    {!collapsed && apimGatewayEntries(target).map(entry => {
      const evidence = gatewayEvidence(entry)
      return <article className="model-list-row gateway-deployment-row" role="row" key={entry.deployment.id}>
        <div className="model-primary-cell"><span className="connection-branch" aria-hidden="true" /><span className="model-list-icon">{entry.deployment.kind === "managed" ? <Cloud size={16} /> : <Server size={16} />}</span><div><b>{entry.deployment.display_name}</b><span>{entry.deployment.native_gateway_id}</span></div></div>
        <div className="model-runtime-cell"><b>{entry.deployment.kind === "managed" ? "Managed" : "Self-hosted"}</b><span>{entry.deployment.hosting_mode}</span></div>
        <div className="model-pricing-cell"><b>{entry.published_models.length} models</b><span title={entry.deployment.endpoint_url ?? undefined}>{entry.deployment.endpoint_url ?? "Endpoint not configured"}</span></div>
        <div className="model-status-cell"><span className={`registry-health ${evidence.state === "ready" ? "available" : evidence.state === "unavailable" ? "unavailable" : "unchecked"}`} title={`${evidence.detail}${evidence.probedAt ? ` (${evidence.probedAt})` : ""}`}><i />{evidence.label}</span></div>
        <div className="model-row-actions"><button type="button" title="Gateway details" aria-label="Gateway details" onClick={() => setDetails(entry)}><Eye size={14} /></button></div>
      </article>
    })}
    {details && <GatewayInventoryDialog entry={details} onClose={() => setDetails(null)} />}
  </section>
}

export function GatewayInventoryDialog({ entry, onClose }: { entry: GatewayEntry; onClose: () => void }) {
  const evidence = gatewayEvidence(entry)
  const fields = [
    ["Native ID", entry.deployment.native_gateway_id], ["Endpoint", entry.deployment.endpoint_url],
    ["Configuration endpoint", entry.configuration_endpoint], ["Declaration", entry.deployment.declared_state],
    ["Reachability", entry.deployment.reachability_state], ["Observation", entry.deployment.observation_state],
    ["Last ARM sync", entry.deployment.last_arm_sync_at], ["Last probe", entry.deployment.last_probe_at],
    ["Last observation", entry.deployment.last_observed_at], ["Release gate", entry.deployment.release_gate],
    ["Runtime version support", entry.deployment.runtime_version_support],
    ["Rate-limit synchronization", entry.deployment.rate_limit_synchronization],
  ]
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="registry-editor-dialog gateway-inventory-dialog">
      <DialogHeader className="registry-editor-header"><DialogTitle>{entry.deployment.display_name}</DialogTitle><DialogDescription>{evidence.label}</DialogDescription></DialogHeader>
      <div className="registry-editor-body">
        <dl className="gateway-inventory-fields">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "Unknown"}</dd></div>)}</dl>
        <h3>Associated APIs</h3><ul>{entry.inventory?.associated_api_ids.length ? entry.inventory.associated_api_ids.map(identity => <li key={identity}>{identity}</li>) : <li>None recorded</li>}</ul>
        <h3>Hostnames</h3><ul>{entry.inventory?.hostname_configurations.length ? entry.inventory.hostname_configurations.map(item => <li key={item.configuration_id}>{item.hostname}<small>{item.certificate_id ?? "No certificate"}</small></li>) : <li>None recorded</li>}</ul>
        <h3>Certificate authorities</h3><ul>{entry.inventory?.certificate_authorities.length ? entry.inventory.certificate_authorities.map(item => <li key={item.authority_id}>{item.name}: {item.is_trusted ? "Trusted" : "Not trusted"}</li>) : <li>None recorded</li>}</ul>
        <h3>Published models</h3><ul>{entry.published_models.length ? entry.published_models.map(model => <li key={model.id}>{model.display_name}<small>{model.model_key}</small></li>) : <li>None recorded</li>}</ul>
      </div>
      <DialogFooter className="registry-editor-footer"><Button variant="outline" onClick={onClose}><X size={14} />Close</Button></DialogFooter>
    </DialogContent>
  </Dialog>
}

export function GatewayRegistrationAction() {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()
  const save = useMutation({
    mutationFn: dataSource.registerApimTarget,
    onSuccess: async target => { await storeApimTarget(queryClient, target); setOpen(false) },
  })
  return <>
    <Button type="button" onClick={() => { save.reset(); setOpen(true) }}><Plus size={14} />Register APIM</Button>
    {open && <GatewayRegistrationDialog busy={save.isPending} error={save.error?.message ?? null} onClose={() => setOpen(false)} onSave={value => save.mutateAsync(value)} />}
  </>
}

export function GatewayRegistrationDialog({ busy, error, onClose, onSave }: {
  busy: boolean; error: string | null; onClose: () => void
  onSave: (value: ApimTargetWrite) => Promise<unknown>
}) {
  const prefix = useId()
  const submitting = useRef(false)
  const [draft, setDraft] = useState({
    name: "", management_endpoint: "", tenant_id: "", subscription_id: "", resource_group: "",
    service_name: "", api_id: "", gateway_endpoint: "",
    dashboard_subscription_id: "turnstile-dashboard", probe_subscription_id: "turnstile-publisher-probe",
  })
  const labels: Record<keyof typeof draft, string> = {
    name: "Name", management_endpoint: "Management endpoint", tenant_id: "Tenant ID",
    subscription_id: "Subscription ID", resource_group: "Resource group", service_name: "APIM service",
    api_id: "API ID", gateway_endpoint: "Gateway endpoint", dashboard_subscription_id: "Dashboard subscription ID",
    probe_subscription_id: "Probe subscription ID",
  }
  const valid = Object.values(draft).every(value => value.trim().length > 0)
  return <Dialog open onOpenChange={open => { if (!open && !busy && !submitting.current) onClose() }}>
    <DialogContent className="registry-editor-dialog gateway-registration-dialog">
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy || submitting.current || !valid) return
        submitting.current = true
        try { await onSave(Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, value.trim()])) as typeof draft) }
        catch {}
        finally { submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>Register APIM</DialogTitle><DialogDescription>Existing APIM instance</DialogDescription></DialogHeader>
        <div className="registry-editor-body"><div className="gateway-registration-fields">
          {(Object.keys(draft) as Array<keyof typeof draft>).map(key => <label className="registry-field" key={key} htmlFor={`${prefix}-${key}`}>
            <span className="registry-field-label">{labels[key]}</span><Input id={`${prefix}-${key}`} name={key} value={draft[key]} required disabled={busy} type={key.endsWith("endpoint") ? "url" : "text"} maxLength={key.endsWith("endpoint") ? 2048 : key === "name" ? 120 : 256} onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} />
          </label>)}
        </div></div>
        {error && <div className="registry-error gateway-registration-error" role="alert">{error}</div>}
        <DialogFooter className="registry-editor-footer"><Button variant="outline" type="button" disabled={busy} onClick={() => { if (!submitting.current) onClose() }}>Cancel</Button><Button type="submit" disabled={busy || !valid}><Save size={14} />{busy ? "Registering" : "Register"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}