import { useEffect, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ShieldCheck } from "lucide-react"
import { request } from "../api/client"
import { type ClientSubscription } from "../api/client-downloads"
import { Button } from "../components/ui/button"
import { useAuth } from "../providers/auth-provider"

type Authorization = {
  user_name: string; purpose: "login" | "import" | "reauth"; state: string
  expires_at: string; reauth_required: boolean; subscriptions: ClientSubscription[]
}

export function ClientAuthorizationPage({ code }: { code: string }) {
  const { user, signOut } = useAuth()
  const query = useQuery({
    queryKey: ["client-authorization", code, user?.id],
    queryFn: ({ signal }) => request<Authorization>(`/api/v1/client-auth/verification?user_code=${encodeURIComponent(code)}`, { signal }),
    retry: false, staleTime: 0,
  })
  const [choice, setChoice] = useState("")
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState("")
  const [error, setError] = useState("")
  const active = useRef<AbortController | null>(null)
  useEffect(() => () => active.current?.abort(), [])
  const items = query.data?.subscriptions ?? []
  const selected = items.find((x) => x.subscription_id === choice) ?? items[0]
  const importing = query.data?.purpose === "import"
  async function decide(approve: boolean) {
    if (active.current) return
    const controller = new AbortController(); active.current = controller
    setBusy(true); setError("")
    try {
      await request("/api/v1/client-auth/decision", {
        method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_code: code, approve, ...(approve && importing && selected ? {
          application_id: selected.application_id, subscription_id: selected.subscription_id, gateway_id: selected.gateway_id,
        } : {}) }),
      })
      if (controller.signal.aborted) return
      sessionStorage.removeItem("turnstile-client-authorization")
      setResult(approve ? "验证已完成，请回到 magpie。插件将自动接收结果。" : "已拒绝授权，原有有效配置不变。")
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "验证失败，请重试。")
    } finally { active.current = null; if (!controller.signal.aborted) setBusy(false) }
  }
  return <div className="client-authorization-shell"><main className="client-downloads-card client-authorization-card">
    <div className="client-downloads-heading"><ShieldCheck size={24} /><div><h1>AIGateway 用户版验证</h1><p>只确认你本人刚从 magpie 发起的请求。</p></div></div>
    <p>设备确认码：<strong className="client-authorization-code" data-no-localize>{code}</strong></p>
    {query.isPending ? <p role="status">正在读取授权申请…</p> : query.isError ? <div role="alert"><p>{query.error.message}</p><Button variant="outline" onClick={() => void query.refetch()}>重试</Button></div> : result ? <div><p role="status">{result}</p><a href="/?page=client-downloads">返回下载客户端</a></div> : <>
      <p>授权用户：<strong data-no-localize>{query.data?.user_name}</strong></p>
      <p>允许插件读取本人可访问的订阅、模型和额度，并通过所选订阅调用模型。不会把 APIM 密钥交给插件。</p>
      {query.data?.state !== "pending" ? <p role="status">这次申请已处理，请回到 magpie 或重新发起验证。</p> : query.data?.reauth_required ? <div><p role="status">这次操作需要重新登录平台，不能仅使用已有会话确认。</p><Button onClick={() => void signOut()}>重新验证平台登录</Button></div> : <form action="/api/v1/client-auth/decision" method="post" onSubmit={(event) => { event.preventDefault(); void decide(true) }}>
        {importing && (items.length ? <>
          <div className="client-downloads-fields client-authorization-fields">
            <label htmlFor="client-import-subscription">可访问的应用订阅<select id="client-import-subscription" name="subscription_id" required value={selected?.subscription_id ?? ""} disabled={busy} onChange={(event) => setChoice(event.target.value)}>{items.map((x) => <option key={x.subscription_id} value={x.subscription_id} data-no-localize>{x.application_name} / {x.subscription_name}</option>)}</select></label>
            <label htmlFor="client-import-gateway">平台网关<select id="client-import-gateway" value={selected?.gateway_id ?? ""} disabled onChange={() => {}}><option data-no-localize value={selected?.gateway_id}>{selected?.gateway_name}</option></select></label>
          </div>
          <h2>本次导入模型</h2>
          <ul className="client-downloads-models">{selected?.models.map((m) => <li key={m.id}><span data-no-localize>{m.name}</span><small data-no-localize>{m.protocol}</small></li>)}</ul>
          <p>确认后完整替换该用户版插件的旧订阅与模型目录；订阅版和其他供应商不受影响。</p>
        </> : <p role="status">没有本人可访问的有效订阅，请联系平台管理员。</p>)}
        {error && <p role="alert" className="client-downloads-error">{error}</p>}
        <div className="client-authorization-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => void decide(false)}>拒绝</Button><Button type="submit" disabled={busy || (importing && !selected?.models.length)}>{busy ? "正在确认…" : importing ? "确认导入模型配置" : "确认授权"}</Button></div>
      </form>}
    </>}
  </main></div>
}
