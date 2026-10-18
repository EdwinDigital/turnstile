CREATE TABLE budget_execution_observation (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period_start DATE NOT NULL,
    attempt_key TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('apim', 'observer')),
    observation_sha256 TEXT NOT NULL CHECK (observation_sha256 ~ '^[0-9a-f]{64}$'),
    observed_at TIMESTAMPTZ NOT NULL,
    observation JSONB NOT NULL CHECK (
        (jsonb_typeof(observation) = 'object'
         AND observation->>'source' = source
         AND jsonb_typeof(observation->'observed_at') = 'string'
         AND observation->'rejection'->>'version' = '2'
         AND observation->'rejection'->>'observation_sha256' = observation_sha256
         AND octet_length(observation::text) <= 4096) IS TRUE
    ),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope_type, scope_id, period_start, attempt_key, source, observation_sha256),
    FOREIGN KEY (scope_type, scope_id, period_start, attempt_key)
        REFERENCES budget_execution_attempt(scope_type, scope_id, period_start, attempt_key)
);

CREATE TRIGGER budget_execution_observation_immutable
BEFORE UPDATE OR DELETE ON budget_execution_observation
FOR EACH ROW EXECUTE FUNCTION reject_budget_reservation_finalization_mutation();