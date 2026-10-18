ALTER TABLE public.managed_model_price
    ADD COLUMN context_price_tiers JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN pending_context_price_tiers JSONB,
    ADD CONSTRAINT managed_model_context_price_tiers_array
        CHECK (jsonb_typeof(context_price_tiers) = 'array'
               AND jsonb_array_length(context_price_tiers) <= 32),
    ADD CONSTRAINT managed_model_pending_context_price_tiers_array
        CHECK (pending_context_price_tiers IS NULL
               OR jsonb_typeof(pending_context_price_tiers) = 'array');

ALTER TABLE public.token_usage
    ADD COLUMN price_context_tier TEXT,
    ADD COLUMN price_context_snapshot JSONB,
    ADD CONSTRAINT token_usage_context_price_snapshot_object
        CHECK (price_context_snapshot IS NULL
               OR jsonb_typeof(price_context_snapshot) = 'object'),
    ADD CONSTRAINT token_usage_context_price_tier_has_snapshot
        CHECK (price_context_tier IS NULL OR price_context_snapshot IS NOT NULL);