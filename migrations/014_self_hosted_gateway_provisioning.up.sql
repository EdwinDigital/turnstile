ALTER TABLE gateway_release_operation
    DROP CONSTRAINT gateway_release_operation_operation_kind_check;
ALTER TABLE gateway_release_operation
    ADD CONSTRAINT gateway_release_operation_operation_kind_check
    CHECK (operation_kind IN (
        'rollback', 'integrity_check', 'gc_plan', 'application_sync',
        'application_provision', 'target_inventory_sync', 'apim_onboarding',
        'self_hosted_gateway_provision'
    ));

CREATE UNIQUE INDEX gateway_provision_request_idx
    ON gateway_release_operation (gateway_profile_id, (semantic_preview->>'request_id'))
    WHERE operation_kind = 'self_hosted_gateway_provision';

ALTER TABLE gateway_release_operation
    ADD CONSTRAINT gateway_release_operation_target_key UNIQUE (id, gateway_profile_id);

ALTER TABLE gateway_deployment
    ADD COLUMN provisioning_operation_id UUID,
    ADD CONSTRAINT gateway_deployment_provision_target_fk
        FOREIGN KEY (provisioning_operation_id, apim_target_id)
        REFERENCES gateway_release_operation(id, gateway_profile_id) ON DELETE RESTRICT,
    ADD CONSTRAINT gateway_deployment_provision_kind_check
        CHECK (provisioning_operation_id IS NULL OR kind = 'self_hosted');