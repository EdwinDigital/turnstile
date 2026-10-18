ALTER TABLE gateway_release_operation
    DROP CONSTRAINT gateway_release_operation_operation_kind_check;

ALTER TABLE gateway_release_operation
    ADD CONSTRAINT gateway_release_operation_operation_kind_check CHECK (
        operation_kind IN (
            'rollback', 'integrity_check', 'gc_plan', 'application_sync',
            'application_provision', 'target_inventory_sync', 'apim_onboarding'
        )
    );