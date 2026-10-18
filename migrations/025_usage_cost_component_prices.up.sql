CREATE OR REPLACE FUNCTION budget_usage_usd_cost(
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
        WHEN is_estimated IS DISTINCT FROM FALSE THEN NULL
        WHEN stored_cost IS NOT NULL THEN stored_cost::NUMERIC(30,14)
        WHEN (input_tokens = 0 OR input_rate IS NOT NULL)
          AND (GREATEST(cached_tokens - cache_write_tokens, 0) = 0 OR cache_read_rate IS NOT NULL)
          AND (cache_write_tokens = 0 OR cache_write_rate IS NOT NULL)
          AND (output_tokens = 0 OR output_rate IS NOT NULL)
        THEN ROUND((
            input_tokens * COALESCE(input_rate, 0)
            + GREATEST(cached_tokens - cache_write_tokens, 0) * COALESCE(cache_read_rate, 0)
            + cache_write_tokens * COALESCE(cache_write_rate, 0)
            + output_tokens * COALESCE(output_rate, 0)
        ) / 1000000, 14)::NUMERIC(30,14)
        ELSE NULL
    END
$$;