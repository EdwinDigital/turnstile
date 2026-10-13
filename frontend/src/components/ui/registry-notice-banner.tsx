import { Check, RefreshCw, TriangleAlert, X } from "lucide-react"

export type RegistryNotice = {
  text: string
  ok: boolean
  context?: string
  detail?: string
  pending?: boolean
  actionLabel?: string
}

export function RegistryNoticeBanner({ notice, onDismiss, onAction }: {
  notice: RegistryNotice
  onDismiss?: () => void
  onAction?: () => void
}) {
  return <div className={`${notice.pending ? "registry-progress" : notice.ok ? "registry-notice" : "registry-error"} smh-notice`} role="status" aria-live="polite">
    {notice.pending ? <RefreshCw className="spin" size={13} /> : notice.ok ? <Check size={13} /> : <TriangleAlert size={13} />}
    <div className="smh-notice-copy">
      {notice.context && <b data-no-localize>{notice.context}</b>}
      <span>{notice.text}</span>
      {notice.detail && <small>{notice.detail}</small>}
    </div>
    {notice.actionLabel && onAction && <button type="button" className="smh-notice-action" onClick={onAction}>{notice.actionLabel}</button>}
    {onDismiss && <button type="button" className="smh-notice-close" onClick={onDismiss} aria-label="关闭"><X size={13} /></button>}
  </div>
}