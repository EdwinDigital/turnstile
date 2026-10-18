CREATE TABLE gateway_application_usage_recovery (
    correlation_id TEXT NOT NULL CHECK (length(btrim(correlation_id)) BETWEEN 1 AND 255),
    application_id UUID NOT NULL REFERENCES gateway_application(id) ON DELETE RESTRICT,
    period_start DATE NOT NULL CHECK (period_start = date_trunc('month', period_start)::date),
    occurred_at TIMESTAMPTZ NOT NULL,
    evidence_at TIMESTAMPTZ NOT NULL CHECK (evidence_at >= occurred_at),
    status_code INTEGER CHECK (status_code BETWEEN 0 AND 599),
    input_tokens BIGINT NOT NULL CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL CHECK (output_tokens >= 0),
    total_tokens BIGINT NOT NULL CHECK (
        total_tokens >= 0 AND total_tokens = input_tokens + output_tokens
    ),
    recovery_kind TEXT NOT NULL CHECK (recovery_kind IN ('exact_usage', 'terminal_zero')),
    source TEXT NOT NULL CHECK (source IN ('apim_gateway_llm_log', 'apim_gateway_log')),
    recovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, correlation_id),
    CHECK (
        (recovery_kind = 'exact_usage' AND source = 'apim_gateway_llm_log')
        OR (recovery_kind = 'terminal_zero' AND total_tokens = 0 AND status_code >= 400)
    )
);

CREATE INDEX gateway_application_usage_recovery_application_idx
    ON gateway_application_usage_recovery (
        application_id, period_start, occurred_at DESC, correlation_id
    );

CREATE FUNCTION reject_gateway_application_usage_recovery_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'gateway_application_usage_recovery rows are immutable';
END;
$$;

CREATE TRIGGER gateway_application_usage_recovery_immutable
BEFORE UPDATE OR DELETE ON gateway_application_usage_recovery
FOR EACH ROW EXECUTE FUNCTION reject_gateway_application_usage_recovery_mutation();

CREATE TABLE gateway_application_ledger_finalization_state (
    period_start DATE NOT NULL CHECK (period_start = date_trunc('month', period_start)::date),
    application_id UUID NOT NULL REFERENCES gateway_application(id) ON DELETE RESTRICT,
    finalized_upper_bound_tokens BIGINT NOT NULL CHECK (finalized_upper_bound_tokens >= 0),
    finalized_upper_bound_count INTEGER NOT NULL CHECK (finalized_upper_bound_count >= 0),
    snapshot_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (period_start, application_id)
);

CREATE TABLE gateway_deployment_provisioning (
    deployment_id UUID PRIMARY KEY REFERENCES gateway_deployment(id) ON DELETE RESTRICT,
    apim_target_id UUID NOT NULL REFERENCES gateway_profile(id) ON DELETE RESTRICT,
    operation_id UUID NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (operation_id, apim_target_id)
        REFERENCES gateway_release_operation(id, gateway_profile_id) ON DELETE RESTRICT,
    FOREIGN KEY (deployment_id, apim_target_id)
        REFERENCES gateway_deployment(id, apim_target_id) ON DELETE RESTRICT
);

CREATE OR REPLACE VIEW budget_reservation_finalization_effective AS
WITH candidates AS (
    SELECT id, scope_type, scope_id, period_start, correlation_id,
        reservation_created_at, reservation_tokens, evidence_at,
        status_code, input_tokens, output_tokens, total_tokens,
        finalization_kind, source, finalized_at
    FROM budget_reservation_finalization
    UNION ALL
    SELECT 0::BIGINT AS id, 'application'::TEXT AS scope_type,
        recovery.application_id::TEXT AS scope_id,
        recovery.period_start, recovery.correlation_id,
        recovery.occurred_at AS reservation_created_at,
        recovery.total_tokens AS reservation_tokens,
        recovery.evidence_at, recovery.status_code,
        recovery.input_tokens, recovery.output_tokens, recovery.total_tokens,
        recovery.recovery_kind AS finalization_kind,
        recovery.source, recovery.recovered_at AS finalized_at
    FROM gateway_application_usage_recovery recovery
)
SELECT DISTINCT ON (scope_type, scope_id, correlation_id)
    scope_type, scope_id, period_start, correlation_id,
    reservation_created_at, reservation_tokens, evidence_at,
    status_code, input_tokens, output_tokens, total_tokens,
    finalization_kind, source, finalized_at
FROM candidates
ORDER BY scope_type, scope_id, correlation_id,
    CASE finalization_kind
        WHEN 'exact_usage' THEN 3
        WHEN 'terminal_zero' THEN 2
        ELSE 1
    END DESC,
    evidence_at DESC,
    id DESC;

CREATE OR REPLACE VIEW invocation_request_trace AS
SELECT usage.id, usage.request_id, usage.correlation_id, usage.ts,
    usage.organization_id, usage.organization, usage.department_id, usage.department,
    usage.project_id, usage.project, usage.agent_id, usage.agent,
    usage.user_id, usage.user_ref, usage.model_id, usage.model, usage.request_source,
    usage.runtime, usage.provider, usage.workflow, usage.run_id, usage.turn_index,
    usage.input_tokens, usage.cached_tokens, usage.cache_write_tokens, usage.output_tokens,
    COALESCE((attempt.completion->>'latency_ms')::BIGINT, usage.latency_ms) AS latency_ms,
    COALESCE((attempt.completion->>'status_code')::INTEGER, usage.status_code) AS status_code,
    usage.estimated_cost, usage.input_price_per_million, usage.cached_price_per_million,
    usage.cache_write_price_per_million, usage.output_price_per_million,
    usage.price_context_tier, usage.price_context_snapshot,
    CASE WHEN attempt.completion->>'outcome' = 'failed'
        THEN 'Platform invocation ' || (attempt.completion->>'failure_kind')
        ELSE usage.error_message END AS error_message,
    usage.estimated, usage.ingest_source, usage.ingest_error, usage.reconciled_at,
    usage.budget_admission, usage.model_admission, usage.usage_domain,
    gateway.apim_target_id, gateway.gateway_deployment_id, gateway.native_gateway_id,
    COALESCE('platform:' || attempt.request_id, usage.id) AS trace_id,
    (NOT usage.estimated OR usage.input_tokens + usage.cached_tokens + usage.output_tokens > 0)
        AS usage_available,
    CASE WHEN attempt.request_id IS NOT NULL
        THEN COALESCE(attempt.completion->>'outcome', 'dispatched') END AS platform_outcome,
    usage.status_code AS gateway_status_code,
    attempt.completion->>'upstream_request_id' AS upstream_request_id,
    gateway.native_instance_id, gateway.is_managed, gateway.apim_region, gateway.api_revision
FROM token_usage usage
LEFT JOIN token_usage_gateway_attribution gateway ON gateway.usage_id = usage.id
LEFT JOIN platform_invocation_usage_match matched ON matched.usage_id = usage.id
LEFT JOIN platform_invocation_attempt attempt ON attempt.request_id = matched.request_id
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
    attempt.snapshot->'metadata'->>'run_id', (attempt.snapshot->'metadata'->>'turn_index')::INTEGER,
    0::BIGINT, 0::BIGINT, 0::BIGINT, 0::BIGINT,
    COALESCE((attempt.completion->>'latency_ms')::BIGINT, 0),
    (attempt.completion->>'status_code')::INTEGER,
    0::NUMERIC, NULL::NUMERIC, NULL::NUMERIC, NULL::NUMERIC, NULL::NUMERIC,
    NULL::TEXT, NULL::JSONB,
    CASE WHEN attempt.completion->>'outcome' = 'failed'
        THEN 'Platform invocation ' || (attempt.completion->>'failure_kind') END,
    TRUE, 'platform', 'platform_usage_unavailable', NULL::TIMESTAMPTZ,
    NULL::TEXT, NULL::TEXT, 'apim',
    (attempt.snapshot->>'apim_target_id')::UUID,
    (attempt.snapshot->>'gateway_deployment_id')::UUID,
    attempt.snapshot->>'native_gateway_id',
    'platform:' || attempt.request_id, FALSE,
    COALESCE(attempt.completion->>'outcome', 'dispatched'), NULL::INTEGER,
    attempt.completion->>'upstream_request_id', NULL::TEXT,
    CASE WHEN attempt.snapshot->>'native_gateway_id' IS NOT NULL
        THEN attempt.snapshot->>'native_gateway_id' = 'managed' END,
    NULL::TEXT, NULL::TEXT
FROM platform_invocation_attempt attempt
WHERE NOT EXISTS (
    SELECT 1 FROM platform_invocation_usage_match matched
    WHERE matched.request_id = attempt.request_id
);