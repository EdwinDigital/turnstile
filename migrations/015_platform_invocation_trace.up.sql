CREATE TABLE public.platform_invocation_attempt (
    request_id TEXT PRIMARY KEY CHECK (length(request_id) BETWEEN 1 AND 512),
    started_at TIMESTAMPTZ NOT NULL,
    snapshot JSONB NOT NULL,
    completion JSONB,
    CHECK ((jsonb_typeof(snapshot) = 'object'
        AND snapshot->>'schema_version' = '1'
        AND snapshot->>'request_id' = request_id
        AND jsonb_typeof(snapshot->'metadata') = 'object') IS TRUE),
    CHECK (completion IS NULL OR (
        jsonb_typeof(completion) = 'object'
        AND completion->>'request_id' = request_id
        AND completion->>'outcome' IN ('succeeded', 'failed')
        AND (completion->>'status_code')::integer BETWEEN 100 AND 599
        AND (completion->>'latency_ms')::bigint >= 0
        AND (
            (completion->>'outcome' = 'failed' AND completion->>'failure_kind' IN (
                'gateway_error', 'http_error', 'timeout', 'transport_error',
                'invalid_response', 'internal_error'
            ))
            OR (completion->>'outcome' = 'succeeded'
                AND completion->>'failure_kind' IS NULL
                AND (completion->>'status_code')::integer BETWEEN 200 AND 299)
        )
    ) IS TRUE)
);

CREATE INDEX platform_invocation_attempt_started_at_idx
    ON public.platform_invocation_attempt (started_at DESC);

CREATE VIEW public.platform_invocation_usage_match AS
SELECT attempt.request_id, matched.usage_id
FROM public.platform_invocation_attempt attempt
JOIN LATERAL (
    SELECT min(usage.id) AS usage_id
    FROM public.token_usage usage
    LEFT JOIN public.token_usage_application_attribution application ON application.usage_id = usage.id
    WHERE usage.request_id = attempt.request_id
      AND usage.correlation_id = attempt.completion->>'gateway_correlation_id'
      AND usage.usage_domain = 'apim'
      AND usage.user_id = attempt.snapshot->'metadata'->>'user_id'
      AND usage.model_id = attempt.snapshot->>'model_id'
      AND usage.runtime = attempt.snapshot->>'runtime_name'
      AND (attempt.snapshot->>'application_id' IS NULL
           OR application.application_id::text = attempt.snapshot->>'application_id')
    HAVING count(*) = 1
) matched ON TRUE;

CREATE VIEW public.invocation_request_trace AS
SELECT usage.id, usage.request_id, usage.correlation_id, usage.ts,
       usage.organization_id, usage.organization, usage.department_id, usage.department,
       usage.project_id, usage.project, usage.agent_id, usage.agent,
       usage.user_id, usage.user_ref, usage.model_id, usage.model, usage.request_source,
       usage.runtime, usage.provider, usage.workflow, usage.run_id, usage.turn_index,
       usage.input_tokens, usage.cached_tokens, usage.cache_write_tokens, usage.output_tokens,
       COALESCE((attempt.completion->>'latency_ms')::bigint, usage.latency_ms) AS latency_ms,
       COALESCE((attempt.completion->>'status_code')::integer, usage.status_code) AS status_code,
       usage.estimated_cost, usage.input_price_per_million, usage.cached_price_per_million,
       usage.cache_write_price_per_million, usage.output_price_per_million,
       usage.price_context_tier, usage.price_context_snapshot,
       CASE WHEN attempt.completion->>'outcome' = 'failed'
            THEN 'Platform invocation ' || (attempt.completion->>'failure_kind')
            ELSE usage.error_message END AS error_message,
       usage.estimated, usage.ingest_source, usage.ingest_error, usage.reconciled_at,
       usage.budget_admission, usage.model_admission, usage.usage_domain,
       (attempt.snapshot->>'apim_target_id')::uuid AS apim_target_id,
       (attempt.snapshot->>'gateway_deployment_id')::uuid AS gateway_deployment_id,
       attempt.snapshot->>'native_gateway_id' AS native_gateway_id,
       COALESCE('platform:' || attempt.request_id, usage.id) AS trace_id,
       (NOT usage.estimated OR usage.input_tokens + usage.cached_tokens + usage.output_tokens > 0)
           AS usage_available,
       CASE WHEN attempt.request_id IS NOT NULL
            THEN COALESCE(attempt.completion->>'outcome', 'dispatched') END AS platform_outcome,
       usage.status_code AS gateway_status_code,
       attempt.completion->>'upstream_request_id' AS upstream_request_id
FROM public.token_usage usage
LEFT JOIN public.platform_invocation_usage_match matched ON matched.usage_id = usage.id
LEFT JOIN public.platform_invocation_attempt attempt ON attempt.request_id = matched.request_id
UNION ALL
SELECT 'platform:' || attempt.request_id, attempt.request_id,
       attempt.completion->>'gateway_correlation_id', attempt.started_at,
       attempt.snapshot->'metadata'->>'organization_id', attempt.snapshot->'metadata'->>'organization',
       attempt.snapshot->'metadata'->>'department_id', attempt.snapshot->'metadata'->>'department',
       attempt.snapshot->'metadata'->>'project_id', attempt.snapshot->'metadata'->>'project',
       attempt.snapshot->'metadata'->>'agent_id', attempt.snapshot->'metadata'->>'agent',
       attempt.snapshot->'metadata'->>'user_id', attempt.snapshot->'metadata'->>'user',
       attempt.snapshot->>'model_id', attempt.snapshot->>'model_name',
       attempt.snapshot->'metadata'->>'request_source', attempt.snapshot->>'runtime_name',
       attempt.snapshot->>'provider_name', attempt.snapshot->'metadata'->>'workflow',
       attempt.snapshot->'metadata'->>'run_id', (attempt.snapshot->'metadata'->>'turn_index')::integer,
       0::bigint, 0::bigint, 0::bigint, 0::bigint,
       COALESCE((attempt.completion->>'latency_ms')::bigint, 0),
       (attempt.completion->>'status_code')::integer,
       0::numeric, NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric,
       NULL::text, NULL::jsonb,
       CASE WHEN attempt.completion->>'outcome' = 'failed'
            THEN 'Platform invocation ' || (attempt.completion->>'failure_kind') END,
       TRUE, 'platform', 'platform_usage_unavailable', NULL::timestamptz,
       NULL::text, NULL::text, 'apim',
       (attempt.snapshot->>'apim_target_id')::uuid,
       (attempt.snapshot->>'gateway_deployment_id')::uuid,
       attempt.snapshot->>'native_gateway_id',
       'platform:' || attempt.request_id, FALSE,
       COALESCE(attempt.completion->>'outcome', 'dispatched'), NULL::integer,
       attempt.completion->>'upstream_request_id'
FROM public.platform_invocation_attempt attempt
WHERE NOT EXISTS (
    SELECT 1 FROM public.platform_invocation_usage_match matched
    WHERE matched.request_id = attempt.request_id
);