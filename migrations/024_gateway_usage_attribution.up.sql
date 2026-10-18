CREATE TABLE token_usage_gateway_attribution (
    usage_id TEXT PRIMARY KEY REFERENCES token_usage(id) ON DELETE RESTRICT,
    apim_target_id UUID NOT NULL
        REFERENCES apim_target_metadata(gateway_profile_id) ON DELETE RESTRICT,
    gateway_deployment_id UUID,
    native_gateway_id TEXT NOT NULL CHECK (
        length(btrim(native_gateway_id)) BETWEEN 1 AND 80
    ),
    native_instance_id TEXT CHECK (
        native_instance_id IS NULL OR length(btrim(native_instance_id)) BETWEEN 1 AND 255
    ),
    is_managed BOOLEAN NOT NULL,
    apim_service_id TEXT NOT NULL CHECK (length(btrim(apim_service_id)) BETWEEN 1 AND 2048),
    apim_region TEXT NOT NULL CHECK (length(btrim(apim_region)) BETWEEN 1 AND 128),
    api_revision TEXT NOT NULL CHECK (length(btrim(api_revision)) BETWEEN 1 AND 256),
    event_schema_version INTEGER NOT NULL CHECK (event_schema_version >= 2),
    attribution_source TEXT NOT NULL CHECK (
        attribution_source IN ('eventhub', 'observer', 'reconciliation', 'backfill')
    ),
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (gateway_deployment_id, apim_target_id)
        REFERENCES gateway_deployment(id, apim_target_id) ON DELETE RESTRICT,
    CHECK (
        (is_managed AND lower(native_gateway_id) = 'managed')
        OR (NOT is_managed AND lower(native_gateway_id) <> 'managed')
    ),
    CHECK (
        NOT is_managed OR native_instance_id IS NULL OR lower(native_instance_id) = 'managed'
    )
);

CREATE INDEX token_usage_gateway_attribution_target_idx
    ON token_usage_gateway_attribution (apim_target_id, observed_at DESC, usage_id);

CREATE INDEX token_usage_gateway_attribution_deployment_idx
    ON token_usage_gateway_attribution (gateway_deployment_id, observed_at DESC, usage_id)
    WHERE gateway_deployment_id IS NOT NULL;

COMMENT ON TABLE token_usage_gateway_attribution IS
    'Versioned APIM target, Gateway deployment, and instance attribution keyed by the canonical token_usage ID.';