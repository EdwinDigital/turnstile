CREATE INDEX pinned_chart_owner_position_idx
    ON pinned_chart (owner_id, position, created_at);

CREATE UNIQUE INDEX pinned_chart_owner_query_idx
    ON pinned_chart (owner_id, (chart->'query'));

CREATE INDEX budget_reservation_finalization_scope_period_idx
    ON budget_reservation_finalization (
        scope_type, scope_id, period_start, reservation_created_at DESC
    );

CREATE UNIQUE INDEX self_hosted_gateway_provision_request_idx
    ON gateway_release_operation ((semantic_preview->>'request_id'))
    WHERE operation_kind = 'self_hosted_gateway_provision';