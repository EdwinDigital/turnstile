import type { GatewayBackendPoolWrite } from "../../data-sources/apim/types"

export function poolPublicationPayload(
  draft: GatewayBackendPoolWrite,
  affinitySupported: boolean,
): GatewayBackendPoolWrite {
  if (affinitySupported) return { ...draft, session_affinity: draft.session_affinity === true }
  if (draft.session_affinity) throw new Error("当前后端未发布会话黏着能力。")
  const { session_affinity: _affinity, ...legacy } = draft
  return legacy
}