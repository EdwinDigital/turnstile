import type { ManagedModel, UsageRequestDetail, UsageRequestSummary } from "../data-sources/apim/types"

type PricedModel = Pick<
  ManagedModel,
  | "id"
  | "model_key"
  | "display_name"
  | "input_cost_per_million"
  | "output_cost_per_million"
  | "cached_cost_per_million"
  | "context_price_tiers"
>

export function hasModelPrices(model: PricedModel): boolean {
  return (model.context_price_tiers?.length ?? 0) >= 2
    || (model.input_cost_per_million != null && model.output_cost_per_million != null)
}

// Telemetry identifies a model by whichever space the caller used: the registry UUID from the
// dashboard BFF, the model key from a desktop client, or the canonical display name written by
// migration 013. Resolving all three keeps one model from being priced in one place and skipped
// in another.
function findPricedModel(models: PricedModel[], key: string, label: string) {
  return models.find((item) => item.id === key)
    ?? models.find((item) => item.model_key === key)
    ?? models.find((item) => item.model_key === label)
    ?? models.find((item) => item.display_name === label)
}

/**
 * Prices one aggregate bucket returned by the trends API. Buckets carry token counts without a
 * stored per-row price snapshot, so this is a registry-price estimate over the complete data set
 * rather than the settled cost the executive KPI reports.
 */
export function estimateBucketCost(
  bucket: { key: string; label: string; input: number; cached: number; output: number },
  models: PricedModel[],
): number | null {
  const model = findPricedModel(models, bucket.key, bucket.label)
  if (model?.context_price_tiers?.length) return null
  if (model?.input_cost_per_million == null || model.output_cost_per_million == null) return null
  const cachedRate = model.cached_cost_per_million ?? model.input_cost_per_million
  return (
    bucket.input * model.input_cost_per_million
    + bucket.cached * cachedRate
    + bucket.output * model.output_cost_per_million
  ) / 1_000_000
}

export function estimateRequestCost(
  request: UsageRequestSummary,
  models: PricedModel[],
): number | null {
  if (request.usage_available === false) return null
  if (request.estimated_cost > 0) return request.estimated_cost
  if (request.cost_available === true) return request.estimated_cost
  if (request.cost_available === false) return null
  const model = models.find((item) => item.id === request.model_id)
    ?? models.find((item) => item.model_key === request.model_name)
  if (model?.context_price_tiers?.length) return null
  if (model?.input_cost_per_million == null || model.output_cost_per_million == null) return null
  const cachedTokens = Math.max(0, request.total_tokens - request.prompt_tokens - request.completion_tokens)
  // Providers bill cached input far below the base rate. Without a configured rate the input rate
  // is kept as the conservative fallback rather than silently treating cached tokens as free.
  const cachedRate = model.cached_cost_per_million ?? model.input_cost_per_million
  return (
    request.prompt_tokens * model.input_cost_per_million
    + cachedTokens * cachedRate
    + request.completion_tokens * model.output_cost_per_million
  ) / 1_000_000
}

export function priceRequests<T extends UsageRequestSummary>(
  requests: T[],
  models: PricedModel[],
): T[] {
  return requests.map((request) => {
    const estimatedCost = estimateRequestCost(request, models)
    return estimatedCost == null ? request : { ...request, estimated_cost: estimatedCost }
  })
}

export function requestCostCalculation(request: UsageRequestDetail) {
  if (request.usage_available === false || request.cost_available === false || (request.estimated && !request.reconciled_at)) return null
  const inputRate = request.input_price_per_million
  const outputRate = request.output_price_per_million
  if (inputRate == null || outputRate == null) return null
  const cachedRate = request.cached_price_per_million ?? inputRate
  const writeRate = request.cache_write_price_per_million ?? cachedRate
  const writes = request.cache_write_tokens ?? 0
  const counts = [request.prompt_tokens, request.cached_tokens, writes, request.completion_tokens]
  if (counts.some(value => !Number.isSafeInteger(value) || value < 0)
    || writes > request.cached_tokens
    || [inputRate, cachedRate, writeRate, outputRate, request.estimated_cost]
      .some(value => !Number.isFinite(value) || value < 0)) return null
  const lowerBound = request.ingest_error === "stream_cache_usage_unavailable"
  const terms = [
    { bucket: "input", tokens: request.prompt_tokens, rate: inputRate },
    ...lowerBound ? [] : [
      { bucket: "cached", tokens: request.cached_tokens - writes, rate: cachedRate },
      { bucket: "write", tokens: writes, rate: writeRate },
    ],
    { bucket: "output", tokens: request.completion_tokens, rate: outputRate },
  ]
  const inputTokens = request.prompt_tokens + request.cached_tokens
  const tiers = request.price_context_snapshot?.context_tiers ?? []
  const contextTier = tiers.find(tier => tier.key === request.price_context_tier) ?? null
  if (tiers.length && (!contextTier || lowerBound
    || inputTokens < contextTier.min_input_tokens
    || (contextTier.max_input_tokens != null && inputTokens > contextTier.max_input_tokens))) return null
  const calculatedCost = Number((terms.reduce((sum, term) => sum + term.tokens * term.rate, 0) / 1_000_000).toFixed(8))
  return {
    terms, contextTier, inputTokens, calculatedCost, lowerBound,
    recordedCost: request.estimated_cost,
    matchesRecordedCost: Math.abs(calculatedCost - request.estimated_cost) < 0.000000005,
  }
}
