import { useEffect, useId, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowRightLeft, ChevronRight, Cloud, Eye, MoreHorizontal, Plus, RefreshCw, Save, Server, TextCursorInput, Trash2, Wifi, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import { finopsKeys, finopsQueries, storeApimTarget, storeGatewayDeployment, storeGatewayDeploymentName } from "../../data-sources/apim/queries"
import type { ApimTargetTopology, ApimTargetWrite, GatewayProfile, SelfHostedGatewayRemovalPreview, SelfHostedGatewayWrite } from "../../data-sources/apim/types"
import { apimGatewayEntries, apimTargetDisplayName, gatewayEvidence, type GatewayEntry } from "../../lib/gateway-topology"
import { GatewayBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogTitle } from "../ui/alert-dialog"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../ui/dropdown-menu"
import { Input } from "../ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"

const terminalStatuses = new Set(["succeeded", "failed", "restored"])

function gatewayEndpointValid(value: string) {
  try {
    const url = new URL(value.trim())
    return url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash
  } catch { return false }
}

export function SelfHostedGatewayEditor({ targets, initialTargetId, entry, preview, loading, available, busy, error, onClose, onSave }: {
  targets: ApimTargetTopology[]; initialTargetId: string; entry?: GatewayEntry
  preview?: SelfHostedGatewayRemovalPreview | null; loading?: boolean
  available: boolean; busy: boolean; error: string | null; onClose: () => void
  onSave: (targetId: string, value: SelfHostedGatewayWrite) => Promise<unknown>
}) {
  const prefix = useId()
  const submitting = useRef(false)
  const [targetId, setTargetId] = useState(initialTargetId)
  const [draft, setDraft] = useState<SelfHostedGatewayWrite>(() => ({
    native_gateway_id: entry?.deployment.native_gateway_id ?? "",
    display_name: entry?.deployment.display_name ?? "",
    endpoint_url: entry?.deployment.endpoint_url ?? "",
    location: entry?.deployment.location ?? "",
    hosting_mode: entry?.deployment.hosting_mode === "azure_managed" ? "other" : entry?.deployment.hosting_mode ?? "other",
    release_gate: entry?.deployment.release_gate ?? "required",
  }))
  const choices = targets.filter(target => target.target.capabilities.self_hosted_gateway !== "unsupported")
  const selected = choices.find(target => target.target.gateway_profile_id === targetId)
  const invalidEndpoint = Boolean(draft.endpoint_url.trim()) && !gatewayEndpointValid(draft.endpoint_url)
  const permitted = !entry || (!loading && preview?.deployment_id === entry.deployment.id
    && preview.removal_allowed && entry.published_models.length === 0 && targetId !== entry.deployment.apim_target_id)
  const valid = available && Boolean(selected) && permitted && Boolean(draft.display_name.trim())
    && Boolean(draft.native_gateway_id.trim()) && draft.native_gateway_id.trim().toLowerCase() !== "managed"
    && gatewayEndpointValid(draft.endpoint_url)
  const update = (name: keyof SelfHostedGatewayWrite, value: string) => setDraft(current => ({ ...current, [name]: value }))
  return <Dialog open onOpenChange={open => { if (!open && !busy && !submitting.current) onClose() }}>
    <DialogContent className="registry-editor-dialog gateway-lifecycle-dialog">
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy || submitting.current || !valid) return
        submitting.current = true
        try { await onSave(targetId, { ...draft, display_name: draft.display_name.trim(), native_gateway_id: draft.native_gateway_id.trim(), endpoint_url: draft.endpoint_url.trim(), location: draft.location?.trim() || null }) }
        catch {}
        finally { submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>{entry ? "Change APIM association" : "Add Self-hosted Gateway"}</DialogTitle><DialogDescription>Platform metadata only. Azure resources and containers remain unchanged.</DialogDescription></DialogHeader>
        <div className="registry-editor-body"><div className="gateway-registration-fields">
          <div className="registry-field gateway-field-wide"><span className="registry-field-label">APIM target</span><Select value={targetId} disabled={busy || !entry} onValueChange={next => setTargetId(next ?? "")}><SelectTrigger className="registry-select-trigger" aria-label="APIM target"><SelectValue>{selected?.target.service_name ?? "Select APIM"}</SelectValue></SelectTrigger><SelectContent>{choices.map(target => <SelectItem key={target.target.gateway_profile_id} value={target.target.gateway_profile_id}>{target.target.service_name}</SelectItem>)}</SelectContent></Select></div>
          <label className="registry-field gateway-field-wide" htmlFor={`${prefix}-configuration`}><span className="registry-field-label">Configuration endpoint</span><Input id={`${prefix}-configuration`} value={selected?.configuration_endpoint ?? ""} readOnly /></label>
          <label className="registry-field" htmlFor={`${prefix}-name`}><span className="registry-field-label">Gateway name</span><Input id={`${prefix}-name`} name="display_name" value={draft.display_name} required readOnly={Boolean(entry)} maxLength={120} disabled={busy} onChange={event => update("display_name", event.target.value)} /></label>
          <label className="registry-field" htmlFor={`${prefix}-native`}><span className="registry-field-label">APIM Gateway ID</span><Input id={`${prefix}-native`} name="native_gateway_id" value={draft.native_gateway_id} required readOnly={Boolean(entry)} maxLength={80} disabled={busy} onChange={event => update("native_gateway_id", event.target.value)} /></label>
          <label className="registry-field gateway-field-wide" htmlFor={`${prefix}-endpoint`}><span className="registry-field-label">Gateway endpoint</span><Input id={`${prefix}-endpoint`} name="endpoint_url" value={draft.endpoint_url} required type="url" aria-invalid={invalidEndpoint} maxLength={2083} disabled={busy} onChange={event => update("endpoint_url", event.target.value)} />{invalidEndpoint && <span className="gateway-field-error" role="alert">HTTPS endpoint required; credentials, query and fragment are not allowed.</span>}</label>
          <label className="registry-field" htmlFor={`${prefix}-location`}><span className="registry-field-label">Location (optional)</span><Input id={`${prefix}-location`} name="location" value={draft.location ?? ""} readOnly={Boolean(entry)} maxLength={128} disabled={busy} onChange={event => update("location", event.target.value)} /></label>
          <div className="registry-field"><span className="registry-field-label">Hosting mode</span><Select value={draft.hosting_mode} disabled={busy || Boolean(entry)} onValueChange={next => update("hosting_mode", next ?? "other")}><SelectTrigger className="registry-select-trigger" aria-label="Hosting mode"><SelectValue>{draft.hosting_mode?.replaceAll("_", " ")}</SelectValue></SelectTrigger><SelectContent>{["aks", "kubernetes", "arc", "container_apps", "docker", "other"].map(mode => <SelectItem value={mode} key={mode}>{mode.replaceAll("_", " ")}</SelectItem>)}</SelectContent></Select></div>
          <div className="registry-field"><span className="registry-field-label">Release gate</span><Select value={draft.release_gate} disabled={busy || Boolean(entry)} onValueChange={next => update("release_gate", next ?? "required")}><SelectTrigger className="registry-select-trigger" aria-label="Release gate"><SelectValue>{draft.release_gate === "optional" ? "Optional" : "Required"}</SelectValue></SelectTrigger><SelectContent><SelectItem value="required">Required</SelectItem><SelectItem value="optional">Optional</SelectItem></SelectContent></Select></div>
        </div>
          {loading && <div role="status" className="gateway-operation-state">Checking dependencies</div>}
          {entry && preview?.blockers.length ? <ul className="registry-error gateway-lifecycle-blockers" role="alert">{preview.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul> : null}
          {entry?.published_models.length ? <ul className="gateway-lifecycle-blockers">{entry.published_models.map(model => <li key={model.id}>{model.display_name}</li>)}</ul> : null}
          {entry && choices.filter(target => target.target.gateway_profile_id !== entry.deployment.apim_target_id).length === 0 && <p role="status">No other supported APIM target</p>}
          {!available && <p role="status">Gateway lifecycle is unavailable</p>}
        </div>
        {error && <div className="registry-error gateway-registration-error" role="alert">{error}</div>}
        <DialogFooter className="registry-editor-footer gateway-lifecycle-footer"><Button type="button" variant="outline" disabled={busy} onClick={() => { if (!submitting.current) onClose() }}>Cancel</Button><Button type="submit" disabled={busy || !valid}>{entry ? <ArrowRightLeft size={14} /> : <Plus size={14} />}{busy ? "Saving" : "Save association"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function GatewayDeploymentNameDialog({ entry, available, busy, error, onClose, onSave }: {
  entry: GatewayEntry; available: boolean; busy: boolean; error: string | null
  onClose: () => void; onSave: (name: string) => Promise<unknown>
}) {
  const prefix = useId()
  const submitting = useRef(false)
  const [name, setName] = useState(entry.deployment.display_name)
  const valid = available && name.trim().length > 0 && name.trim() !== entry.deployment.display_name
  return <Dialog open onOpenChange={open => { if (!open && !busy && !submitting.current) onClose() }}>
    <DialogContent className="registry-editor-dialog gateway-lifecycle-dialog">
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy || submitting.current || !valid) return
        submitting.current = true
        try { await onSave(name.trim()) } catch {} finally { submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>Gateway name</DialogTitle><DialogDescription>{entry.deployment.native_gateway_id}</DialogDescription></DialogHeader>
        <div className="registry-editor-body"><label className="registry-field" htmlFor={`${prefix}-name`}><span className="registry-field-label">Name</span><Input id={`${prefix}-name`} value={name} required maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></label>{!available && <p role="status">Gateway is no longer available</p>}</div>
        {error && <div className="registry-error gateway-registration-error" role="alert">{error}</div>}
        <DialogFooter className="registry-editor-footer gateway-lifecycle-footer"><Button type="button" variant="outline" disabled={busy} onClick={() => { if (!submitting.current) onClose() }}>Cancel</Button><Button type="submit" disabled={busy || !valid}><Save size={14} />{busy ? "Saving" : "Save"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function GatewayRetirementDialog({ entry, preview, loading, busy, error, available, onClose, onConfirm }: {
  entry: GatewayEntry; preview: SelfHostedGatewayRemovalPreview | null; loading: boolean
  busy: boolean; error: string | null; available: boolean
  onClose: () => void; onConfirm: () => Promise<unknown>
}) {
  const submitting = useRef(false)
  const allowed = available && !loading && preview?.deployment_id === entry.deployment.id && preview.removal_allowed
  return <AlertDialog open onOpenChange={open => { if (!open && !busy && !submitting.current) onClose() }}>
    <AlertDialogContent className="model-delete-dialog gateway-retirement-dialog">
      <div className="model-delete-body"><AlertDialogTitle>Retire Self-hosted Gateway</AlertDialogTitle><AlertDialogDescription>Azure resources and containers remain unchanged.</AlertDialogDescription><b>{entry.deployment.display_name}</b>
        {loading && <p role="status">Checking dependencies</p>}
        {preview?.models.length ? <ul className="gateway-lifecycle-blockers">{preview.models.map(model => <li key={model.id}>{model.display_name}</li>)}</ul> : null}
        {preview?.blockers.length ? <ul className="registry-error gateway-lifecycle-blockers" role="alert">{preview.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul> : null}
        {error && <div className="registry-error" role="alert">{error}</div>}
      </div>
      <AlertDialogFooter className="model-delete-footer"><AlertDialogCancel disabled={busy} onClick={event => { if (submitting.current) event.preventDefault() }}>Cancel</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={busy || !allowed} onClick={async () => {
        if (!allowed || busy || submitting.current) return
        submitting.current = true
        try { await onConfirm() } catch {} finally { submitting.current = false }
      }}><Trash2 size={14} />{busy ? "Retiring" : "Retire"}</AlertDialogAction></AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
}

export function GatewayTargetGroup({ target, profile, canManage, syncAvailable, lifecycleAvailable = false, targets = [], onRename }: {
  target: ApimTargetTopology
  profile: GatewayProfile
  canManage: boolean
  syncAvailable: boolean
  lifecycleAvailable?: boolean
  targets?: ApimTargetTopology[]
  onRename?: () => void
}) {
  const [collapsed, setCollapsed] = useState(false)
  const [details, setDetails] = useState<GatewayEntry | null>(null)
  const [operationId, setOperationId] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ entry?: GatewayEntry } | null>(null)
  const [renaming, setRenaming] = useState<GatewayEntry | null>(null)
  const [retiring, setRetiring] = useState<GatewayEntry | null>(null)
  const queryClient = useQueryClient()
  const operation = useQuery(finopsQueries.gatewayReleaseOperation(operationId))
  const preview = useQuery(finopsQueries.selfHostedGatewayRemoval(
    retiring?.deployment.id ?? editing?.entry?.deployment.id ?? null,
  ))
  const sync = useMutation({
    mutationFn: () => dataSource.syncApimTarget(profile.id),
    onSuccess: result => {
      queryClient.setQueryData(finopsKeys.gatewayReleaseOperation(result.operation.id), result.operation)
      setOperationId(result.operation.id)
    },
  })
  const saveGateway = useMutation({
    mutationFn: ({ targetId, value, previousId }: { targetId: string; value: SelfHostedGatewayWrite; previousId?: string }) => previousId
      ? dataSource.changeSelfHostedGatewayAssociation(previousId, { ...value, apim_target_id: targetId })
      : dataSource.addSelfHostedGateway(targetId, value),
    onSuccess: async (result, variables) => {
      await storeGatewayDeployment(queryClient, result, variables.previousId)
      setEditing(null)
      setCollapsed(false)
      if (!variables.previousId && syncAvailable) sync.mutate()
    },
  })
  const rename = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => dataSource.renameSelfHostedGateway(id, name),
    onSuccess: async (_, variables) => {
      await storeGatewayDeploymentName(queryClient, variables.id, variables.name)
      setRenaming(null)
    },
  })
  const validate = useMutation({
    mutationFn: dataSource.validateSelfHostedGateway,
    onSuccess: result => storeGatewayDeployment(queryClient, result),
    onSettled: () => { void queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets }) },
  })
  const retire = useMutation({
    mutationFn: dataSource.removeSelfHostedGateway,
    onSuccess: async (_, id) => {
      await storeGatewayDeployment(queryClient, null, id)
      setRetiring(null)
      if (syncAvailable) sync.mutate()
    },
  })
  useEffect(() => {
    if (operation.data?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: finopsKeys.apimTargets })
    }
  }, [operation.data?.status, queryClient])
  const running = sync.isPending || Boolean(operationId && (!operation.data || !terminalStatuses.has(operation.data.status)))
  const busy = running || saveGateway.isPending || rename.isPending || validate.isPending || retire.isPending
  const entryAvailable = (entry: GatewayEntry) => target.deployments.some(item => item.deployment.id === entry.deployment.id && !item.deployment.retired_at)
  const failure = validate.error?.message ?? sync.error?.message ?? operation.error?.message
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
        {canManage && lifecycleAvailable ? <DropdownMenu>
          <DropdownMenuTrigger render={<button type="button" />} title="APIM actions" aria-label="APIM actions" disabled={busy}><MoreHorizontal size={14} /></DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="gateway-actions-menu">
            {onRename && <DropdownMenuItem onClick={onRename}><TextCursorInput size={14} />Name</DropdownMenuItem>}
            <DropdownMenuItem disabled={target.target.capabilities.self_hosted_gateway === "unsupported"} onClick={() => { saveGateway.reset(); setEditing({}) }}><Plus size={14} />Add Self-hosted Gateway</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu> : onRename && <button type="button" title="Name" aria-label="Name" onClick={onRename} disabled={busy}><TextCursorInput size={14} /></button>}
        {canManage && <button type="button" title={running ? "Synchronizing" : "Synchronize inventory"} aria-label="Synchronize inventory" disabled={busy || !syncAvailable} onClick={() => sync.mutate()}><RefreshCw size={14} className={running ? "spin" : ""} /></button>}
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
        <div className="model-row-actions">
          <button type="button" title="Gateway details" aria-label="Gateway details" onClick={() => setDetails(entry)}><Eye size={14} /></button>
          {canManage && lifecycleAvailable && entry.deployment.kind === "self_hosted" && <DropdownMenu>
            <DropdownMenuTrigger render={<button type="button" />} title="Gateway actions" aria-label="Gateway actions" disabled={busy}><MoreHorizontal size={14} /></DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="gateway-actions-menu">
              <DropdownMenuItem onClick={() => { rename.reset(); setRenaming(entry) }}><TextCursorInput size={14} />Gateway name</DropdownMenuItem>
              <DropdownMenuItem disabled={!entry.deployment.endpoint_url} onClick={() => validate.mutate(entry.deployment.id)}><Wifi size={14} />Validate connection</DropdownMenuItem>
              <DropdownMenuItem disabled={!syncAvailable} onClick={() => sync.mutate()}><RefreshCw size={14} />Synchronize inventory</DropdownMenuItem>
              <DropdownMenuItem onClick={() => { saveGateway.reset(); setEditing({ entry }) }}><ArrowRightLeft size={14} />Change APIM association</DropdownMenuItem>
              <DropdownMenuItem variant="destructive" onClick={() => { retire.reset(); setRetiring(entry) }}><Trash2 size={14} />Retire Gateway</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>}
        </div>
      </article>
    })}
    {details && <GatewayInventoryDialog entry={details} onClose={() => setDetails(null)} />}
    {editing && <SelfHostedGatewayEditor targets={targets.length ? targets : [target]} initialTargetId={profile.id} entry={editing.entry} preview={preview.data ?? null} loading={preview.isFetching} available={canManage && lifecycleAvailable && (!editing.entry || entryAvailable(editing.entry) && !preview.error)} busy={saveGateway.isPending} error={saveGateway.error?.message ?? preview.error?.message ?? null} onClose={() => setEditing(null)} onSave={(targetId, value) => saveGateway.mutateAsync({ targetId, value, previousId: editing.entry?.deployment.id })} />}
    {renaming && <GatewayDeploymentNameDialog entry={renaming} available={canManage && lifecycleAvailable && entryAvailable(renaming)} busy={rename.isPending} error={rename.error?.message ?? null} onClose={() => setRenaming(null)} onSave={name => rename.mutateAsync({ id: renaming.deployment.id, name })} />}
    {retiring && <GatewayRetirementDialog entry={retiring} preview={preview.data ?? null} loading={preview.isFetching} available={canManage && lifecycleAvailable && entryAvailable(retiring) && !preview.error} busy={retire.isPending} error={retire.error?.message ?? preview.error?.message ?? null} onClose={() => setRetiring(null)} onConfirm={() => retire.mutateAsync(retiring.deployment.id)} />}
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