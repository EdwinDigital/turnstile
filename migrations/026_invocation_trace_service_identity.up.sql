CREATE OR REPLACE VIEW public.platform_invocation_usage_match AS
SELECT attempt.request_id, matched.usage_id
FROM public.platform_invocation_attempt attempt
JOIN LATERAL (
    SELECT min(usage.id) AS usage_id
    FROM public.token_usage usage
    LEFT JOIN public.token_usage_gateway_attribution gateway ON gateway.usage_id = usage.id
    LEFT JOIN public.token_usage_application_attribution application ON application.usage_id = usage.id
    WHERE usage.request_id = attempt.request_id
      AND usage.usage_domain = 'apim'
      AND usage.user_id = attempt.snapshot->'metadata'->>'user_id'
      AND usage.model_id = attempt.snapshot->>'model_id'
     AND attempt.completion->>'gateway_correlation_id' IS NOT NULL
     AND usage.correlation_id = attempt.completion->>'gateway_correlation_id'
      AND (
          (gateway.usage_id IS NULL AND attempt.snapshot->>'gateway_deployment_id' IS NULL)
          OR (
              gateway.apim_target_id::text = attempt.snapshot->>'apim_target_id'
              AND gateway.native_gateway_id = attempt.snapshot->>'native_gateway_id'
              AND gateway.gateway_deployment_id::text = attempt.snapshot->>'gateway_deployment_id'
              AND (
                  lower(gateway.apim_service_id) = lower(attempt.snapshot->>'apim_service_id')
                  OR lower(gateway.apim_service_id) = lower(regexp_replace(
                      attempt.snapshot->>'apim_service_id',
                      '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft[.]ApiManagement/service/([^/]+)/?$',
                      '\1', 'i'))
                  OR lower(regexp_replace(
                      gateway.apim_service_id,
                      '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft[.]ApiManagement/service/([^/]+)/?$',
                      '\1', 'i')) = lower(attempt.snapshot->>'apim_service_id')
              )
          )
      )
      AND (attempt.snapshot->>'application_id' IS NULL
           OR application.application_id::text = attempt.snapshot->>'application_id')
    HAVING count(*) = 1
) matched ON TRUE;