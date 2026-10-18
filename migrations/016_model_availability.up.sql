CREATE TABLE model_availability_identity (
    model_id UUID PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('staged', 'active', 'deleted')),
    staged_publication_id UUID REFERENCES gateway_publication(id) ON DELETE RESTRICT,
    effective_revision INTEGER CHECK (effective_revision > 0 AND effective_revision <= revision),
    effective_enabled BOOLEAN,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((effective_revision IS NULL AND effective_enabled IS NULL AND verified_at IS NULL)
        OR (effective_revision IS NOT NULL AND effective_enabled IS NOT NULL
            AND verified_at IS NOT NULL))
);

CREATE TABLE model_availability_capability (
    target_id UUID PRIMARY KEY REFERENCES gateway_profile(id) ON DELETE RESTRICT,
    coverage JSONB NOT NULL CHECK (jsonb_typeof(coverage) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE model_availability_node_enrollment (
    enrollment_id UUID PRIMARY KEY,
    target_id UUID NOT NULL REFERENCES gateway_profile(id) ON DELETE RESTRICT,
    deployment_id UUID NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    enrollment JSONB NOT NULL CHECK (jsonb_typeof(enrollment) = 'object'),
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (deployment_id, revision),
    FOREIGN KEY (deployment_id, target_id)
        REFERENCES gateway_deployment(id, apim_target_id) ON DELETE RESTRICT
);

CREATE TABLE model_availability_operation (
    id UUID PRIMARY KEY,
    model_id UUID NOT NULL REFERENCES model_availability_identity(model_id) ON DELETE RESTRICT,
    idempotency_key UUID NOT NULL UNIQUE,
    expected_revision INTEGER NOT NULL CHECK (expected_revision > 0),
    desired_revision INTEGER NOT NULL CHECK (desired_revision = expected_revision + 1),
    desired_enabled BOOLEAN NOT NULL,
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_xid BIGINT NOT NULL DEFAULT txid_current(),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'projected', 'partial', 'succeeded', 'failed', 'unknown')),
    targets JSONB NOT NULL CHECK (jsonb_typeof(targets) = 'array'),
    projected_revision INTEGER CHECK (projected_revision > 0),
    projected_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_code TEXT,
    UNIQUE (model_id, desired_revision)
);

CREATE TABLE model_availability_task (
    model_id UUID PRIMARY KEY REFERENCES model_availability_identity(model_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    operation_id UUID REFERENCES model_availability_operation(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'leased', 'done', 'failed')),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 3),
    last_error_code TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE INDEX model_availability_task_pending
    ON model_availability_task(available_at, model_id) WHERE status IN ('pending', 'leased');

CREATE TABLE model_availability_audit (
    id BIGSERIAL PRIMARY KEY,
    model_id UUID NOT NULL REFERENCES model_availability_identity(model_id) ON DELETE RESTRICT,
    operation_id UUID REFERENCES model_availability_operation(id) ON DELETE RESTRICT,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION guard_model_availability_history() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME IN ('model_availability_audit', 'model_availability_node_enrollment')
        OR TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Model availability history is immutable';
    END IF;
    IF ROW(NEW.id, NEW.model_id, NEW.idempotency_key, NEW.expected_revision,
           NEW.desired_revision, NEW.desired_enabled, NEW.created_by, NEW.created_at,
           NEW.accepted_xid)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.model_id, OLD.idempotency_key, OLD.expected_revision,
           OLD.desired_revision, OLD.desired_enabled, OLD.created_by, OLD.created_at,
           OLD.accepted_xid) THEN
        RAISE EXCEPTION 'Model availability intent is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER model_availability_operation_history
    BEFORE UPDATE OR DELETE ON model_availability_operation
    FOR EACH ROW EXECUTE FUNCTION guard_model_availability_history();
CREATE TRIGGER model_availability_audit_history
    BEFORE UPDATE OR DELETE ON model_availability_audit
    FOR EACH ROW EXECUTE FUNCTION guard_model_availability_history();
CREATE TRIGGER model_availability_node_enrollment_history
    BEFORE UPDATE OR DELETE ON model_availability_node_enrollment
    FOR EACH ROW EXECUTE FUNCTION guard_model_availability_history();

CREATE FUNCTION guard_managed_model_availability() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    identity_row model_availability_identity%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO identity_row FROM model_availability_identity
            WHERE model_id = NEW.id FOR UPDATE;
        IF FOUND AND identity_row.lifecycle <> 'staged' THEN
            RAISE EXCEPTION 'A retained model availability identity cannot be reused';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.enabled IS DISTINCT FROM OLD.enabled AND NOT EXISTS (
            SELECT 1 FROM model_availability_operation operation
            JOIN model_availability_identity identity USING (model_id)
            WHERE operation.model_id = OLD.id
              AND operation.desired_revision = identity.revision
              AND operation.desired_enabled = NEW.enabled
              AND operation.accepted_xid = txid_current()
              AND operation.status = 'pending'
              AND identity.lifecycle = 'active'
        ) THEN
            RAISE EXCEPTION 'Use the versioned model availability command';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO identity_row FROM model_availability_identity
        WHERE model_id = OLD.id FOR UPDATE;
    IF NOT FOUND OR EXISTS (
        SELECT 1 FROM model_availability_operation
        WHERE model_id = OLD.id AND status NOT IN ('succeeded', 'failed')
    ) THEN
        RAISE EXCEPTION 'Model availability transition prevents deletion';
    END IF;
    UPDATE model_availability_identity SET lifecycle = 'deleted', revision = revision + 1,
        updated_at = now() WHERE model_id = OLD.id RETURNING * INTO identity_row;
    INSERT INTO model_availability_task(model_id, revision)
        VALUES (OLD.id, identity_row.revision)
        ON CONFLICT (model_id) DO UPDATE SET revision = EXCLUDED.revision,
            operation_id = NULL, status = 'pending', available_at = now(),
            lease_owner = NULL, lease_expires_at = NULL, attempt_count = 0,
            retry_count = 0, last_error_code = NULL, updated_at = now();
    INSERT INTO model_availability_audit(model_id, event, actor, detail)
        VALUES (OLD.id, 'deleted', 'model-lifecycle',
            jsonb_build_object('revision', identity_row.revision));
    RETURN OLD;
END;
$$;

CREATE FUNCTION initialize_managed_model_availability() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    next_revision INTEGER;
BEGIN
    INSERT INTO model_availability_identity(model_id, lifecycle)
        VALUES (NEW.id, 'active')
        ON CONFLICT (model_id) DO UPDATE SET lifecycle = 'active',
            revision = model_availability_identity.revision + 1, updated_at = now()
        WHERE model_availability_identity.lifecycle = 'staged'
        RETURNING revision INTO next_revision;
    IF next_revision IS NULL THEN
        RAISE EXCEPTION 'Model availability identity cannot be initialized';
    END IF;
    INSERT INTO model_availability_task(model_id, revision)
        VALUES (NEW.id, next_revision)
        ON CONFLICT (model_id) DO UPDATE SET revision = EXCLUDED.revision,
            operation_id = NULL, status = 'pending', available_at = now(),
            lease_owner = NULL, lease_expires_at = NULL, attempt_count = 0,
            retry_count = 0, last_error_code = NULL, updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER managed_model_availability_guard
    BEFORE INSERT OR UPDATE OF enabled OR DELETE ON managed_model
    FOR EACH ROW EXECUTE FUNCTION guard_managed_model_availability();
CREATE TRIGGER managed_model_availability_initialize
    AFTER INSERT ON managed_model
    FOR EACH ROW EXECUTE FUNCTION initialize_managed_model_availability();

CREATE FUNCTION model_availability_inventory() RETURNS TABLE (
    target_id UUID, inventory_digest TEXT
) LANGUAGE sql STABLE AS $$
    SELECT gateway.id, encode(sha256(convert_to(jsonb_build_object(
        'gateway', jsonb_build_object('id', gateway.id, 'base_url', gateway.base_url,
            'enabled', gateway.enabled),
        'target', jsonb_build_object('arm_id', target.arm_resource_id,
            'api_id', target.api_id, 'api_path', target.api_path,
            'tenant_id', target.tenant_id, 'principal_id', target.principal_id),
        'deployments', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', deployment.id, 'native_id', deployment.native_gateway_id,
            'endpoint', deployment.endpoint_url, 'kind', deployment.kind,
            'retired_at', deployment.retired_at) ORDER BY deployment.id)
            FROM gateway_deployment deployment WHERE deployment.apim_target_id = gateway.id),
            '[]'::JSONB),
        'node_enrollments', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', enrollment.enrollment_id, 'deployment_id', enrollment.deployment_id,
            'revision', enrollment.revision, 'enrollment', enrollment.enrollment)
            ORDER BY enrollment.deployment_id, enrollment.revision)
            FROM model_availability_node_enrollment enrollment
            WHERE enrollment.target_id = gateway.id), '[]'::JSONB),
        'bindings', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', binding.id, 'model_id', binding.logical_model_id,
            'runtime_id', binding.runtime_id, 'enabled', binding.enabled,
            'scope', binding.deployment_scope_mode, 'publication', binding.current_publication_id)
            ORDER BY binding.id)
            FROM apim_target_route_binding binding WHERE binding.apim_target_id = gateway.id),
            '[]'::JSONB),
        'publications', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', publication.id, 'revision', publication.apim_revision,
            'status', publication.status, 'spec', publication.desired_spec)
            ORDER BY publication.id)
            FROM gateway_publication publication WHERE publication.gateway_profile_id = gateway.id),
            '[]'::JSONB)
    )::TEXT, 'UTF8')), 'hex')
    FROM gateway_profile gateway
    LEFT JOIN apim_target_metadata target ON target.gateway_profile_id = gateway.id
    WHERE gateway.implementation = 'apim' AND (
        target.gateway_profile_id IS NOT NULL OR gateway.base_url IS NOT NULL
        OR EXISTS (SELECT 1 FROM gateway_deployment deployment
            WHERE deployment.apim_target_id = gateway.id)
        OR EXISTS (SELECT 1 FROM apim_target_route_binding binding
            WHERE binding.apim_target_id = gateway.id)
        OR EXISTS (SELECT 1 FROM gateway_publication publication
            WHERE publication.gateway_profile_id = gateway.id)
    );
$$;

INSERT INTO model_availability_identity(model_id, lifecycle)
    SELECT id, 'active' FROM managed_model;
INSERT INTO model_availability_task(model_id, revision)
    SELECT model_id, revision FROM model_availability_identity;