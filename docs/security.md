# Security

## Secrets

Never commit `.env`, Azure parameter files with values, publish profiles, certificates, keys, tokens, connection strings, database dumps, or deployment packages. Use Key Vault and managed identity for Azure-hosted workloads.

Provider credentials are write-only through the API, encrypted before persistence, redacted from reads, and excluded from logs. APIM subscription-key reveal responses are Owner-only and use `Cache-Control: no-store`.

## Identity and authorization

- Microsoft Entra tokens are signature-, audience-, issuer-, and domain-validated.
- Application sessions are HTTP-only and secure in production.
- Budget, model, gateway, account-link and organization-level mutations are Owner-only.
- Explicit department grants allow a Member to maintain only that department's personnel
  business profiles and teams. They do not grant global governance, login-account management,
  budget allocation, model assignment, connection management or gateway publication rights.
- A current or previously delegated Member does not inherit legacy global reads when grants
  are revoked. Scoped SQL, detail lookups, totals, histories and stored assistant/report scopes
  enforce the same department boundary; private client caches are invalidated on permission changes.
  The authenticated profile refreshes every 30 seconds in either workspace and on window focus.
  Global Application and GitHub Copilot views reject scoped Members until their own verified
  department-query boundaries exist; ordinary Members retain the prior policy.
- Ordinary Members retain the pre-existing read policy. This release does not introduce
  complete tenant isolation or change `owner/member` into a three-role login model.
- Managed identities receive narrowly scoped data-plane and management-plane roles.
- APIM removes Turnstile-internal headers before forwarding requests to providers.

In database-directory mode, Microsoft sign-in binds the signed `(cloud, tid, oid)` to an
account. An email collision with an existing password account or Owner requires explicit
administrator binding and cannot silently adopt its role. `ENTRA_ALLOWED_TENANT_IDS` can
add an exact tenant allow-list without widening the existing email-domain gate.
Directory sync and Microsoft login configuration are independent.

## Menu Permission Groups

Local `user`, `organization_admin`, `department_admin` and `team_admin` groups control
application navigation only. Administrator groups can coexist and menus are their union.
`menu_permission_groups` is authoritative; the singular field is a legacy projection.
Unassigned and unlinked accounts default to `user`; active
linked people supply the local group. Disabled/source-disabled people lose its menus.
Owner independently retains all applicable menus and is the only role allowed to change groups.
These names do not grant account roles, department capabilities, Graph consent or model access.

The server publishes the menu whitelist in login/current-profile responses. The client applies
it to navigation, search, direct routes, prefetching and assistant entry points, and refreshes
it on focus/every 30 seconds. Hiding a menu is not business API authorization: existing Owner
mutations, department-scoped reads, ordinary Member API read policy and APIM admission remain
unchanged. API callers must continue to rely on those independent checks.
Entra profile imports, job titles, team membership and legacy department grants never assign
local menu groups. A menu-only edit is audited and invalidates permission caches without
publishing a gateway identity update.

Directory connections store credential references, not secrets. Graph access is read-only,
uses fixed cloud endpoints, follows only same-cloud HTTPS paging links and encrypts delta
checkpoints with the existing credential key. Missing permissions, incomplete data and expired
cursors are not successful empty snapshots. Import and identity association do not create
Owners, model access or department grants. Large source-membership reductions require
separate review. Directory mutations are audited and preserve governance IDs and billing evidence.

## Telemetry privacy

Usage events contain identifiers, status, latency, token counts, and pricing metadata. Prompt and completion content must not be emitted to Event Hubs, logs, metrics, or PostgreSQL.

## Pre-push review

1. Run a secret scanner over tracked files.
2. Review `git diff --cached`.
3. Confirm ignored local configuration is not staged.
4. Confirm generated packages and logs are absent.
5. Rotate any value that may have been exposed before relying on history cleanup.
