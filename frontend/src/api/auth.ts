import { ApiError, apiUrl } from "./client"

export type SignInMethod = "password" | "entra"

export type AuthUser = {
  id: string
  name: string | null
  email: string
  role: "owner" | "member"
  method: SignInMethod
  /** Absent only while a newly built frontend is talking to a pre-expiry-field API. */
  session_expires_at?: string
}

async function readProfile(): Promise<AuthUser | null> {
  const response = await fetch(apiUrl("/api/v1/auth/me"), { credentials: "include" })
  if (!response.ok) return null
  return (await response.json()) as AuthUser
}

async function postIdentity(path: string, body: unknown): Promise<AuthUser> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => null)) as { detail?: unknown } | null
    throw new ApiError(response.status, typeof detail?.detail === "string"
      ? detail.detail : "登录失败，请重试。")
  }
  return (await response.json()) as AuthUser
}

export const authApi = {
  profile: readProfile,
  signInWithPassword: (email: string, password: string) =>
    postIdentity("/api/v1/auth/login", { email, password }),
  exchangeEntraToken: (idToken: string) =>
    postIdentity("/api/v1/auth/entra", { id_token: idToken }),
  signOut: async () => {
    await fetch(apiUrl("/api/v1/auth/logout"), {
      method: "POST",
      credentials: "include",
    })
  },
}