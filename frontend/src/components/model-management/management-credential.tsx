import { useId, useRef, useState } from "react"
import { KeyRound } from "lucide-react"

import { Button } from "../ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"

export function ManagementCredentialField({ value, disabled, onChange }: {
  value: string
  disabled: boolean
  onChange: (value: string) => void
}) {
  const identity = useId()
  return <label className="registry-field gateway-field-wide" htmlFor={`${identity}-management-credential`}>
    <span className="registry-field-label">Management credential</span>
    <Input id={`${identity}-management-credential`} name="management_credential" type="password"
      value={value} disabled={disabled} autoComplete="off" spellCheck={false}
      onChange={event => onChange(event.target.value)} />
  </label>
}

export function ManagementCredentialDialog({ title, targetName, submitLabel, busy, available, error, onClose, onConfirm }: {
  title: string
  targetName: string
  submitLabel: string
  busy: boolean
  available: boolean
  error: string | null
  onClose: () => void
  onConfirm: (credential: string) => Promise<unknown>
}) {
  const [credential, setCredential] = useState("")
  const submitting = useRef(false)
  const close = () => {
    if (busy || submitting.current) return
    setCredential("")
    onClose()
  }
  return <Dialog open onOpenChange={open => { if (!open) close() }}>
    <DialogContent className="registry-editor-dialog gateway-lifecycle-dialog">
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy || submitting.current || !available) return
        submitting.current = true
        try { await onConfirm(credential) }
        catch {}
        finally { setCredential(""); submitting.current = false }
      }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>{title}</DialogTitle><DialogDescription>{targetName}</DialogDescription></DialogHeader>
        <div className="registry-editor-body"><ManagementCredentialField value={credential} disabled={busy} onChange={setCredential} /></div>
        {error && <div className="registry-error gateway-registration-error" role="alert">{error}</div>}
        <DialogFooter className="registry-editor-footer gateway-lifecycle-footer">
          <Button type="button" variant="outline" disabled={busy} onClick={close}>Cancel</Button>
          <Button type="submit" disabled={busy || !available}><KeyRound size={14} />{submitLabel}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}