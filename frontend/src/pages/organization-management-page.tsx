import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Building2, ChevronLeft, ChevronRight, Ellipsis, FolderTree, Link2, PanelLeft,
  Pencil, Plus, RefreshCw, Save, Shield, UserRound, Users,
} from "lucide-react"
import {
  directoryApi, type DirectoryCapabilities, type DirectoryOrganization,
  type DirectoryPerson, type DirectoryStatus, type DirectoryUnit,
} from "../data-sources/apim/api/organization-management"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Textarea } from "../components/ui/textarea"
import { Checkbox } from "../components/ui/checkbox"
import { Alert, AlertDescription } from "../components/ui/alert"
import { Badge } from "../components/ui/badge"
import { Empty, EmptyContent, EmptyTitle } from "../components/ui/empty"
import { Field, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "../components/ui/field"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs"
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "../components/ui/dialog"
import { confirmDirectoryLeave, DirectorySelect as MenuSelect, useDirectoryLeaveGuard } from "../components/directory-controls"
import { DirectorySyncPanel } from "./directory-sync-panel"
import { DirectoryCandidatesPanel } from "./directory-candidates-panel"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { ResizableTable } from "../components/ui/resizable-table"
import { cn } from "../lib/utils"
import { useAuth } from "../providers/auth-provider"
import { useTimezone } from "../providers/timezone-provider"
import { getIntlLocale } from "../locales/index"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"

const statusNames: Record<DirectoryStatus, string> = { active: "启用", inactive: "停用", archived: "已归档" }
const actionNames: Record<string, string> = {
  created: "新增", updated: "编辑", account_linked: "关联账号", status_changed: "状态变更",
  teams_updated: "团队成员变更", administrators_updated: "管理员变更", scheduled: "计划调岗",
}
type Editor = {
  kind: "organization" | "department" | "team" | "person"
  row?: DirectoryOrganization | DirectoryUnit | DirectoryPerson
}

function FormInput({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return <Field><FieldLabel htmlFor={id}>{label}</FieldLabel>{children}</Field>
}

function DirectoryEditor({ editor, organizationId, departmentId, units, onClose, onSaved }: {
  editor: Editor; organizationId: string; departmentId: string; units: DirectoryUnit[]
  onClose: () => void; onSaved: (id: string) => void
}) {
  const row = editor.row
  const person = editor.kind === "person" ? row as DirectoryPerson | undefined : undefined
  const entity = editor.kind !== "person" ? row as DirectoryOrganization | DirectoryUnit | undefined : undefined
  const [name, setName] = useState(person?.display_name ?? entity?.name ?? "")
  const [code, setCode] = useState(entity?.code ?? "")
  const [email, setEmail] = useState(person?.contact_email ?? (
    entity && "contact_email" in entity ? entity.contact_email ?? "" : ""
  ))
  const [identity, setIdentity] = useState(person?.governance_user_id ?? "")
  const [employee, setEmployee] = useState(person?.employee_number ?? "")
  const [title, setTitle] = useState(person?.job_title ?? "")
  const [description, setDescription] = useState(entity?.description ?? "")
  const [status, setStatus] = useState<DirectoryStatus>(entity?.status ?? "active")
  const [teamIds, setTeamIds] = useState(person?.team_ids ?? [])
  const [dirty, setDirty] = useState(false)
  useDirectoryLeaveGuard(dirty)
  const key = useMemo(() => crypto.randomUUID(), [])
  const label = { organization: "组织", department: "部门", team: "团队", person: "人员" }[editor.kind]
  const mutation = useMutation({
    mutationFn: async () => {
      if (editor.kind === "person") {
        if (person) return directoryApi.updatePerson(person.id, {
          display_name: name, contact_email: email || null, employee_number: employee || null,
          job_title: title, expected_revision: person.revision,
        })
        return directoryApi.createPerson({
          display_name: name, governance_user_id: identity, contact_email: email || identity,
          employee_number: employee || null, job_title: title, department_id: departmentId,
          team_ids: teamIds,
        }, key)
      }
      const value = { code, name, description, status, expected_revision: entity?.revision }
      if (editor.kind === "organization") {
        return entity ? directoryApi.updateOrganization(entity.id, { ...value, contact_email: email || null })
          : directoryApi.createOrganization({ ...value, contact_email: email || null }, key)
      }
      const unitValue = {
        ...value, kind: editor.kind,
        parent_unit_id: editor.kind === "team"
          ? (entity as DirectoryUnit | undefined)?.parent_unit_id ?? departmentId : null,
      }
      return entity ? directoryApi.updateUnit(entity.id, unitValue)
        : directoryApi.createUnit(organizationId, unitValue, key)
    },
    onSuccess: (saved) => onSaved(saved.id),
  })
  const change = (setter: (value: string) => void) => (value: string) => {
    setter(value); setDirty(true)
  }
  const close = () => {
    if (!mutation.isPending && (!dirty || window.confirm("放弃未保存的更改？"))) onClose()
  }
  const submit = (event: FormEvent) => { event.preventDefault(); mutation.mutate() }
  const teams = units.filter((unit) => unit.kind === "team" && unit.parent_unit_id === departmentId && unit.status === "active")
  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>{entity || person ? "编辑" : "新增"}{label}</DialogTitle>
        <DialogDescription>{person?.externally_managed ? "Entra ID" : label}</DialogDescription>
      </DialogHeader>
      <form onSubmit={submit}>
        <FieldGroup>
          <FormInput id="directory-name" label="名称">
            <Input id="directory-name" autoFocus value={name} required maxLength={160}
              disabled={person?.externally_managed} onChange={(event) => change(setName)(event.target.value)} />
          </FormInput>
          {editor.kind !== "person" && <FormInput id="directory-code" label="编码">
            <Input id="directory-code" value={code} required maxLength={64} pattern={"[a-zA-Z0-9][a-zA-Z0-9_\\-]*"}
              onChange={(event) => change(setCode)(event.target.value)} />
          </FormInput>}
          {editor.kind === "person" && !person && <FormInput id="directory-identity" label="人员标识">
            <Input id="directory-identity" type="email" value={identity} required maxLength={255}
              onChange={(event) => change(setIdentity)(event.target.value)} />
          </FormInput>}
          {(editor.kind === "person" || editor.kind === "organization") && <FormInput id="directory-email" label="联系邮箱">
            <Input id="directory-email" type="email" value={email} maxLength={320}
              disabled={person?.externally_managed} onChange={(event) => change(setEmail)(event.target.value)} />
          </FormInput>}
          {editor.kind === "person" ? <>
            <FormInput id="directory-employee" label="员工号"><Input id="directory-employee" value={employee}
              maxLength={64} onChange={(event) => change(setEmployee)(event.target.value)} /></FormInput>
            <FormInput id="directory-title" label="岗位"><Input id="directory-title" value={title}
              disabled={person?.externally_managed}
              maxLength={160} onChange={(event) => change(setTitle)(event.target.value)} /></FormInput>
            {!person && teams.length > 0 && <FieldSet><FieldLegend>团队</FieldLegend>
              <div className="directory-checkboxes">{teams.map((team) => <label key={team.id}>
                <Checkbox checked={teamIds.includes(team.id)} onCheckedChange={(checked) => {
                  setTeamIds((current) => checked ? [...current, team.id] : current.filter((id) => id !== team.id))
                  setDirty(true)
                }} />{team.name}
              </label>)}</div>
            </FieldSet>}
          </> : <>
            <FormInput id="directory-description" label="描述"><Textarea id="directory-description"
              value={description} maxLength={2000} onChange={(event) => change(setDescription)(event.target.value)} /></FormInput>
            {entity && <Field><FieldLabel>状态</FieldLabel><MenuSelect label="状态" value={status}
              items={Object.entries(statusNames).map(([value, label]) => ({ value, label }))}
              onChange={(value) => { setStatus(value as DirectoryStatus); setDirty(true) }} /></Field>}
          </>}
        </FieldGroup>
        {mutation.error && <Alert><AlertDescription>{mutation.error.message}</AlertDescription></Alert>}
        <DialogFooter><Button type="button" variant="outline" onClick={close} disabled={mutation.isPending}>取消</Button>
          <Button type="submit" disabled={mutation.isPending || (Boolean(row) && !dirty)}>
            {mutation.isPending ? <RefreshCw className="spin" data-icon="inline-start" /> : <Save data-icon="inline-start" />}保存
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function PersonActionDialog({ person, action, units, onClose, onSaved }: {
  person: DirectoryPerson; action: "link" | "teams" | "status" | "transfer"
  units: DirectoryUnit[]; onClose: () => void; onSaved: () => void
}) {
  const [accountId, setAccountId] = useState(person.app_user_id ?? "")
  const [accountQuery, setAccountQuery] = useState(person.account_email ?? "")
  const manualTeams = person.manual_team_ids ?? (person.externally_managed ? [] : person.team_ids)
  const sourceTeams = person.source_team_ids ?? (person.externally_managed ? person.team_ids : [])
  const [teamIds, setTeamIds] = useState(manualTeams)
  const [target, setTarget] = useState("")
  const [targetOrganization, setTargetOrganization] = useState(person.organization_id ?? "")
  const [month, setMonth] = useState(() => {
    const now = new Date()
    return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString().slice(0, 7)
  })
  const [disableAccount, setDisableAccount] = useState(false)
  const [reason, setReason] = useState("")
  const [previewed, setPreviewed] = useState(false)
  useDirectoryLeaveGuard(Boolean(reason || target || disableAccount || accountId !== (person.app_user_id ?? "")
    || [...teamIds].sort().join() !== [...manualTeams].sort().join()))
  const key = useMemo(() => crypto.randomUUID(), [])
  const accounts = useQuery({
    queryKey: ["directory", "accounts", accountQuery],
    queryFn: () => directoryApi.accounts(accountQuery), enabled: action === "link",
  })
  const statusPreview = useQuery({
    queryKey: ["directory", "status-preview", person.id], queryFn: () => directoryApi.statusPreview(person.id),
    enabled: action === "status",
  })
  const organizations = useQuery({
    queryKey: ["directory", "transfer-organizations"], queryFn: directoryApi.organizations,
    enabled: action === "transfer",
  })
  const targetUnits = useQuery({
    queryKey: ["directory", "transfer-units", targetOrganization],
    queryFn: () => directoryApi.units(targetOrganization), enabled: action === "transfer" && Boolean(targetOrganization),
  })
  const transferValue = { target_department_id: target, effective_month: `${month}-01`, expected_revision: person.revision, reason }
  const preview = useMutation({
    mutationFn: () => directoryApi.transferPreview(person.id, transferValue),
    onSuccess: () => setPreviewed(true),
  })
  const save = useMutation({
    mutationFn: async () => {
      const common = { expected_revision: person.revision, reason }
      if (action === "link") return directoryApi.linkAccount(person.id, { ...common, app_user_id: accountId })
      if (action === "teams") return directoryApi.setTeams(person.id, { ...common, team_ids: teamIds })
      if (action === "status") return directoryApi.setStatus(person.id, {
        ...common, status: person.status === "active" ? "inactive" : "active", disable_account: disableAccount,
      })
      return directoryApi.transfer(person.id, { ...transferValue, preview_digest: preview.data?.preview_digest }, key)
    },
    onSuccess: onSaved,
  })
  const label = { link: "关联账号", teams: "团队成员", status: "人员状态", transfer: "计划调岗" }[action]
  const teams = units.filter((unit) => unit.kind === "team" && unit.parent_unit_id === person.department_id && unit.status === "active")
  const departments = (targetUnits.data ?? units).filter((unit) => unit.kind === "department"
    && unit.id !== person.department_id && unit.status === "active")
  const allowed = action === "link" ? Boolean(accountId)
    : action === "transfer" ? previewed : action === "status" ? Boolean(statusPreview.data) : true
  return <Dialog open onOpenChange={(open) => { if (!open && !save.isPending) onClose() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>{label}</DialogTitle><DialogDescription data-no-localize>{person.display_name}</DialogDescription></DialogHeader>
      <FieldGroup>
        {action === "link" && <FormInput id="directory-account-search" label="搜索登录账号">
          <Input id="directory-account-search" value={accountQuery} maxLength={200}
            onChange={(event) => { setAccountQuery(event.target.value); setAccountId("") }} />
        </FormInput>}
        {action === "link" && <Field><FieldLabel>登录账号</FieldLabel><MenuSelect label="登录账号" value={accountId}
          items={(accounts.data ?? []).map((account) => ({ value: account.id, label: `${account.display_name ?? account.email} · ${account.email}` }))}
          onChange={setAccountId} /></Field>}
        {action === "teams" && <FieldSet><FieldLegend>团队</FieldLegend><div className="directory-checkboxes">
          {teams.map((team) => <label key={team.id}><Checkbox
            checked={teamIds.includes(team.id) || sourceTeams.includes(team.id)}
            disabled={sourceTeams.includes(team.id)}
            onCheckedChange={(checked) => setTeamIds((current) => checked ? [...current, team.id] : current.filter((id) => id !== team.id))} />
            {team.name}{sourceTeams.includes(team.id) && <Badge variant="outline">Entra ID</Badge>}</label>)}
          {teams.length === 0 && <span>暂无团队</span>}
        </div></FieldSet>}
        {action === "status" && <>
          <div className="directory-facts"><span>人员状态</span><Badge>{statusNames[person.status]}</Badge>
            <span>变更后</span><Badge>{person.status === "active" ? "停用" : "启用"}</Badge>
            <span>历史预算</span><span>{statusPreview.data?.budgets.length ?? "--"}</span>
            <span>网关归属</span><Badge variant="outline">待同步</Badge>
          </div>
          {person.status === "active" && person.app_user_id && <label className="directory-checkbox-line">
            <Checkbox checked={disableAccount} onCheckedChange={(checked) => setDisableAccount(Boolean(checked))} />同时停用登录账号
          </label>}
        </>}
        {action === "transfer" && <>
          <Field><FieldLabel>目标组织</FieldLabel><MenuSelect label="目标组织" value={targetOrganization}
            items={(organizations.data ?? []).filter((org) => org.status === "active").map((org) => ({ value: org.id, label: org.name }))}
            onChange={(value) => { setTargetOrganization(value); setTarget(""); setPreviewed(false) }} /></Field>
          <Field><FieldLabel>目标部门</FieldLabel><MenuSelect label="目标部门" value={target}
            items={departments.map((unit) => ({ value: unit.id, label: unit.name }))}
            onChange={(value) => { setTarget(value); setPreviewed(false) }} /></Field>
          <FormInput id="directory-transfer-month" label="生效月份"><Input id="directory-transfer-month" type="month"
            value={month} onChange={(event) => { setMonth(event.target.value); setPreviewed(false) }} /></FormInput>
          <Button variant="outline" disabled={!target || !month || preview.isPending} onClick={() => preview.mutate()}>
            <RefreshCw data-icon="inline-start" />检查影响
          </Button>
          {preview.data && <div className="directory-facts"><span>未来预算</span><span>{preview.data.future_budgets.length}</span>
            <span>生效日期</span><span>{preview.data.effective_month}</span>
          </div>}
        </>}
        <FormInput id="directory-action-reason" label="变更原因"><Textarea id="directory-action-reason" value={reason}
          maxLength={2000} onChange={(event) => { setReason(event.target.value); setPreviewed(false) }} /></FormInput>
      </FieldGroup>
      {(save.error || preview.error || accounts.error || statusPreview.error) && <Alert><AlertDescription>
        {(save.error || preview.error || accounts.error || statusPreview.error)?.message}
      </AlertDescription></Alert>}
      <DialogFooter><DialogClose render={<Button variant="outline" disabled={save.isPending} />}>取消</DialogClose>
        <Button disabled={!allowed || save.isPending} onClick={() => save.mutate()}><Save data-icon="inline-start" />确认</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

function DepartmentAdministrators({ department, owner, scopeKey, onSaved }: {
  department: DirectoryUnit; owner: boolean; scopeKey: string; onSaved: () => void
}) {
  const existing = useQuery({
    queryKey: ["directory", "administrators", department.id, scopeKey],
    queryFn: () => directoryApi.administrators(department.id),
  })
  const [selection, setSelection] = useState<string[] | null>(null)
  const [query, setQuery] = useState("")
  const [cursor, setCursor] = useState("")
  const [previous, setPrevious] = useState<string[]>([])
  const people = useQuery({
    queryKey: ["directory", "administrator-candidates", department.id, query, cursor, scopeKey],
    queryFn: () => directoryApi.people({ department_id: department.id, status: "active", query, cursor }),
  })
  useDirectoryLeaveGuard(selection !== null)
  const selected = selection ?? existing.data?.map((row) => row.app_user_id) ?? []
  const save = useMutation({
    mutationFn: () => directoryApi.setAdministrators(department.id, {
      account_ids: selected, expected_revision: department.revision,
    }),
    onSuccess: () => { setSelection(null); onSaved() },
  })
  const candidates = (people.data?.items ?? []).filter((person) => person.app_user_id && person.account_enabled
    && !person.manual_disabled && !person.source_disabled)
  return <div className="directory-admins">
    <Input aria-label="搜索管理员候选人" placeholder="搜索人员" value={query}
      onChange={(event) => { setQuery(event.target.value); setCursor(""); setPrevious([]) }} />
    {existing.error && <Alert><AlertDescription>{existing.error.message}</AlertDescription></Alert>}
    <FieldSet><FieldLegend>部门管理员</FieldLegend><div className="directory-checkboxes">
      {candidates.map((person) => <label key={person.id}><Checkbox
        checked={selected.includes(person.app_user_id!)} disabled={!owner}
        onCheckedChange={(checked) => setSelection(checked ? [...selected, person.app_user_id!] : selected.filter((id) => id !== person.app_user_id))} />
        <span data-no-localize>{person.display_name}</span><small data-no-localize>{person.account_email}</small>
      </label>)}
    </div></FieldSet>
    {(existing.data ?? []).filter((admin) => !candidates.some((person) => person.app_user_id === admin.app_user_id)).map((admin) =>
      <label key={admin.id} className="directory-existing-admin">
        <Checkbox checked={selected.includes(admin.app_user_id)} disabled={!owner}
          onCheckedChange={(checked) => setSelection(checked ? [...selected, admin.app_user_id] : selected.filter((id) => id !== admin.app_user_id))} />
        <Shield /><span data-no-localize>{admin.display_name ?? admin.email}</span><small data-no-localize>{admin.email}</small></label>,
    )}
    {people.isPending ? <Empty><EmptyTitle>正在加载</EmptyTitle></Empty>
      : candidates.length === 0 && <Empty><EmptyTitle>暂无已关联账号的人员</EmptyTitle></Empty>}
    {people.error && <Alert><AlertDescription>{people.error.message}</AlertDescription></Alert>}
    <div className="directory-pagination">
      <Button variant="outline" size="icon-sm" aria-label="上一页" title="上一页" disabled={!previous.length}
        onClick={() => { setCursor(previous.at(-1) ?? ""); setPrevious((values) => values.slice(0, -1)) }}><ChevronLeft /></Button>
      <Button variant="outline" size="icon-sm" aria-label="下一页" title="下一页" disabled={!people.data?.next_cursor}
        onClick={() => { setPrevious((values) => [...values, cursor]); setCursor(people.data?.next_cursor ?? "") }}><ChevronRight /></Button>
    </div>
    {save.error && <Alert><AlertDescription>{save.error.message}</AlertDescription></Alert>}
    {owner && <Button disabled={selection === null || save.isPending} onClick={() => save.mutate()}>
      <Save data-icon="inline-start" />保存管理员
    </Button>}
  </div>
}

export function OrganizationManagementPage({ onToggleSidebar, capabilities }: {
  onToggleSidebar: () => void; capabilities: DirectoryCapabilities | undefined
}) {
  const { user, refreshProfile } = useAuth()
  const { timezone } = useTimezone()
  const queryClient = useQueryClient()
  const [organizationId, setOrganizationId] = useState(() => new URLSearchParams(location.search).get("directory_org") ?? "")
  const [unitId, setUnitId] = useState(() => new URLSearchParams(location.search).get("directory_unit") ?? "")
  const [query, setQuery] = useState("")
  const [debouncedQuery, setDebouncedQuery] = useState("")
  const [status, setStatus] = useState<DirectoryStatus | "all">("all")
  const [cursor, setCursor] = useState("")
  const [previousCursors, setPreviousCursors] = useState<string[]>([])
  const [editor, setEditor] = useState<Editor | null>(null)
  const [action, setAction] = useState<{ person: DirectoryPerson; action: "link" | "teams" | "status" | "transfer" } | null>(null)
  const [tab, setTab] = useState("people")
  const canRead = capabilities?.can_read === true
  const owner = capabilities?.can_manage_organizations === true
  const scope = [user?.id, capabilities?.permission_revision]
  const organizations = useQuery({
    queryKey: ["directory", "organizations", ...scope], queryFn: directoryApi.organizations,
    enabled: canRead,
  })
  const effectiveOrgId = organizationId || organizations.data?.[0]?.id || ""
  const organization = organizations.data?.find((row) => row.id === effectiveOrgId)
  const units = useQuery({
    queryKey: ["directory", "units", effectiveOrgId, ...scope],
    queryFn: () => directoryApi.units(effectiveOrgId), enabled: canRead && Boolean(organization),
  })
  const selectedUnit = units.data?.find((row) => row.id === unitId)
  const departmentId = selectedUnit?.kind === "department" ? selectedUnit.id : selectedUnit?.parent_unit_id ?? ""
  const department = units.data?.find((row) => row.id === departmentId)
  const teamId = selectedUnit?.kind === "team" ? selectedUnit.id : undefined
  const people = useQuery({
    queryKey: ["directory", "people", effectiveOrgId, departmentId, teamId, debouncedQuery, status, cursor, ...scope],
    queryFn: () => directoryApi.people({
      organization_id: effectiveOrgId,
      ...(departmentId ? { department_id: departmentId } : {}),
      ...(teamId ? { team_id: teamId } : {}), query: debouncedQuery,
      ...(status !== "all" ? { status } : {}), ...(cursor ? { cursor } : {}),
    }),
    enabled: canRead && Boolean(organization) && (!unitId || Boolean(selectedUnit)) && tab === "people",
  })
  const audit = useQuery({
    queryKey: ["directory", "audit", departmentId, ...scope],
    queryFn: () => directoryApi.audit(departmentId || undefined),
    enabled: canRead && tab === "audit",
  })
  const transfers = useQuery({
    queryKey: ["directory", "transfers", ...scope], queryFn: directoryApi.transfers,
    enabled: canRead && owner && tab === "transfers",
  })
  const cancelTransfer = useMutation({
    mutationFn: directoryApi.cancelTransfer,
    onSuccess: () => changed(),
  })
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 250)
    return () => clearTimeout(timer)
  }, [query])
  useEffect(() => { setCursor(""); setPreviousCursors([]) }, [unitId, effectiveOrgId, debouncedQuery, status])
  useEffect(() => {
    const url = new URL(location.href)
    if (organizationId) url.searchParams.set("directory_org", organizationId); else url.searchParams.delete("directory_org")
    if (unitId) url.searchParams.set("directory_unit", unitId); else url.searchParams.delete("directory_unit")
    history.replaceState(null, "", url)
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }, [organizationId, unitId])
  const changed = async () => {
    setEditor(null); setAction(null)
    await refreshProfile()
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["directory"] }),
      queryClient.invalidateQueries({ queryKey: ["reference", "enterprise-entities"] }),
      queryClient.invalidateQueries({ queryKey: ["finops"] }),
      queryClient.invalidateQueries({ queryKey: ["user-settings"] }),
    ])
  }
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["directory"] })
  const editPeople = owner || capabilities?.capabilities.includes("directory.edit_people")
  const selectOrganization = (id: string) => {
    if (confirmDirectoryLeave()) { setOrganizationId(id); setUnitId(""); setTab("people") }
  }
  const selectUnit = (id: string) => { if (confirmDirectoryLeave()) { setUnitId(id); setTab("people") } }
  const error = organizations.error || units.error || people.error || audit.error
  const missing = organizations.data && organizationId && !organization
    || units.data && unitId && !selectedUnit
  return <div className="finops-workspace directory-workspace">
    <header className="finops-header"><div>
      <Button variant="ghost" size="icon-sm" aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft /></Button>
      <span className="finops-header-icon"><FolderTree /></span><h1>组织管理</h1>
    </div><Button variant="ghost" size="icon-sm" aria-label="刷新组织信息" title="刷新组织信息" onClick={() => void refresh()}><RefreshCw /></Button></header>
    {!capabilities ? <Empty><EmptyTitle>正在加载</EmptyTitle></Empty> : !canRead ? <Empty><EmptyTitle>无组织管理权限</EmptyTitle></Empty>
      : <div className="directory-layout">
        <aside className="directory-tree" aria-label="组织目录">
          <div className="directory-tree-heading"><strong>组织目录</strong>{owner && <Button variant="ghost" size="icon-sm"
            aria-label="新增组织" title="新增组织" onClick={() => setEditor({ kind: "organization" })}><Plus /></Button>}</div>
          {(organizations.data ?? []).map((org) => <div key={org.id}>
            <button type="button" className={cn("directory-tree-node", effectiveOrgId === org.id && !unitId && "selected")}
              onClick={() => selectOrganization(org.id)} title={org.name}><Building2 /><span data-no-localize>{org.name}</span></button>
            {effectiveOrgId === org.id && (units.data ?? []).filter((unit) => unit.kind === "department").map((unit) => <div key={unit.id}>
              <button type="button" className={cn("directory-tree-node directory-tree-department", unitId === unit.id && "selected")}
                onClick={() => selectUnit(unit.id)} title={unit.name}><Users /><span data-no-localize>{unit.name}</span>
                {unit.status !== "active" && <Badge variant="outline">{statusNames[unit.status]}</Badge>}
              </button>
              {(units.data ?? []).filter((team) => team.parent_unit_id === unit.id).map((team) => <button type="button" key={team.id}
                className={cn("directory-tree-node directory-tree-team", unitId === team.id && "selected")}
                onClick={() => selectUnit(team.id)} title={team.name}><UserRound /><span data-no-localize>{team.name}</span></button>)}
            </div>)}
          </div>)}
          {organizations.data?.length === 0 && <Empty><EmptyTitle>暂无组织</EmptyTitle></Empty>}
        </aside>
        <main className="directory-main">
          {error && <Alert><AlertDescription>{error.message}</AlertDescription><Button variant="outline" size="sm" onClick={() => void refresh()}>重试</Button></Alert>}
          {missing ? <Empty><EmptyTitle>当前组织或部门不可访问</EmptyTitle></Empty> : !organization ? <Empty>
            <EmptyTitle>{organizations.isPending ? "正在加载" : "暂无组织"}</EmptyTitle>
            {owner && <EmptyContent><Button onClick={() => setEditor({ kind: "organization" })}><Plus data-icon="inline-start" />创建组织</Button></EmptyContent>}
          </Empty> : <>
            <div className="directory-context-heading"><div><h2 data-no-localize>{selectedUnit?.name ?? organization.name}</h2>
              <span>{selectedUnit?.kind === "team" ? "团队" : selectedUnit ? "部门" : "组织"}</span>
              <Badge variant="outline">{statusNames[selectedUnit?.status ?? organization.status]}</Badge>
              <small data-no-localize>{selectedUnit?.code ?? organization.code}</small>
            </div><div>
              {(owner || selectedUnit?.kind === "team") && <Button variant="outline" size="sm" onClick={() => setEditor({
                kind: selectedUnit?.kind ?? "organization", row: selectedUnit ?? organization,
              })}><Pencil data-icon="inline-start" />编辑</Button>}
              {owner && !selectedUnit && <Button variant="outline" size="sm" onClick={() => setEditor({ kind: "department" })}><Plus data-icon="inline-start" />新增部门</Button>}
              {department && (owner || capabilities?.capabilities.includes("directory.edit_teams")) &&
                <Button variant="outline" size="sm" onClick={() => setEditor({ kind: "team" })}><Plus data-icon="inline-start" />新增团队</Button>}
            </div></div>
            <Tabs value={tab} onValueChange={(value) => { if (confirmDirectoryLeave()) setTab(String(value)) }}>
              <TabsList><TabsTrigger value="people">人员</TabsTrigger>
                {department && <TabsTrigger value="administrators">管理员</TabsTrigger>}
                <TabsTrigger value="audit">变更记录</TabsTrigger>
                {owner && <TabsTrigger value="sync">Entra ID</TabsTrigger>}
                {owner && <TabsTrigger value="transfers">调岗计划</TabsTrigger>}
                {owner && <TabsTrigger value="candidates">历史身份</TabsTrigger>}
              </TabsList>
              <TabsContent value="people">
                <div className="directory-toolbar"><Input aria-label="搜索人员" placeholder="搜索人员" value={query}
                  onChange={(event) => setQuery(event.target.value)} />
                  <MenuSelect label="人员状态" value={status} items={[{ value: "all", label: "全部状态" },
                    ...Object.entries(statusNames).map(([value, label]) => ({ value, label }))]}
                    onChange={(value) => setStatus(value as DirectoryStatus | "all")} />
                  <span className="directory-count">{people.data?.total ?? "--"} 人</span>
                  {department && editPeople && <Button size="sm" onClick={() => setEditor({ kind: "person" })}><Plus data-icon="inline-start" />新增人员</Button>}
                </div>
                <div className="directory-table-scroll"><ResizableTable className="directory-table">
                  <caption className="sr-only">组织人员</caption><thead><tr>
                    <th><span>人员</span></th><th><span>部门 / 团队</span></th><th><span>岗位</span></th>
                    <th><span>账号</span></th><th><span>来源</span></th><th><span>状态</span></th><th><span>操作</span></th>
                  </tr></thead>
                  <tbody>{(people.data?.items ?? []).map((person) => <tr key={person.id}>
                    <td><strong data-no-localize>{person.display_name}</strong><small data-no-localize>{person.governance_user_id}</small>
                      {person.employee_number && <small data-no-localize>{person.employee_number}</small>}</td>
                    <td><span data-no-localize>{person.department_name ?? "--"}</span><small data-no-localize>{person.team_ids.map((id) => units.data?.find((unit) => unit.id === id)?.name ?? id).join("、") || "--"}</small></td>
                    <td data-no-localize>{person.job_title || "--"}</td>
                    <td><span data-no-localize>{person.account_email ?? "--"}</span><small>{person.app_user_id ? person.account_enabled ? "启用" : "停用" : "未关联"}</small></td>
                    <td><Badge variant="outline">{person.externally_managed ? "Entra ID" : "本地"}</Badge></td>
                    <td><Badge variant={person.status === "active" && !person.source_disabled ? "secondary" : "outline"}>
                      {person.source_disabled ? "来源已停用" : statusNames[person.status]}</Badge></td>
                    <td>{editPeople && <DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" className="directory-person-trigger"
                      aria-label={`操作 ${person.display_name}`} title="人员操作" />}><Ellipsis /></DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="directory-person-menu"><DropdownMenuGroup>
                        <DropdownMenuItem onClick={() => setEditor({ kind: "person", row: person })}><Pencil />编辑</DropdownMenuItem>
                        <DropdownMenuItem onClick={() => setAction({ person, action: "teams" })}><Users />团队成员</DropdownMenuItem>
                        {owner && <>
                          <DropdownMenuItem onClick={() => setAction({ person, action: "link" })}><Link2 />关联账号</DropdownMenuItem>
                          <DropdownMenuItem disabled={!capabilities.can_schedule_transfers}
                            onClick={() => setAction({ person, action: "transfer" })}><FolderTree />计划调岗</DropdownMenuItem>
                          <DropdownMenuItem onClick={() => setAction({ person, action: "status" })}><Shield />人员状态</DropdownMenuItem>
                        </>}
                      </DropdownMenuGroup></DropdownMenuContent>
                    </DropdownMenu>}</td>
                  </tr>)}</tbody>
                </ResizableTable></div>
                {people.isPending && <Empty><EmptyTitle>正在加载</EmptyTitle></Empty>}
                {people.data?.total === 0 && <Empty><EmptyTitle>暂无人员</EmptyTitle></Empty>}
                <div className="directory-pagination"><span>{cursor ? Number(cursor) + 1 : people.data?.total ? 1 : 0} - {Number(cursor || 0) + (people.data?.items.length ?? 0)}</span>
                  <Button variant="outline" size="icon-sm" aria-label="上一页" title="上一页" disabled={previousCursors.length === 0}
                    onClick={() => { setCursor(previousCursors.at(-1) ?? ""); setPreviousCursors((current) => current.slice(0, -1)) }}><ChevronLeft /></Button>
                  <Button variant="outline" size="icon-sm" aria-label="下一页" title="下一页" disabled={!people.data?.next_cursor}
                    onClick={() => { setPreviousCursors((current) => [...current, cursor]); setCursor(people.data?.next_cursor ?? "") }}><ChevronRight /></Button>
                </div>
              </TabsContent>
              {department && <TabsContent value="administrators"><DepartmentAdministrators key={department.id}
                department={department} scopeKey={scope.join(":")} owner={owner} onSaved={() => void changed()} /></TabsContent>}
              {owner && tab === "sync" && <TabsContent value="sync"><DirectorySyncPanel
                key={effectiveOrgId} organization={organization} units={units.data ?? []}
                capabilities={capabilities} onSaved={changed} /></TabsContent>}
              {owner && tab === "transfers" && <TabsContent value="transfers">
                {(transfers.error || cancelTransfer.error) && <Alert><AlertDescription>
                  {(transfers.error || cancelTransfer.error)?.message}</AlertDescription></Alert>}
                <div className="directory-table-scroll"><ResizableTable className="directory-table">
                  <thead><tr><th><span>人员</span></th><th><span>原部门</span></th><th><span>目标部门</span></th>
                    <th><span>生效月份</span></th><th><span>状态</span></th><th><span>操作</span></th></tr></thead>
                  <tbody>{(transfers.data ?? []).filter((item) => departmentId
                    ? item.source_department_id === departmentId || item.target_department_id === departmentId
                    : item.source_organization_id === effectiveOrgId || item.target_organization_id === effectiveOrgId
                  ).map((item) => <tr key={item.id}>
                    <td data-no-localize>{item.display_name}<small>{item.governance_user_id}</small></td>
                    <td data-no-localize>{item.source_department_name}</td><td data-no-localize>{item.target_department_name}</td>
                    <td data-no-localize>{item.effective_month}</td>
                    <td><Badge>{({ scheduled: "已预约", completed: "已完成", conflicted: "冲突", cancelled: "已取消" })[item.status]}</Badge>
                      {item.conflict_code && <small data-no-localize>{item.conflict_code}</small>}</td>
                    <td>{["scheduled", "conflicted"].includes(item.status) && <Button variant="outline" size="sm"
                      disabled={cancelTransfer.isPending} onClick={() => cancelTransfer.mutate(item.id)}>取消</Button>}</td>
                  </tr>)}</tbody>
                </ResizableTable></div>
                {transfers.data?.length === 0 && <Empty><EmptyTitle>暂无调岗计划</EmptyTitle></Empty>}
              </TabsContent>}
              {owner && tab === "candidates" && <TabsContent value="candidates">
                <DirectoryCandidatesPanel onSaved={changed} />
              </TabsContent>}
              <TabsContent value="audit"><div className="directory-audit-list">{(audit.data ?? []).map((event) => <div key={event.id}>
                <span>{actionNames[event.action] ?? event.action}</span><time data-no-localize>{new Intl.DateTimeFormat(getIntlLocale(), {
                  dateStyle: "medium", timeStyle: "short", timeZone: timezone,
                }).format(new Date(event.changed_at))}</time><strong data-no-localize>{event.actor_label}</strong>
                <small>{event.reason || event.source}</small>
              </div>)}</div>{audit.data?.length === 0 && <Empty><EmptyTitle>暂无变更记录</EmptyTitle></Empty>}</TabsContent>
            </Tabs>
          </>}
        </main>
      </div>}
    {editor && <DirectoryEditor key={`${editor.kind}-${editor.row?.id ?? "new"}`} editor={editor}
      organizationId={effectiveOrgId} departmentId={departmentId} units={units.data ?? []}
      onClose={() => setEditor(null)} onSaved={(id) => {
        if (editor.kind === "organization" && !editor.row) { setOrganizationId(id); setUnitId("") }
        if (editor.kind === "department" && !editor.row) setUnitId(id)
        void changed()
      }} />}
    {action && <PersonActionDialog key={`${action.person.id}-${action.action}`} {...action}
      units={units.data ?? []} onClose={() => setAction(null)} onSaved={() => void changed()} />}
  </div>
}
