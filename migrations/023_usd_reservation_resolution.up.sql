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
        ELSIF source_rank = 2 AND source_key LIKE 'resolution:%' THEN
            PERFORM 1 FROM budget_execution_resolution resolution
            WHERE 'resolution:' || resolution.id::text = source_key
              AND (resolution.scope_type, resolution.scope_id,
                   resolution.period_start, resolution.attempt_key) = (
                  execution.scope_type, execution.scope_id,
                  execution.period_start, execution.attempt_key)
              AND resolution.document->>'decision' = 'release_unverified'
              AND resolution.document #> '{receipt,identity}' = execution.identity
              AND resolution.document #>> '{receipt,epoch}' = execution.epoch::text
              AND (resolution.document #>> '{receipt,reserved_tokens}')::BIGINT
                  = execution.reserved_tokens
              AND resolution.document #>> '{receipt,budget_mode}' = 'usd'
              AND (resolution.document #>> '{receipt,reserved_usd}')::NUMERIC
                  = execution.reserved_usd
              AND resolution.document #> '{receipt,price_snapshot}' = admitted_price
              AND (resolution.document->>'approved_at')::TIMESTAMPTZ = observed
              AND tokens = 0;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'USD release evidence does not match its immutable approval';
            END IF;
            actual_usd := NULL;
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