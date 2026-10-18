CREATE TABLE gateway_application_tags (
    application_id UUID PRIMARY KEY REFERENCES gateway_application(id) ON DELETE RESTRICT,
    tags JSONB NOT NULL CHECK (jsonb_typeof(tags) = 'object')
);