CREATE TABLE apim_target_metadata (
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    gateway_profile_id UUID PRIMARY KEY REFERENCES gateway_profile(id) ON DELETE RESTRICT,
    arm_resource_id TEXT NOT NULL CHECK (length(btrim(arm_resource_id)) BETWEEN 1 AND 2048
                                        AND arm_resource_id LIKE '/subscriptions/%'),
    tenant_id UUID NOT NULL,
    subscription_id UUID NOT NULL,
    resource_group TEXT NOT NULL CHECK (length(btrim(resource_group)) BETWEEN 1 AND 90),
    service_name TEXT NOT NULL CHECK (length(btrim(service_name)) BETWEEN 1 AND 50),
    api_id TEXT NOT NULL CHECK (length(btrim(api_id)) BETWEEN 1 AND 256),
    api_path TEXT NOT NULL CHECK (length(btrim(api_path)) BETWEEN 1 AND 400
                                 AND api_path = btrim(api_path, '/')),
    service_tier TEXT NOT NULL CHECK (length(btrim(service_tier)) BETWEEN 1 AND 64),
    tier_family TEXT NOT NULL CHECK (tier_family IN ('classic', 'v2')),
    service_location TEXT NOT NULL CHECK (length(btrim(service_location)) BETWEEN 1 AND 128),
    principal_id UUID,
    management_auth_profile_ref TEXT CHECK (management_auth_profile_ref IS NULL
                                           OR length(btrim(management_auth_profile_ref)) BETWEEN 1 AND 255),
    probe_subscription_id TEXT CHECK (probe_subscription_id IS NULL
                                     OR length(btrim(probe_subscription_id)) BETWEEN 1 AND 255),
    probe_secret_ref TEXT CHECK (probe_secret_ref IS NULL OR probe_secret_ref ~ '^https://[^[:space:]]+$'),
    ownership_mode TEXT NOT NULL CHECK (ownership_mode IN ('turnstile_managed', 'adopted', 'read_only')),
    capabilities JSONB NOT NULL DEFAULT '{"schema_version":1}'::jsonb
        CHECK (jsonb_typeof(capabilities) = 'object'),
    last_inventory_sync_at TIMESTAMPTZ,
    last_validated_at TIMESTAMPTZ,
    drift_state TEXT NOT NULL DEFAULT 'unknown' CHECK (drift_state IN ('unknown', 'in_sync', 'drifted')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (tier_family <> 'v2'
           OR COALESCE(capabilities->>'self_hosted_gateway', 'unknown') <> 'supported')
);
CREATE UNIQUE INDEX apim_target_metadata_arm_resource_ci_idx
ON apim_target_metadata(lower(arm_resource_id));
CREATE UNIQUE INDEX apim_target_metadata_service_identity_ci_idx
ON apim_target_metadata(tenant_id, subscription_id, lower(resource_group), lower(service_name));

CREATE FUNCTION validate_apim_target_gateway_profile() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM gateway_profile
                   WHERE id = NEW.gateway_profile_id AND implementation = 'apim') THEN
        RAISE EXCEPTION 'APIM target requires an APIM gateway profile';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER apim_target_gateway_profile_check
BEFORE INSERT OR UPDATE OF gateway_profile_id ON apim_target_metadata
FOR EACH ROW EXECUTE FUNCTION validate_apim_target_gateway_profile();

CREATE TABLE gateway_deployment (
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    apim_target_id UUID NOT NULL REFERENCES apim_target_metadata(gateway_profile_id) ON DELETE RESTRICT,
    native_gateway_id TEXT NOT NULL CHECK (length(btrim(native_gateway_id)) BETWEEN 1 AND 80),
    kind TEXT NOT NULL CHECK (kind IN ('managed', 'self_hosted')),
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 120),
    location TEXT CHECK (location IS NULL OR length(btrim(location)) BETWEEN 1 AND 128),
    endpoint_url TEXT CHECK (endpoint_url IS NULL OR endpoint_url ~ '^https://[^[:space:]]+$'),
    hosting_mode TEXT NOT NULL CHECK (hosting_mode IN (
        'azure_managed', 'aks', 'kubernetes', 'arc', 'container_apps', 'docker', 'other')),
    lifecycle_ownership TEXT NOT NULL CHECK (lifecycle_ownership IN ('customer', 'turnstile_iac', 'external')),
    release_gate TEXT NOT NULL DEFAULT 'required' CHECK (release_gate IN ('required', 'optional')),
    declared_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (declared_state IN ('unknown', 'present', 'absent', 'unsupported')),
    reachability_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (reachability_state IN ('unknown', 'reachable', 'unreachable')),
    observation_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (observation_state IN ('unknown', 'observed', 'stale')),
    runtime_version_support TEXT NOT NULL DEFAULT 'unknown'
        CHECK (runtime_version_support IN ('unknown', 'supported', 'unsupported')),
    rate_limit_synchronization TEXT NOT NULL DEFAULT 'unknown'
        CHECK (rate_limit_synchronization IN ('unknown', 'supported', 'unsupported')),
    last_arm_sync_at TIMESTAMPTZ,
    last_probe_at TIMESTAMPTZ,
    last_observed_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, apim_target_id),
    CHECK ((kind = 'managed' AND lower(native_gateway_id) = 'managed' AND hosting_mode = 'azure_managed')
        OR (kind = 'self_hosted' AND lower(native_gateway_id) <> 'managed' AND hosting_mode <> 'azure_managed')),
    CHECK (declared_state = 'unknown' OR last_arm_sync_at IS NOT NULL),
    CHECK (reachability_state = 'unknown' OR (endpoint_url IS NOT NULL AND last_probe_at IS NOT NULL)),
    CHECK (observation_state = 'unknown' OR last_observed_at IS NOT NULL),
    CHECK (hosting_mode <> 'container_apps' OR rate_limit_synchronization <> 'supported')
);
CREATE UNIQUE INDEX gateway_deployment_native_id_ci_idx
ON gateway_deployment(apim_target_id, lower(native_gateway_id));
CREATE INDEX gateway_deployment_active_target_idx
ON gateway_deployment(apim_target_id, kind, display_name) WHERE retired_at IS NULL;
CREATE INDEX gateway_deployment_observation_idx
ON gateway_deployment(apim_target_id, last_observed_at DESC);

CREATE TABLE gateway_instance_observation (
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    deployment_id UUID NOT NULL REFERENCES gateway_deployment(id) ON DELETE RESTRICT,
    native_instance_id TEXT NOT NULL CHECK (length(btrim(native_instance_id)) BETWEEN 1 AND 255),
    runtime_version TEXT CHECK (runtime_version IS NULL OR length(btrim(runtime_version)) BETWEEN 1 AND 64),
    processor_count INTEGER CHECK (processor_count >= 1),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    last_successful_request_at TIMESTAMPTZ,
    source TEXT NOT NULL CHECK (source IN ('apim_context', 'cluster_telemetry', 'local_log', 'probe')),
    confidence TEXT NOT NULL CHECK (confidence IN ('declared', 'reachable', 'observed')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (deployment_id, native_instance_id),
    CHECK (last_seen_at >= first_seen_at),
    CHECK (last_successful_request_at IS NULL OR last_successful_request_at <= last_seen_at)
);
CREATE INDEX gateway_instance_observation_recent_idx
ON gateway_instance_observation(deployment_id, last_seen_at DESC);

CREATE TABLE apim_target_route_binding (
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    logical_model_id UUID NOT NULL REFERENCES managed_model(id) ON DELETE RESTRICT,
    apim_target_id UUID NOT NULL REFERENCES apim_target_metadata(gateway_profile_id) ON DELETE RESTRICT,
    runtime_id UUID NOT NULL REFERENCES model_runtime(id) ON DELETE RESTRICT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    is_default_serving BOOLEAN NOT NULL DEFAULT FALSE,
    deployment_scope_mode TEXT NOT NULL DEFAULT 'all' CHECK (deployment_scope_mode IN ('all', 'selected')),
    current_publication_id UUID REFERENCES gateway_publication(id) ON DELETE RESTRICT,
    capability_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (capability_state IN ('unknown', 'supported', 'unsupported')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (logical_model_id, apim_target_id),
    UNIQUE (id, apim_target_id),
    CHECK (enabled OR NOT is_default_serving)
);
CREATE UNIQUE INDEX apim_target_route_binding_one_default_idx
ON apim_target_route_binding(logical_model_id) WHERE is_default_serving;
CREATE INDEX apim_target_route_binding_target_idx
ON apim_target_route_binding(apim_target_id, enabled, logical_model_id);

CREATE TABLE apim_target_route_binding_deployment (
    route_binding_id UUID NOT NULL,
    apim_target_id UUID NOT NULL,
    gateway_deployment_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (route_binding_id, gateway_deployment_id),
    FOREIGN KEY (route_binding_id, apim_target_id)
        REFERENCES apim_target_route_binding(id, apim_target_id) ON DELETE RESTRICT,
    FOREIGN KEY (gateway_deployment_id, apim_target_id)
        REFERENCES gateway_deployment(id, apim_target_id) ON DELETE RESTRICT
);
CREATE INDEX apim_target_route_binding_deployment_gateway_idx
ON apim_target_route_binding_deployment(gateway_deployment_id, route_binding_id);

CREATE TABLE gateway_deployment_inventory (
    deployment_id UUID PRIMARY KEY REFERENCES gateway_deployment(id) ON DELETE RESTRICT,
    source_api_version TEXT NOT NULL CHECK (length(btrim(source_api_version)) BETWEEN 1 AND 64),
    synced_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX gateway_deployment_inventory_sync_idx
ON gateway_deployment_inventory(synced_at DESC, deployment_id);

CREATE TABLE gateway_deployment_api_association (
    deployment_id UUID NOT NULL REFERENCES gateway_deployment_inventory(deployment_id) ON DELETE RESTRICT,
    api_id TEXT NOT NULL CHECK (length(btrim(api_id)) BETWEEN 1 AND 256),
    PRIMARY KEY (deployment_id, api_id)
);
CREATE TABLE gateway_deployment_hostname_inventory (
    deployment_id UUID NOT NULL REFERENCES gateway_deployment_inventory(deployment_id) ON DELETE RESTRICT,
    configuration_id TEXT NOT NULL CHECK (length(btrim(configuration_id)) BETWEEN 1 AND 2048),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 255),
    hostname TEXT NOT NULL CHECK (length(btrim(hostname)) BETWEEN 1 AND 255),
    certificate_id TEXT CHECK (certificate_id IS NULL OR length(btrim(certificate_id)) BETWEEN 1 AND 2048),
    negotiate_client_certificate BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (deployment_id, configuration_id)
);
CREATE TABLE gateway_deployment_ca_inventory (
    deployment_id UUID NOT NULL REFERENCES gateway_deployment_inventory(deployment_id) ON DELETE RESTRICT,
    authority_id TEXT NOT NULL CHECK (length(btrim(authority_id)) BETWEEN 1 AND 2048),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 255),
    is_trusted BOOLEAN NOT NULL,
    PRIMARY KEY (deployment_id, authority_id)
);

ALTER TABLE gateway_release_operation DROP CONSTRAINT gateway_release_operation_operation_kind_check;
ALTER TABLE gateway_release_operation ADD CONSTRAINT gateway_release_operation_operation_kind_check
CHECK (operation_kind IN ('rollback', 'integrity_check', 'gc_plan', 'application_sync',
                         'application_provision', 'target_inventory_sync'));