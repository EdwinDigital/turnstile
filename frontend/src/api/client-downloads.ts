import { apiUrl, ApiError, request, SESSION_EXPIRED_EVENT } from "./client"

export type ClientSubscription = {
  application_id: string; application_name: string; subscription_id: string; subscription_name: string
  gateway_id: string; gateway_name: string; base_url: string
  models: Array<{ id: string; name: string; protocol: string; context_window: number | null; capabilities: string[] }>
}
export type ClientDownloadOptions = {
  items: ClientSubscription[]; key_management_available: boolean; unavailable_reason: string | null; plugin_version: string
}
export const clientDownloadsApi = {
  options: (signal?: AbortSignal) => request<ClientDownloadOptions>("/api/v1/client-downloads/options", { signal }),
  async download(item: ClientSubscription, keyKind: "primary" | "secondary", signal?: AbortSignal): Promise<Blob> {
    const response = await fetch(apiUrl("/api/v1/client-downloads/magpie"), {
      method: "POST", credentials: "include", cache: "no-store", signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ application_id: item.application_id, subscription_id: item.subscription_id,
        gateway_id: item.gateway_id, key_kind: keyKind }),
    })
    if (response.status === 401) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: string } | null
      throw new ApiError(response.status, typeof body?.detail === "string" ? body.detail : "下载失败，请重试。")
    }
    if (!response.headers.get("content-type")?.includes("application/zip")) throw new Error("下载失败，请重试。")
    return response.blob()
  },
}
