import type { ModelAvailabilityReadback } from "../data-sources/apim/types"

const inFlightStatuses = new Set<ModelAvailabilityReadback["status"]>(["pending", "projected", "partial"])

export function availabilityRefetchInterval(items: readonly ModelAvailabilityReadback[] | undefined, error: unknown = null) {
  return error ? 15000 : items?.some((item) => inFlightStatuses.has(item.status)) ? 2000 : 15000
}

export function availabilityPresentation(readback: ModelAvailabilityReadback | undefined, submitting = false, registryEnabled = false, requestedEnabled?: boolean) {
  const inFlight = submitting || Boolean(readback && inFlightStatuses.has(readback.status))
  const verified = !submitting && readback?.status === "succeeded"
    && readback.effective_revision === readback.desired_revision
    && readback.effective_enabled === readback.desired_enabled
    && readback.verified_at !== null
  const targetEnabled = submitting && requestedEnabled !== undefined ? requestedEnabled : readback?.desired_enabled ?? registryEnabled
  const state = submitting ? "pending" : verified ? "succeeded" : readback?.status === "succeeded" ? "unknown" : readback?.status ?? "unknown"
  return {
    state,
    inFlight,
    verified,
    checked: readback?.desired_enabled ?? registryEnabled,
    canChange: readback?.command_ready === true && !inFlight,
    canRetry: readback?.status === "failed" && readback.operation !== null
      && readback.command_ready === true && !submitting,
    direction: targetEnabled ? "enabling" : "disabling",
  } as const
}