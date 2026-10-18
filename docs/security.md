# Security

## Secrets

Never commit `.env`, Azure parameter files with values, publish profiles, certificates, keys, tokens, connection strings, database dumps, or deployment packages. Use Key Vault and managed identity for Azure-hosted workloads.

Provider credentials are write-only through the API, encrypted before persistence, redacted from reads, and excluded from logs. APIM subscription-key reveal responses are Owner-only and use `Cache-Control: no-store`.

## Identity and authorization

- Microsoft Entra tokens are signature-, audience-, issuer-, and domain-validated.
- Application sessions are HTTP-only and secure in production.
- Governance mutations are Owner-only.
- Managed identities receive narrowly scoped data-plane and management-plane roles.
- APIM removes Turnstile-internal headers before forwarding requests to providers.

## Telemetry privacy

Usage events contain identifiers, status, latency, token counts, and pricing metadata. Prompt and completion content must not be emitted to Event Hubs, logs, metrics, or PostgreSQL.

## Pre-push review

1. Run a secret scanner over tracked files.
2. Review `git diff --cached`.
3. Confirm ignored local configuration is not staged.
4. Confirm generated packages and logs are absent.
5. Rotate any value that may have been exposed before relying on history cleanup.
