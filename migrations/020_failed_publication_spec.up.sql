DROP INDEX gateway_publication_one_reusable_spec_idx;

CREATE UNIQUE INDEX gateway_publication_one_reusable_spec_idx
    ON gateway_publication (gateway_profile_id, desired_spec_sha256)
    WHERE status NOT IN ('failed', 'superseded', 'rolled_back');