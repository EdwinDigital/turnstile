CREATE TABLE budget_execution_resolution (
    id UUID PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period_start DATE NOT NULL,
    attempt_key TEXT NOT NULL,
    document JSONB NOT NULL CHECK (
        (jsonb_typeof(document) = 'object'
         AND document->>'version' = '1'
         AND document->>'id' = id::text
         AND document->>'decision' = 'release_unverified'
         AND length(document->>'actor') BETWEEN 1 AND 255
         AND document->>'reason' IN (
             'gateway_validation_failure', 'provider_result_unavailable', 'evidence_irrecoverable'
         )
         AND document->>'evidence_sha256' ~ '^[0-9a-f]{64}$'
         AND jsonb_typeof(document->'approved_at') = 'string'
         AND document #>> '{receipt,identity,scope,scope_type}' = scope_type
         AND document #>> '{receipt,identity,scope,scope_id}' = scope_id
         AND document #>> '{receipt,identity,scope,period_start}' = period_start::text
         AND octet_length(document::text) <= 8192) IS TRUE
    ),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (scope_type, scope_id, period_start, attempt_key),
    FOREIGN KEY (scope_type, scope_id, period_start, attempt_key)
        REFERENCES budget_execution_attempt(scope_type, scope_id, period_start, attempt_key)
);

CREATE TRIGGER budget_execution_resolution_immutable
BEFORE UPDATE OR DELETE ON budget_execution_resolution
FOR EACH ROW EXECUTE FUNCTION reject_budget_reservation_finalization_mutation();