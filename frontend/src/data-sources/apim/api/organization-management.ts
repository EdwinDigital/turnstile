import { apiUrl, ApiError, SESSION_EXPIRED_EVENT } from "../../../api/client"

export type DirectoryStatus = "active" | "inactive" | "archived"
export type DirectoryCapabilities = {
  can_read: boolean
  can_manage_organizations: boolean
  can_manage_connections: boolean
  capabilities: string[]
  department_ids: string[]
  permission_revision: number
  directory_version: number
  protocol_version: number
  can_schedule_transfers?: boolean
  gateway_projected_version?: number
  gateway_projection_state?: "pending" | "confirmed"
  sync_available?: boolean
  identity_projection_available?: boolean
  managed_identity_tenant_id?: string | null
  deployment_cloud?: "public" | "usgov" | "china"
}
export type DirectoryOrganization = {
  id: string; code: string; name: string; description: string
  contact_email: string | null; status: DirectoryStatus; revision: number
}
export type DirectoryUnit = Omit<DirectoryOrganization, "contact_email"> & {
  organization_id: string; parent_unit_id: string | null; kind: "department" | "team"
}
export type DirectoryPerson = {
  id: string; governance_user_id: string; display_name: string; contact_email: string | null
  employee_number: string | null; job_title: string; status: DirectoryStatus
  manual_disabled: boolean; revision: number; department_id: string | null
  department_name: string | null; organization_id: string | null; team_ids: string[]
  manual_team_ids: string[]; source_team_ids: string[]
  app_user_id: string | null; account_email: string | null; account_enabled: boolean | null
  externally_managed: boolean
  source_disabled: boolean
}
export type DirectoryAccount = {
  id: string; email: string; display_name: string | null; role: "owner" | "member"; enabled: boolean
}
export type DirectoryAdministrator = {
  id: string; app_user_id: string; department_id: string; capabilities: string[]
  email: string; display_name: string | null
}
export type DirectoryAudit = {
  id: string; entity_type: string; entity_id: string; action: string
  actor_label: string; source: string; changed_at: string; reason: string
  before_value: Record<string, unknown> | null; after_value: Record<string, unknown> | null
}
export type DirectoryPeoplePage = {
  items: DirectoryPerson[]; total: number; next_cursor: string | null; generated_at: string
}

export type DirectoryConnection = {
  id: string; organization_id: string; name: string; cloud: "public" | "usgov" | "china"
  tenant_id: string; client_id: string | null
  authentication_mode: "managed_identity" | "key_vault_secret"
  scope: "selected_groups" | "all_users"; default_department_id: string | null
  include_guests: boolean; enabled: boolean; sync_interval_minutes: number
  revision: number; validated_revision: number | null; validated_at: string | null
  last_success_at: string | null; credential_configured: boolean
}
export type DirectoryGroupMapping = {
  group_id: string; unit_id: string; membership_mode: "direct" | "transitive"; enabled: boolean
}
export type DirectorySyncJob = {
  id: string; connection_id: string; connection_revision: number; directory_version: number
  mode: "full" | "delta"; status: "queued" | "running" | "awaiting_review" | "applying" | "succeeded" | "failed" | "cancelled" | "conflicted"
  summary: { create?: number; update?: number; conflicts?: number; ignored?: number; missing?: number; mass_change?: boolean }
  error_code: string | null; created_at: string; completed_at: string | null
}
export type DirectorySyncChange = {
  object_id: string; decision: string; conflict_code: string | null
  payload: { profile?: { displayName?: string; mail?: string; userPrincipalName?: string }; department_id?: string; person_id?: string }
}
export type DirectoryTransfer = {
  id: string; person_id: string; display_name: string; governance_user_id: string
  source_department_name: string; target_department_name: string; effective_month: string
  source_department_id: string; target_department_id: string
  source_organization_id: string; target_organization_id: string
  status: "scheduled" | "completed" | "conflicted" | "cancelled"; conflict_code: string | null
}
export type DirectoryCandidate = {
  id: string; original_user_id: string; organization_id: string | null
  department_id: string | null; source: string; status: string; last_seen_at: string
}

async function directoryRequest<T>(
  path: string, method = "GET", body?: unknown, idempotencyKey?: string,
): Promise<T> {
  const response = await fetch(apiUrl(`/api/v1/organization-management/${path}`), {
    method, credentials: "include", cache: "no-store",
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (response.status === 401) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  if (!response.ok) {
    const error = await response.json().catch(() => null) as {
      detail?: string | Array<{ msg: string }>; code?: string
    } | null
    const detail = Array.isArray(error?.detail)
      ? error.detail.map((entry) => entry.msg).join(" ")
      : error?.detail
    throw new ApiError(response.status, detail || "组织信息请求失败。")
  }
  return response.json() as Promise<T>
}

export const directoryApi = {
  capabilities: () => directoryRequest<DirectoryCapabilities>("capabilities"),
  organizations: () => directoryRequest<DirectoryOrganization[]>("organizations"),
  createOrganization: (value: unknown, key: string) =>
    directoryRequest<DirectoryOrganization>("organizations", "POST", value, key),
  updateOrganization: (id: string, value: unknown) =>
    directoryRequest<DirectoryOrganization>(`organizations/${encodeURIComponent(id)}`, "PATCH", value),
  units: (organizationId: string) =>
    directoryRequest<DirectoryUnit[]>(`organizations/${encodeURIComponent(organizationId)}/units`),
  createUnit: (organizationId: string, value: unknown, key: string) =>
    directoryRequest<DirectoryUnit>(`organizations/${encodeURIComponent(organizationId)}/units`, "POST", value, key),
  updateUnit: (id: string, value: unknown) =>
    directoryRequest<DirectoryUnit>(`units/${encodeURIComponent(id)}`, "PATCH", value),
  people: (values: { organization_id?: string; department_id?: string; team_id?: string; query?: string; status?: DirectoryStatus; cursor?: string }) =>
    directoryRequest<DirectoryPeoplePage>(`people?${new URLSearchParams(
      Object.entries(values).filter(([, value]) => Boolean(value)),
    )}`),
  createPerson: (value: unknown, key: string) =>
    directoryRequest<DirectoryPerson>("people", "POST", value, key),
  updatePerson: (id: string, value: unknown) =>
    directoryRequest<DirectoryPerson>(`people/${encodeURIComponent(id)}`, "PATCH", value),
  setTeams: (id: string, value: unknown) =>
    directoryRequest<DirectoryPerson>(`people/${encodeURIComponent(id)}/teams`, "PUT", value),
  accounts: (query = "") =>
    directoryRequest<DirectoryAccount[]>(`accounts?${new URLSearchParams({ query })}`),
  linkAccount: (id: string, value: unknown) =>
    directoryRequest<DirectoryPerson>(`people/${encodeURIComponent(id)}/account-link`, "POST", value),
  administrators: (departmentId: string) =>
    directoryRequest<DirectoryAdministrator[]>(`departments/${encodeURIComponent(departmentId)}/administrators`),
  setAdministrators: (departmentId: string, value: unknown) =>
    directoryRequest<DirectoryUnit>(`departments/${encodeURIComponent(departmentId)}/administrators`, "PUT", value),
  audit: (departmentId?: string) =>
    directoryRequest<DirectoryAudit[]>(`audit?${new URLSearchParams(departmentId ? { department_id: departmentId } : {})}`),
  statusPreview: (id: string) =>
    directoryRequest<{ person: DirectoryPerson; budgets: Array<{ period_start: string; token_limit: number }> }>(`people/${encodeURIComponent(id)}/status-preview`, "POST"),
  setStatus: (id: string, value: unknown) =>
    directoryRequest<DirectoryPerson>(`people/${encodeURIComponent(id)}/status-changes`, "POST", value),
  transferPreview: (id: string, value: unknown) =>
    directoryRequest<{ future_budgets: unknown[]; effective_month: string; preview_digest: string }>(`people/${encodeURIComponent(id)}/transfer-preview`, "POST", value),
  transfer: (id: string, value: unknown, key: string) =>
    directoryRequest<Record<string, unknown>>(`people/${encodeURIComponent(id)}/transfers`, "POST", value, key),
  transfers: () => directoryRequest<DirectoryTransfer[]>("transfers"),
  cancelTransfer: (id: string) => directoryRequest<Record<string, unknown>>(`transfers/${id}/cancel`, "POST", { reason: "" }),
  identityConflicts: (query: string) => directoryRequest<DirectoryCandidate[]>(`identity-conflicts?${new URLSearchParams({ query })}`),
  resolveIdentityConflict: (id: string, value: unknown) =>
    directoryRequest<Record<string, unknown>>(`identity-conflicts/${id}/resolve`, "POST", value),
  connections: () => directoryRequest<DirectoryConnection[]>("connections"),
  createConnection: (value: unknown, key: string) =>
    directoryRequest<DirectoryConnection>("connections", "POST", value, key),
  updateConnection: (id: string, value: unknown) =>
    directoryRequest<DirectoryConnection>(`connections/${id}`, "PATCH", value),
  validateConnection: (id: string) =>
    directoryRequest<{ validated: boolean }>(`connections/${id}/validate`, "POST"),
  mappings: (id: string) =>
    directoryRequest<DirectoryGroupMapping[]>(`connections/${id}/group-mappings`),
  setMappings: (id: string, items: DirectoryGroupMapping[], revision: number) =>
    directoryRequest<DirectoryConnection>(`connections/${id}/group-mappings`, "PUT", {
      items, expected_revision: revision,
    }),
  sync: (id: string, mode: "full" | "delta", key: string) =>
    directoryRequest<DirectorySyncJob>(`connections/${id}/sync-jobs`, "POST", { mode }, key),
  syncJobs: (id: string) =>
    directoryRequest<DirectorySyncJob[]>(`sync-jobs?${new URLSearchParams({ connection_id: id })}`),
  syncChanges: (id: string, offset: number) =>
    directoryRequest<{ total: number; items: DirectorySyncChange[] }>(`sync-jobs/${id}/changes?offset=${offset}`),
  applySync: (id: string, version: number, approveMissing: boolean, approveMass: boolean) =>
    directoryRequest<DirectorySyncJob>(`sync-jobs/${id}/apply`, "POST", {
      expected_directory_version: version, approve_missing: approveMissing, approve_mass_changes: approveMass,
    }),
  cancelSync: (id: string) =>
    directoryRequest<DirectorySyncJob>(`sync-jobs/${id}/cancel`, "POST"),
  bindExternalPerson: (connectionId: string, objectId: string, value: unknown) =>
    directoryRequest<Record<string, unknown>>(`connections/${connectionId}/users/${objectId}/binding`, "POST", value),
}
