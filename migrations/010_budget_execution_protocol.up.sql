CREATE TABLE budget_execution_rollout (
    epoch UUID PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared', 'active', 'paused')),
    gateway_profile_ids UUID[] NOT NULL CHECK (cardinality(gateway_profile_ids) > 0),
    admitted_after TIMESTAMPTZ NOT NULL,
    legacy_fence_verified_at TIMESTAMPTZ,
    scope_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    prepared_period DATE CHECK (extract(day FROM prepared_period) = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (NOT scope_sync_enabled OR (state = 'active' AND legacy_fence_verified_at IS NOT NULL))
);
CREATE UNIQUE INDEX budget_execution_one_active_rollout
ON budget_execution_rollout ((TRUE)) WHERE state = 'active';

CREATE TABLE budget_execution_scope (
    scope_type TEXT NOT NULL CHECK (scope_type IN ('person', 'application')),
    scope_id TEXT NOT NULL CHECK (length(btrim(scope_id)) BETWEEN 1 AND 255),
    period_start DATE NOT NULL CHECK (extract(day FROM period_start) = 1),
    epoch UUID NOT NULL,
    state TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared', 'active', 'draining')),
    admitted_after TIMESTAMPTZ NOT NULL,
    legacy_fence_verified_at TIMESTAMPTZ,
    recovery_next_partition TEXT,
    recovery_next_row TEXT,
    recovery_version BIGINT NOT NULL DEFAULT 0 CHECK (recovery_version >= 0),
    recovery_checked_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
    policy_revision BIGINT NOT NULL DEFAULT 0 CHECK (policy_revision >= 0),
    policy_applied_revision BIGINT NOT NULL DEFAULT 0,
    desired_limit BIGINT NOT NULL DEFAULT 0 CHECK (desired_limit >= 0),
    desired_enforce BOOLEAN NOT NULL DEFAULT TRUE,
    desired_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    policy_updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    seed_confirmed_tokens BIGINT CHECK (seed_confirmed_tokens >= 0),
    seed_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope_type, scope_id, period_start),
    UNIQUE (scope_type, scope_id, period_start, epoch),
    CHECK (state = 'prepared' OR legacy_fence_verified_at IS NOT NULL),
    CHECK (policy_applied_revision BETWEEN 0 AND policy_revision)
);
CREATE INDEX budget_execution_scope_preparation
ON budget_execution_scope(created_at) WHERE state = 'prepared';

CREATE FUNCTION queue_budget_execution_policy(kind TEXT, identity TEXT, month DATE)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE amount BIGINT; blocking BOOLEAN; available BOOLEAN;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'execution-policy:' || kind || ':' || identity || ':' || month::text, 0));
    IF kind = 'person' THEN
        SELECT budget.token_limit, COALESCE(enforcement.mode, 'audit') = 'block', TRUE
        INTO amount, blocking, available FROM token_budget budget
        LEFT JOIN department_enforcement enforcement ON enforcement.department_id = budget.parent_scope_id
        WHERE budget.scope_type = 'user' AND budget.scope_id = identity AND budget.period_start = month;
        IF NOT FOUND THEN amount := 0; blocking := FALSE; available := TRUE; END IF;
    ELSIF kind = 'application' THEN
        SELECT budget.token_limit, budget.enforce, application.status = 'active'
        INTO amount, blocking, available FROM gateway_application_budget budget
        JOIN gateway_application application ON application.id = budget.application_id
        WHERE budget.application_id::text = identity AND budget.period_start = month;
    ELSE
        RAISE EXCEPTION 'Unsupported execution scope type';
    END IF;
    UPDATE budget_execution_scope SET desired_limit = COALESCE(amount, 0),
        desired_enforce = COALESCE(blocking, TRUE), desired_enabled = COALESCE(available, FALSE),
        policy_revision = policy_revision + 1, policy_updated_at = clock_timestamp()
    WHERE scope_type = kind AND scope_id = identity AND period_start = month
      AND (policy_revision = 0 OR (desired_limit, desired_enforce, desired_enabled)
        IS DISTINCT FROM (COALESCE(amount, 0), COALESCE(blocking, TRUE), COALESCE(available, FALSE)));
    PERFORM pg_notify('budget_settlement_ready', '');
END;
$$;

CREATE FUNCTION budget_execution_policy_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE execution budget_execution_scope; selected JSONB;
BEGIN
    selected := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    FOR execution IN SELECT * FROM budget_execution_scope
    WHERE (TG_TABLE_NAME = 'token_budget' AND scope_type = 'person'
          AND scope_id = selected->>'scope_id' AND period_start = (selected->>'period_start')::date)
       OR (TG_TABLE_NAME = 'gateway_application_budget' AND scope_type = 'application'
          AND scope_id = selected->>'application_id' AND period_start = (selected->>'period_start')::date)
       OR (TG_TABLE_NAME = 'gateway_application' AND scope_type = 'application' AND scope_id = selected->>'id')
       OR (TG_TABLE_NAME = 'department_enforcement' AND scope_type = 'person' AND scope_id IN (
          SELECT scope_id FROM token_budget WHERE scope_type = 'user'
          AND parent_scope_id = selected->>'department_id'))
       OR (TG_TABLE_NAME = 'budget_execution_scope' AND scope_type = selected->>'scope_type'
          AND scope_id = selected->>'scope_id' AND period_start = (selected->>'period_start')::date)
    ORDER BY scope_type, scope_id, period_start
    LOOP
        PERFORM queue_budget_execution_policy(execution.scope_type, execution.scope_id, execution.period_start);
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER budget_execution_person_policy AFTER INSERT OR UPDATE OR DELETE ON token_budget
FOR EACH ROW EXECUTE FUNCTION budget_execution_policy_changed();
CREATE TRIGGER budget_execution_application_policy AFTER INSERT OR UPDATE OR DELETE ON gateway_application_budget
FOR EACH ROW EXECUTE FUNCTION budget_execution_policy_changed();
CREATE TRIGGER budget_execution_application_status AFTER UPDATE OF status ON gateway_application
FOR EACH ROW EXECUTE FUNCTION budget_execution_policy_changed();
CREATE TRIGGER budget_execution_department_policy AFTER INSERT OR UPDATE OR DELETE ON department_enforcement
FOR EACH ROW EXECUTE FUNCTION budget_execution_policy_changed();
CREATE TRIGGER budget_execution_initial_policy AFTER INSERT ON budget_execution_scope
FOR EACH ROW EXECUTE FUNCTION budget_execution_policy_changed();

CREATE TABLE budget_execution_dispatch (
    id UUID PRIMARY KEY,
    caller_request_id TEXT NOT NULL,
    receipts JSONB NOT NULL CHECK (jsonb_typeof(receipts) = 'array'
                                  AND jsonb_array_length(receipts) BETWEEN 1 AND 2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER budget_execution_dispatch_immutable
BEFORE UPDATE OR DELETE ON budget_execution_dispatch
FOR EACH ROW EXECUTE FUNCTION reject_budget_reservation_finalization_mutation();

CREATE TABLE budget_execution_attempt (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period_start DATE NOT NULL,
    epoch UUID NOT NULL,
    attempt_key TEXT NOT NULL CHECK (attempt_key ~ '^[0-9a-f]{64}$'),
    gateway_profile_id UUID NOT NULL REFERENCES gateway_profile(id),
    native_gateway_id TEXT NOT NULL CHECK (length(btrim(native_gateway_id)) BETWEEN 1 AND 80),
    correlation_id TEXT NOT NULL CHECK (length(btrim(correlation_id)) BETWEEN 1 AND 255),
    caller_request_id TEXT NOT NULL,
    model_id TEXT NOT NULL CHECK (length(btrim(model_id)) BETWEEN 1 AND 255),
    admitted_at TIMESTAMPTZ NOT NULL,
    reserved_tokens BIGINT NOT NULL CHECK (reserved_tokens > 0),
    identity JSONB NOT NULL CHECK (jsonb_typeof(identity) = 'object'),
    dispatch_id UUID REFERENCES budget_execution_dispatch(id),
    usage_id TEXT REFERENCES token_usage(id),
    usage_complete BOOLEAN NOT NULL DEFAULT FALSE,
    completion_checked_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
    uncertain_finalized_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope_type, scope_id, period_start, attempt_key),
    UNIQUE (scope_type, scope_id, period_start, gateway_profile_id, native_gateway_id, correlation_id),
    FOREIGN KEY (scope_type, scope_id, period_start, epoch)
        REFERENCES budget_execution_scope(scope_type, scope_id, period_start, epoch)
);
CREATE INDEX budget_execution_attempt_usage ON budget_execution_attempt(usage_id);
CREATE INDEX budget_execution_attempt_correlation ON budget_execution_attempt(correlation_id, model_id);
CREATE INDEX budget_execution_attempt_caller ON budget_execution_attempt(caller_request_id);
CREATE INDEX budget_execution_completion_cursor ON budget_execution_attempt(completion_checked_at, created_at);

CREATE TABLE budget_execution_evidence (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period_start DATE NOT NULL,
    attempt_key TEXT NOT NULL,
    evidence_key TEXT NOT NULL CHECK (length(btrim(evidence_key)) BETWEEN 1 AND 512),
    evidence_rank SMALLINT NOT NULL CHECK (evidence_rank BETWEEN 1 AND 3),
    total_tokens BIGINT NOT NULL CHECK (total_tokens >= 0),
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (scope_type, scope_id, period_start, attempt_key, evidence_key, evidence_rank, total_tokens),
    FOREIGN KEY (scope_type, scope_id, period_start, attempt_key)
        REFERENCES budget_execution_attempt(scope_type, scope_id, period_start, attempt_key)
);
CREATE TRIGGER budget_execution_evidence_immutable
BEFORE UPDATE OR DELETE ON budget_execution_evidence
FOR EACH ROW EXECUTE FUNCTION reject_budget_reservation_finalization_mutation();

CREATE TABLE budget_settlement_task (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period_start DATE NOT NULL,
    attempt_key TEXT NOT NULL,
    selected_evidence_id BIGINT NOT NULL REFERENCES budget_execution_evidence(id),
    evidence_rank SMALLINT NOT NULL CHECK (evidence_rank BETWEEN 1 AND 3),
    evidence_key TEXT NOT NULL,
    total_tokens BIGINT NOT NULL CHECK (total_tokens >= 0),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    applied_revision BIGINT NOT NULL DEFAULT 0 CHECK (applied_revision BETWEEN 0 AND revision),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_error TEXT,
    applied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (scope_type, scope_id, period_start, attempt_key),
    FOREIGN KEY (scope_type, scope_id, period_start, attempt_key)
        REFERENCES budget_execution_attempt(scope_type, scope_id, period_start, attempt_key)
);
CREATE INDEX budget_settlement_pending ON budget_settlement_task(available_at, id)
WHERE applied_revision < revision;

CREATE FUNCTION queue_budget_execution_evidence(
    execution budget_execution_attempt, source_key TEXT, source_rank SMALLINT,
    tokens BIGINT, observed TIMESTAMPTZ
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE evidence_id BIGINT;
BEGIN
    INSERT INTO budget_execution_evidence (
        scope_type, scope_id, period_start, attempt_key, evidence_key,
        evidence_rank, total_tokens, observed_at
    ) VALUES (execution.scope_type, execution.scope_id, execution.period_start, execution.attempt_key,
              source_key, source_rank, tokens, observed)
    ON CONFLICT DO NOTHING RETURNING id INTO evidence_id;
    IF evidence_id IS NULL OR source_rank = 1 THEN RETURN; END IF;
    INSERT INTO budget_settlement_task AS stored (
        scope_type, scope_id, period_start, attempt_key, selected_evidence_id,
        evidence_rank, evidence_key, total_tokens
    ) VALUES (execution.scope_type, execution.scope_id, execution.period_start, execution.attempt_key,
              evidence_id, source_rank, source_key, tokens)
    ON CONFLICT (scope_type, scope_id, period_start, attempt_key) DO UPDATE SET
        selected_evidence_id = EXCLUDED.selected_evidence_id,
        evidence_rank = EXCLUDED.evidence_rank, evidence_key = EXCLUDED.evidence_key,
        total_tokens = EXCLUDED.total_tokens, revision = stored.revision + 1,
        available_at = clock_timestamp(), updated_at = clock_timestamp(), last_error = NULL
    WHERE EXCLUDED.evidence_rank > stored.evidence_rank;
    PERFORM pg_notify('budget_settlement_ready', '');
END;
$$;

CREATE FUNCTION enqueue_budget_execution_usage(selected_usage_id TEXT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE usage token_usage; execution budget_execution_attempt; rank SMALLINT; tokens BIGINT;
BEGIN
    SELECT * INTO usage FROM token_usage WHERE id = selected_usage_id;
    IF NOT FOUND OR usage.usage_domain <> 'apim' THEN RETURN; END IF;
    IF usage.reconciled_at IS NOT NULL AND usage.ingest_error = 'stream_cache_usage_unavailable' THEN
        rank := 1; tokens := usage.input_tokens + usage.output_tokens;
    ELSE RETURN;
    END IF;
    FOR execution IN SELECT * FROM budget_execution_attempt WHERE usage_id = usage.id LOOP
        PERFORM queue_budget_execution_evidence(
            execution, 'usage:' || usage.id || ':' || rank::text, rank, tokens, usage.ts);
    END LOOP;
END;
$$;
CREATE FUNCTION budget_execution_usage_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM enqueue_budget_execution_usage(NEW.id);
    RETURN NEW;
END;
$$;
CREATE TRIGGER budget_execution_usage_outbox AFTER INSERT OR UPDATE ON token_usage
FOR EACH ROW EXECUTE FUNCTION budget_execution_usage_changed();

CREATE FUNCTION budget_execution_ack_matches(
    execution budget_execution_attempt, acknowledgement billable_request_attempt
) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT (acknowledgement).state = 'exact' AND (acknowledgement).actual_tokens IS NOT NULL
        AND (execution).model_id = (acknowledgement).model_id
        AND (execution).correlation_id = (acknowledgement).correlation_id
        AND (execution).caller_request_id = (acknowledgement).id::text
        AND (execution).gateway_profile_id::text = (acknowledgement).evidence->>'gateway_profile_id'
        AND (execution).native_gateway_id = (acknowledgement).evidence->>'native_gateway_id'
        AND (((execution).scope_type = (acknowledgement).scope_type
              AND (execution).scope_id = (acknowledgement).scope_id)
            OR ((acknowledgement).evidence->>'admission_authority' = 'apim_execution' AND EXISTS (
                SELECT 1 FROM budget_execution_dispatch dispatch
                JOIN budget_execution_attempt anchor ON anchor.dispatch_id = dispatch.id
                WHERE dispatch.id = (execution).dispatch_id
                    AND dispatch.caller_request_id = (execution).caller_request_id
                    AND anchor.scope_type = (acknowledgement).scope_type
                    AND anchor.scope_id = (acknowledgement).scope_id
                    AND anchor.period_start = (execution).period_start
                    AND anchor.admitted_at = (execution).admitted_at
                    AND anchor.model_id = (execution).model_id
                    AND anchor.caller_request_id = (execution).caller_request_id
                    AND anchor.gateway_profile_id = (execution).gateway_profile_id
                    AND anchor.native_gateway_id = (execution).native_gateway_id
                    AND anchor.correlation_id = (execution).correlation_id
                    AND dispatch.receipts @> jsonb_build_array(jsonb_build_object(
                        'identity', (execution).identity, 'epoch', (execution).epoch::text,
                        'reserved_tokens', (execution).reserved_tokens))
                    AND dispatch.receipts @> jsonb_build_array(jsonb_build_object(
                        'identity', anchor.identity, 'epoch', anchor.epoch::text,
                        'reserved_tokens', anchor.reserved_tokens))
            )));
$$;
CREATE FUNCTION enqueue_budget_execution_ack(execution budget_execution_attempt) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE acknowledgement billable_request_attempt;
BEGIN
    SELECT attempt.* INTO acknowledgement FROM billable_request_attempt attempt
    WHERE attempt.id::text = execution.caller_request_id AND budget_execution_ack_matches(execution, attempt);
    IF FOUND THEN
        PERFORM queue_budget_execution_evidence(execution, 'ack:' || acknowledgement.id::text,
            2::smallint, acknowledgement.actual_tokens, acknowledgement.updated_at);
    END IF;
END;
$$;
CREATE FUNCTION budget_execution_ack_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE execution budget_execution_attempt;
BEGIN
    IF NEW.state <> 'exact' OR NEW.actual_tokens IS NULL OR NEW.correlation_id IS NULL THEN RETURN NEW; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('execution-ack:' || NEW.id::text, 0));
    FOR execution IN SELECT attempt.* FROM budget_execution_attempt attempt
        WHERE attempt.caller_request_id = NEW.id::text AND budget_execution_ack_matches(attempt, NEW)
    LOOP
        PERFORM queue_budget_execution_evidence(execution, 'ack:' || NEW.id::text,
            2::smallint, NEW.actual_tokens, NEW.updated_at);
    END LOOP;
    RETURN NEW;
END;
$$;
CREATE TRIGGER budget_execution_ack_outbox AFTER INSERT OR UPDATE ON billable_request_attempt
FOR EACH ROW EXECUTE FUNCTION budget_execution_ack_changed();

CREATE VIEW budget_execution_conflicts AS
SELECT task.id AS task_id, evidence.id AS conflicting_evidence_id,
       task.total_tokens AS selected_tokens, evidence.total_tokens AS conflicting_tokens
FROM budget_settlement_task task
JOIN budget_execution_evidence evidence USING (scope_type, scope_id, period_start, attempt_key)
WHERE evidence.total_tokens <> task.total_tokens;

CREATE FUNCTION prepare_budget_execution_scope(kind TEXT, identity TEXT, month DATE)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE rollout budget_execution_rollout;
BEGIN
    SELECT * INTO rollout FROM budget_execution_rollout WHERE state = 'active' AND scope_sync_enabled;
    IF NOT FOUND THEN RETURN; END IF;
    IF kind = 'application' AND NOT EXISTS (
        SELECT 1 FROM gateway_application WHERE id::text = identity AND NOT system_managed
        AND gateway_profile_id = ANY(rollout.gateway_profile_ids)
    ) THEN RETURN; END IF;
    IF kind NOT IN ('person', 'application') THEN RAISE EXCEPTION 'Unsupported execution scope type'; END IF;
    INSERT INTO budget_execution_scope (
        scope_type, scope_id, period_start, epoch, state, admitted_after, legacy_fence_verified_at
    ) VALUES (kind, identity, month, rollout.epoch, 'prepared',
              GREATEST(rollout.admitted_after, month::timestamp AT TIME ZONE 'UTC'), rollout.legacy_fence_verified_at)
    ON CONFLICT (scope_type, scope_id, period_start) DO NOTHING;
END;
$$;
CREATE FUNCTION budget_execution_prepare_new_budget() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'token_budget' THEN
        IF NEW.scope_type = 'user' THEN
            PERFORM prepare_budget_execution_scope('person', NEW.scope_id, NEW.period_start);
        END IF;
    ELSIF TG_TABLE_NAME = 'gateway_application_budget' THEN
        PERFORM prepare_budget_execution_scope('application', NEW.application_id::text, NEW.period_start);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER budget_execution_prepare_person AFTER INSERT ON token_budget
FOR EACH ROW EXECUTE FUNCTION budget_execution_prepare_new_budget();
CREATE TRIGGER budget_execution_prepare_application AFTER INSERT ON gateway_application_budget
FOR EACH ROW EXECUTE FUNCTION budget_execution_prepare_new_budget();
CREATE FUNCTION budget_execution_prepare_observed_person() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.usage_domain = 'apim' AND position('@' IN NEW.user_id) > 1 THEN
        PERFORM prepare_budget_execution_scope('person', NEW.user_id,
            date_trunc('month', NEW.ts AT TIME ZONE 'UTC')::date);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER budget_execution_prepare_observed_person AFTER INSERT ON token_usage
FOR EACH ROW EXECUTE FUNCTION budget_execution_prepare_observed_person();