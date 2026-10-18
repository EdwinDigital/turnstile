CREATE TABLE multi_target_rollout (
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rollout_kind TEXT NOT NULL CHECK (
        rollout_kind IN ('model_add', 'model_remove', 'credential_rotation', 'route_reconcile')
    ),
    logical_model_id UUID,
    request_sha256 TEXT NOT NULL UNIQUE CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL CHECK (length(btrim(created_by)) BETWEEN 1 AND 320),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (rollout_kind NOT IN ('model_add', 'model_remove') OR logical_model_id IS NOT NULL)
);

CREATE TABLE multi_target_rollout_child (
    rollout_id UUID NOT NULL REFERENCES multi_target_rollout(id) ON DELETE RESTRICT,
    apim_target_id UUID NOT NULL
        REFERENCES apim_target_metadata(gateway_profile_id) ON DELETE RESTRICT,
    publication_id UUID NOT NULL UNIQUE REFERENCES gateway_publication(id) ON DELETE RESTRICT,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rollout_id, apim_target_id)
);

CREATE INDEX multi_target_rollout_created_idx
    ON multi_target_rollout (created_at DESC, id DESC);

CREATE INDEX multi_target_rollout_child_target_idx
    ON multi_target_rollout_child (apim_target_id, rollout_id);

CREATE FUNCTION validate_multi_target_rollout_child()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    publication_target UUID;
BEGIN
    SELECT gateway_profile_id INTO publication_target
      FROM gateway_publication WHERE id = NEW.publication_id;
    IF publication_target IS NULL OR publication_target <> NEW.apim_target_id THEN
        RAISE EXCEPTION 'Multi-target child publication must belong to its APIM target';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER multi_target_rollout_child_target_check
BEFORE INSERT OR UPDATE OF apim_target_id, publication_id
ON multi_target_rollout_child
FOR EACH ROW EXECUTE FUNCTION validate_multi_target_rollout_child();