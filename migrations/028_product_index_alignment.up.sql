CREATE INDEX budget_reservation_finalization_scope_period_idx
    ON budget_reservation_finalization (
        scope_type, scope_id, period_start, reservation_created_at DESC
    );

CREATE UNIQUE INDEX self_hosted_gateway_provision_request_idx
    ON gateway_release_operation ((semantic_preview->>'request_id'))
    WHERE operation_kind = 'self_hosted_gateway_provision';