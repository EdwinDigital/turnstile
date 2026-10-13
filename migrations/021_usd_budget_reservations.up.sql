ALTER TABLE token_budget ALTER COLUMN token_limit DROP NOT NULL;

ALTER TABLE token_budget
    ADD COLUMN budget_mode TEXT NOT NULL DEFAULT 'tokens'
        CHECK (budget_mode IN ('tokens', 'usd')),
    ADD COLUMN usd_limit NUMERIC(30,14) CHECK (usd_limit > 0),
    ADD CONSTRAINT token_budget_mode_limit_check CHECK (
        (budget_mode = 'tokens' AND token_limit IS NOT NULL AND usd_limit IS NULL)
        OR (budget_mode = 'usd' AND token_limit IS NULL AND usd_limit IS NOT NULL)
    );

ALTER TABLE token_budget_audit
    ADD COLUMN previous_budget_mode TEXT CHECK (previous_budget_mode IN ('tokens', 'usd')),
    ADD COLUMN new_budget_mode TEXT CHECK (new_budget_mode IN ('tokens', 'usd')),
    ADD COLUMN previous_usd_limit NUMERIC(30,14) CHECK (previous_usd_limit > 0),
    ADD COLUMN new_usd_limit NUMERIC(30,14) CHECK (new_usd_limit > 0);

ALTER TABLE gateway_application_budget ALTER COLUMN token_limit DROP NOT NULL;

ALTER TABLE gateway_application_budget
    ADD COLUMN budget_mode TEXT NOT NULL DEFAULT 'tokens'
        CHECK (budget_mode IN ('tokens', 'usd')),
    ADD COLUMN usd_limit NUMERIC(30,14) CHECK (usd_limit > 0),
    ADD CONSTRAINT gateway_application_budget_mode_limit_check CHECK (
        (budget_mode = 'tokens' AND token_limit IS NOT NULL AND usd_limit IS NULL)
        OR (budget_mode = 'usd' AND token_limit IS NULL AND usd_limit IS NOT NULL)
    );

ALTER TABLE gateway_application_ledger_state
    ALTER COLUMN token_limit DROP NOT NULL;

DO $$
DECLARE available_constraint_name TEXT;
BEGIN
    SELECT ledger_constraint.conname INTO STRICT available_constraint_name
    FROM pg_constraint ledger_constraint
    JOIN pg_attribute ledger_column
        ON ledger_column.attrelid = ledger_constraint.conrelid
    WHERE ledger_constraint.conrelid = 'gateway_application_ledger_state'::regclass
        AND ledger_constraint.contype = 'c'
        AND ledger_column.attname = 'available_tokens'
        AND ledger_column.attnum = ANY(ledger_constraint.conkey);
    EXECUTE format(
        'ALTER TABLE gateway_application_ledger_state DROP CONSTRAINT %I',
        available_constraint_name
    );
END
$$;

ALTER TABLE gateway_application_ledger_state
    ADD COLUMN budget_mode TEXT NOT NULL DEFAULT 'tokens'
        CHECK (budget_mode IN ('tokens', 'usd')),
    ADD COLUMN usd_limit NUMERIC(30,14) CHECK (usd_limit > 0),
    ADD COLUMN confirmed_usd NUMERIC(30,14) NOT NULL DEFAULT 0 CHECK (confirmed_usd >= 0),
    ADD COLUMN pending_reserved_usd NUMERIC(30,14) NOT NULL DEFAULT 0
        CHECK (pending_reserved_usd >= 0),
    ADD COLUMN upper_bound_usd NUMERIC(30,14) NOT NULL DEFAULT 0 CHECK (upper_bound_usd >= 0),
    ADD CONSTRAINT gateway_application_ledger_mode_limit_check CHECK (
        (budget_mode = 'tokens' AND token_limit IS NOT NULL AND usd_limit IS NULL
            AND available_tokens = GREATEST(
                token_limit - confirmed_tokens - pending_reserved_tokens
                - finalized_upper_bound_tokens, 0
            ))
        OR (budget_mode = 'usd' AND token_limit IS NULL AND usd_limit IS NOT NULL
            AND available_tokens = 0)
    );

ALTER TABLE budget_execution_scope
    ADD COLUMN budget_mode TEXT NOT NULL DEFAULT 'tokens'
        CHECK (budget_mode IN ('tokens', 'usd')),
    ADD COLUMN seed_confirmed_usd NUMERIC(30,14) CHECK (seed_confirmed_usd >= 0),
    ADD COLUMN desired_usd_limit NUMERIC(30,14) CHECK (desired_usd_limit > 0),
    ADD COLUMN confirmed_usd NUMERIC(30,14) NOT NULL DEFAULT 0 CHECK (confirmed_usd >= 0),
    ADD COLUMN active_reserved_usd NUMERIC(30,14) NOT NULL DEFAULT 0
        CHECK (active_reserved_usd >= 0),
    ADD COLUMN upper_bound_usd NUMERIC(30,14) NOT NULL DEFAULT 0 CHECK (upper_bound_usd >= 0),
    ADD CONSTRAINT budget_execution_scope_mode_limit_check CHECK (
        (budget_mode = 'tokens' AND desired_usd_limit IS NULL)
        OR (budget_mode = 'usd' AND desired_usd_limit IS NOT NULL)
    );

ALTER TABLE budget_execution_attempt
    ADD COLUMN budget_mode TEXT NOT NULL DEFAULT 'tokens'
        CHECK (budget_mode IN ('tokens', 'usd')),
    ADD COLUMN reserved_usd NUMERIC(30,14) CHECK (reserved_usd >= 0),
    ADD COLUMN actual_usd NUMERIC(30,14) CHECK (actual_usd >= 0),
    ADD COLUMN price_snapshot JSONB CHECK (
        price_snapshot IS NULL OR jsonb_typeof(price_snapshot) = 'object'
    ),
    ADD CONSTRAINT budget_execution_attempt_mode_evidence_check CHECK (
        (budget_mode = 'tokens' AND reserved_usd IS NULL AND actual_usd IS NULL
            AND price_snapshot IS NULL)
        OR (budget_mode = 'usd' AND reserved_usd IS NOT NULL AND price_snapshot IS NOT NULL)
    );

ALTER TABLE budget_execution_evidence
    ADD COLUMN actual_usd NUMERIC(30,14) CHECK (actual_usd >= 0),
    ADD COLUMN price_snapshot JSONB CHECK (
        price_snapshot IS NULL OR jsonb_typeof(price_snapshot) = 'object'
    );

ALTER TABLE budget_settlement_task
    ADD COLUMN actual_usd NUMERIC(30,14) CHECK (actual_usd >= 0),
    ADD COLUMN price_snapshot JSONB CHECK (
        price_snapshot IS NULL OR jsonb_typeof(price_snapshot) = 'object'
    );

ALTER TABLE token_usage
    ADD COLUMN usd_cost NUMERIC(30,14) CHECK (usd_cost >= 0),
    ADD COLUMN usd_price_sha256 TEXT CHECK (
        usd_price_sha256 IS NULL OR usd_price_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN usd_price_updated_at TIMESTAMPTZ;

CREATE FUNCTION budget_usage_usd_cost(
    stored_cost NUMERIC,
    is_estimated BOOLEAN,
    input_tokens BIGINT,
    cached_tokens BIGINT,
    cache_write_tokens BIGINT,
    output_tokens BIGINT,
    input_rate NUMERIC,
    cache_read_rate NUMERIC,
    cache_write_rate NUMERIC,
    output_rate NUMERIC
)
RETURNS NUMERIC(30,14) LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN NOT is_estimated AND input_rate IS NOT NULL AND cache_read_rate IS NOT NULL
          AND cache_write_rate IS NOT NULL AND output_rate IS NOT NULL
        THEN COALESCE(stored_cost, ROUND((
            input_tokens * input_rate
            + GREATEST(cached_tokens - cache_write_tokens, 0) * cache_read_rate
            + cache_write_tokens * cache_write_rate
            + output_tokens * output_rate
        ) / 1000000, 14)::NUMERIC(30,14))
        ELSE NULL
    END
$$;

CREATE FUNCTION budget_scope_confirmed_usd(kind TEXT, identity TEXT, month DATE)
RETURNS NUMERIC(30,14) LANGUAGE sql STABLE AS $$
    WITH usage_rows AS (
        SELECT budget_usage_usd_cost(
            usd_cost, estimated, input_tokens, cached_tokens, cache_write_tokens,
            output_tokens, input_price_per_million, cached_price_per_million,
            cache_write_price_per_million, output_price_per_million
        ) AS usd_cost
        FROM token_usage
        WHERE kind = 'person' AND user_id = identity AND usage_domain = 'apim'
            AND ts >= month::timestamp AT TIME ZONE 'UTC'
            AND ts < (month + INTERVAL '1 month')::timestamp AT TIME ZONE 'UTC'
        UNION ALL
        SELECT budget_usage_usd_cost(
            usage.usd_cost, usage.estimated, usage.input_tokens, usage.cached_tokens,
            usage.cache_write_tokens, usage.output_tokens, usage.input_price_per_million,
            usage.cached_price_per_million, usage.cache_write_price_per_million,
            usage.output_price_per_million
        ) AS usd_cost
        FROM token_usage usage
        JOIN token_usage_application_attribution attribution ON attribution.usage_id = usage.id
        WHERE kind = 'application' AND attribution.application_id::text = identity
            AND usage.usage_domain = 'apim'
            AND usage.ts >= month::timestamp AT TIME ZONE 'UTC'
            AND usage.ts < (month + INTERVAL '1 month')::timestamp AT TIME ZONE 'UTC'
    )
    SELECT CASE
        WHEN count(*) FILTER (WHERE usd_cost IS NULL) > 0 THEN NULL
        ELSE COALESCE(sum(usd_cost), 0)::NUMERIC(30,14)
    END
    FROM usage_rows
$$;

CREATE OR REPLACE FUNCTION queue_budget_execution_policy(kind TEXT, identity TEXT, month DATE)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE amount BIGINT; usd_amount NUMERIC(30,14); selected_mode TEXT;
        blocking BOOLEAN; available BOOLEAN;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'execution-policy:' || kind || ':' || identity || ':' || month::text, 0));
    IF kind = 'person' THEN
        SELECT budget.budget_mode, budget.token_limit, budget.usd_limit,
               COALESCE(enforcement.mode, 'audit') = 'block', TRUE
        INTO selected_mode, amount, usd_amount, blocking, available
        FROM token_budget budget
        LEFT JOIN department_enforcement enforcement ON enforcement.department_id = budget.parent_scope_id
        WHERE budget.scope_type = 'user' AND budget.scope_id = identity AND budget.period_start = month;
        IF NOT FOUND THEN
            selected_mode := 'tokens'; amount := 0; usd_amount := NULL;
            blocking := FALSE; available := TRUE;
        END IF;
    ELSE
        SELECT budget.budget_mode, budget.token_limit, budget.usd_limit,
               budget.enforce, application.status = 'active'
        INTO selected_mode, amount, usd_amount, blocking, available
        FROM gateway_application_budget budget
        JOIN gateway_application application ON application.id = budget.application_id
        WHERE budget.application_id::text = identity AND budget.period_start = month;
    END IF;
    UPDATE budget_execution_scope SET budget_mode = COALESCE(selected_mode, 'tokens'),
        desired_limit = COALESCE(amount, 0), desired_usd_limit = usd_amount,
        desired_enforce = COALESCE(blocking, TRUE), desired_enabled = COALESCE(available, FALSE),
        policy_revision = policy_revision + 1, policy_updated_at = clock_timestamp()
    WHERE scope_type = kind AND scope_id = identity AND period_start = month
      AND (policy_revision = 0 OR (budget_mode, desired_limit, desired_usd_limit,
          desired_enforce, desired_enabled) IS DISTINCT FROM (
          COALESCE(selected_mode, 'tokens'), COALESCE(amount, 0), usd_amount,
          COALESCE(blocking, TRUE), COALESCE(available, FALSE)));
    PERFORM pg_notify('budget_settlement_ready', '');
END;
$$;

CREATE OR REPLACE FUNCTION queue_budget_execution_evidence(
    execution budget_execution_attempt, source_key TEXT, source_rank SMALLINT,
    tokens BIGINT, observed TIMESTAMPTZ
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE evidence_id BIGINT; actual_usd NUMERIC(30,14); admitted_price JSONB;
BEGIN
    IF execution.budget_mode = 'usd' THEN
        admitted_price := execution.price_snapshot;
        IF admitted_price IS NULL
            OR admitted_price->>'model_id' IS DISTINCT FROM execution.model_id::text
            OR COALESCE(admitted_price->>'price_sha256', '') !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'USD execution is missing its immutable admission price';
        END IF;
        IF source_rank = 3 THEN
            SELECT usage.usd_cost INTO actual_usd
            FROM token_usage usage
            WHERE usage.id = execution.usage_id
              AND usage.usage_domain = 'apim'
              AND NOT usage.estimated
              AND usage.usd_cost IS NOT NULL
              AND usage.usd_price_sha256 = admitted_price->>'price_sha256';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Exact USD usage does not match its admitted price snapshot';
            END IF;
        ELSIF source_rank = 2 AND source_key LIKE 'rejection:%' THEN
            actual_usd := 0;
        ELSIF source_rank = 1 THEN
            actual_usd := NULL;
            admitted_price := NULL;
        ELSE
            RETURN;
        END IF;
    ELSE
        actual_usd := NULL;
        admitted_price := NULL;
    END IF;

    INSERT INTO budget_execution_evidence (
        scope_type, scope_id, period_start, attempt_key, evidence_key,
        evidence_rank, total_tokens, observed_at, actual_usd, price_snapshot
    ) VALUES (
        execution.scope_type, execution.scope_id, execution.period_start, execution.attempt_key,
        source_key, source_rank, tokens, observed, actual_usd, admitted_price
    ) ON CONFLICT DO NOTHING RETURNING id INTO evidence_id;
    IF evidence_id IS NULL THEN RETURN; END IF;
    IF source_rank = 1 THEN RETURN; END IF;
    INSERT INTO budget_settlement_task AS stored (
        scope_type, scope_id, period_start, attempt_key, selected_evidence_id,
        evidence_rank, evidence_key, total_tokens, actual_usd, price_snapshot
    ) VALUES (
        execution.scope_type, execution.scope_id, execution.period_start, execution.attempt_key,
        evidence_id, source_rank, source_key, tokens, actual_usd, admitted_price
    ) ON CONFLICT (scope_type, scope_id, period_start, attempt_key) DO UPDATE SET
        selected_evidence_id = EXCLUDED.selected_evidence_id,
        evidence_rank = EXCLUDED.evidence_rank, evidence_key = EXCLUDED.evidence_key,
        total_tokens = EXCLUDED.total_tokens, actual_usd = EXCLUDED.actual_usd,
        price_snapshot = EXCLUDED.price_snapshot, revision = stored.revision + 1,
        available_at = clock_timestamp(), updated_at = clock_timestamp(), last_error = NULL
    WHERE EXCLUDED.evidence_rank > stored.evidence_rank;
    PERFORM pg_notify('budget_settlement_ready', '');
END;
$$;