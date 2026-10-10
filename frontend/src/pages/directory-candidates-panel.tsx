import { useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Link2, X } from "lucide-react"
import { directoryApi, type DirectoryCandidate } from "../data-sources/apim/api/organization-management"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Badge } from "../components/ui/badge"
import { Alert, AlertDescription } from "../components/ui/alert"
import { Empty, EmptyTitle } from "../components/ui/empty"
import { ResizableTable } from "../components/ui/resizable-table"
import { DirectorySelect } from "../components/directory-controls"
import { Field, FieldGroup, FieldLabel } from "../components/ui/field"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "../components/ui/dialog"

function CandidateLink({ candidate, onClose, onSaved }: {
  candidate: DirectoryCandidate; onClose: () => void; onSaved: () => Promise<void>
}) {
  const [query, setQuery] = useState(candidate.original_user_id)
  const [personId, setPersonId] = useState("")
  const people = useQuery({
    queryKey: ["directory", "observation-person", query],
    queryFn: () => directoryApi.people({ query }),
  })
  const save = useMutation({
    mutationFn: () => directoryApi.resolveIdentityConflict(candidate.id, { person_id: personId, ignore: false, reason: "" }),
    onSuccess: async () => { await onSaved(); onClose() },
  })
  return <Dialog open onOpenChange={(open) => { if (!open && !save.isPending) onClose() }}>
    <DialogContent className="directory-dialog">
      <DialogHeader><DialogTitle>关联历史人员</DialogTitle><DialogDescription data-no-localize>{candidate.original_user_id}</DialogDescription></DialogHeader>
      <FieldGroup><Field><FieldLabel htmlFor="candidate-search">搜索人员</FieldLabel>
        <Input id="candidate-search" value={query} maxLength={200} onChange={(event) => { setQuery(event.target.value); setPersonId("") }} /></Field>
        <Field><FieldLabel>本地人员</FieldLabel><DirectorySelect label="本地人员" value={personId}
          items={(people.data?.items ?? []).filter((person) => person.governance_user_id === candidate.original_user_id)
            .map((person) => ({ value: person.id, label: `${person.display_name} · ${person.governance_user_id}` }))}
          onChange={setPersonId} /></Field></FieldGroup>
      {(people.error || save.error) && <Alert><AlertDescription>{(people.error || save.error)?.message}</AlertDescription></Alert>}
      <DialogFooter><Button variant="outline" onClick={onClose} disabled={save.isPending}>取消</Button>
        <Button disabled={!personId || save.isPending} onClick={() => save.mutate()}><Link2 data-icon="inline-start" />确认关联</Button></DialogFooter>
    </DialogContent>
  </Dialog>
}

export function DirectoryCandidatesPanel({ onSaved }: { onSaved: () => Promise<void> }) {
  const [query, setQuery] = useState("")
  const [link, setLink] = useState<DirectoryCandidate | null>(null)
  const candidates = useQuery({
    queryKey: ["directory", "identity-conflicts", query],
    queryFn: () => directoryApi.identityConflicts(query),
  })
  const ignore = useMutation({
    mutationFn: (id: string) => directoryApi.resolveIdentityConflict(id, { ignore: true, reason: "" }),
    onSuccess: onSaved,
  })
  return <section>
    <div className="directory-toolbar"><Input aria-label="搜索历史身份" placeholder="搜索历史身份" value={query}
      maxLength={200} onChange={(event) => setQuery(event.target.value)} /></div>
    {(candidates.error || ignore.error) && <Alert><AlertDescription>{(candidates.error || ignore.error)?.message}</AlertDescription></Alert>}
    <div className="directory-table-scroll"><ResizableTable className="directory-table">
      <thead><tr><th><span>原始人员标识</span></th><th><span>部门</span></th><th><span>来源</span></th>
        <th><span>状态</span></th><th><span>操作</span></th></tr></thead>
      <tbody>{(candidates.data ?? []).map((candidate) => <tr key={candidate.id}>
        <td data-no-localize>{candidate.original_user_id}</td><td data-no-localize>{candidate.department_id ?? "--"}</td>
        <td data-no-localize>{candidate.source}</td><td><Badge>{candidate.status === "conflict" ? "冲突" : "待关联"}</Badge></td>
        <td><Button variant="ghost" size="icon-sm" aria-label="关联历史人员" title="关联历史人员" onClick={() => setLink(candidate)}><Link2 /></Button>
          <Button variant="ghost" size="icon-sm" aria-label="忽略历史身份" title="忽略历史身份" disabled={ignore.isPending}
            onClick={() => ignore.mutate(candidate.id)}><X /></Button></td>
      </tr>)}</tbody>
    </ResizableTable></div>
    {candidates.isPending ? <Empty><EmptyTitle>正在加载</EmptyTitle></Empty>
      : candidates.data?.length === 0 && <Empty><EmptyTitle>暂无历史身份候选</EmptyTitle></Empty>}
    {link && <CandidateLink candidate={link} onClose={() => setLink(null)} onSaved={onSaved} />}
  </section>
}
