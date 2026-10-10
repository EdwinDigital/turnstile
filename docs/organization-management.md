# Organization Management

## Runtime Boundaries

The APIM workspace maintains organizations, departments, teams and personnel in PostgreSQL.
Contact email and display-name edits do not change governance IDs, prior-period budget parents,
historical usage or model policies. Account binding is explicit and one-to-one. Department
administrators are scoped Members, not a third global account role.

The GitHub Copilot workspace hides this menu and search entry. Its enterprise team and cost-center
features retain their own data source. Switching workspaces does not delete directory data or
stop background jobs.

Readiness requires schema 012, the configured directory authority and compatible packages.
An HTTP health response alone does not prove directory, Graph or gateway readiness.

`/api/v1/enterprise/entities` remains the active assignment/invocation projection.
`/api/v1/enterprise/query-entities` is a separate, scoped, query-only projection for an aware
window up to 366 days. It retains original unknown/archived IDs and latest in-window
attribution without editing historical requests. The query client does not rewrite legacy
user IDs into today's governance identities.

## Empty Installation

1. Use a dedicated new environment and private deployment state. Review the normal what-if.
2. Explicitly opt in with `directoryEmptyInitializationAllowed: true`. The deployment runner
   supplies `directoryExpectedCoreDigest`; sync and projection remain disabled.
3. Keep the standard `backend.migrate`, `backend.bootstrap`, `backend.api` startup sequence.
   Bootstrap creates only the first Owner; initialization creates no customer organizations,
   departments, users, budgets, projects or model grants.
4. Sign in as the Owner, create the real hierarchy and explicitly link existing accounts.
   Configure budgets and models through their existing Owner workflows.
5. Restart and repeat initialization only to validate idempotency, not to reset business data.

## Existing Installation

Keep the source `legacy` and new write/worker flags disabled until the approved cutover.
Do not rerun APIM bootstrap or change the evidence cutoff, encryption key, subscription keys,
ledger partitions or Event Hub checkpoint.

1. Record application states, exact mounted packages and SHA-256, assets, app settings,
   migration checksums, APIM revision and billing/model-policy baselines. Back up PostgreSQL
   and private deployment state, and verify the backup can be read/restored in an approved
   isolated environment. Burstable PostgreSQL does not imply an on-demand backup is available.
2. Stop directory/governance writers for a maintenance window. Apply pending additive migrations
   using `python -m backend.migrate`; never edit 001-011 or apply schema through a second runner.
3. Deploy the same compatible API, Telemetry and Control-plane candidate in legacy mode.
   Each package contains `directory-runtime-manifest.json`; verify target, protocol and
   matching core digest, and confirm all expected Functions index.
4. Plan and approve the real legacy directory plus actual governance references. The plan
   contains personal information and must remain in a 0700 directory as a 0600 file.
5. Apply and verify the saved plan. Durable staging precedes atomic materialization.
   Interrupted materialization rolls back business rows; retrying the same plan resumes
   its staging checkpoint rather than cloning identities or repeating audit.
6. With all three writers stopped, merge precise current settings using
   `infra/directory-settings-upgrade.bicep`. Review a no-Delete what-if limited to the three
   exact appsetting resources. Set the same database source and core digest; keep Graph and
   projection disabled. Read back the complete setting maps and preserve unrelated values.
7. Capture private three-package maintenance evidence, verify and activate by database CAS.
   Restore each application's recorded running/stopped state. Verify real login, source
   normalization, scopes, histories, budgets, models and directory CRUD.

Use the authorized environment's `DATABASE_URL`, never a connection string in command arguments:

```bash
python -m scripts.directory_upgrade plan --output <private-directory>/plan.json
python -m scripts.directory_upgrade apply --plan <private-directory>/plan.json --approve
python -m scripts.directory_upgrade verify --run-id <run-id>
python -m scripts.directory_upgrade activate --run-id <run-id> --approve \
  --runtime-manifest <private-directory>/runtime-evidence.json
```

The runtime evidence is an operator-verified readback, not an application-generated assertion.
It contains `captured_at` (UTC, within 15 minutes), the plan's `instance_digest`,
`protocol_version: 1`, `backup_verified: true`, `maintenance_confirmed: true`, and exactly
three `applications` entries named `api`, `telemetry`, `control-plane`. Each entry records
`target`, `state: Stopped`, the mounted `core_digest`, `configured_source: database`,
the matching `configured_core_digest`, and the exact `package_sha256`. Do not manufacture
evidence from intended settings without verifying the deployed packages and Azure state.

When `uv` wheel downloads fail, the packager's pip fallback requires an existing Python
3.11+ interpreter with pip. Set `TURNSTILE_BUILD_PYTHON` when the default interpreter has
no pip; it installs only into the private staged target and still checks every package
against `uv.lock`. An older host interpreter may incorrectly evaluate dependency markers
for the Linux/Python 3.11 target and is not accepted.

Plans bind the database instance, migration checksums, code digest, legacy source digest and
billing/account baseline. Drift requires new investigation; never edit the stored baseline to
make verification pass. Unknown historical identities remain candidates, not guessed employees.

### Compatible Runtime Updates

After database activation, routine updates must not rerun backfill or activation against a
different code digest. Back up the current compatible package trio, settings and database,
then build all three new packages with an identical core digest. Verify all applied migration
checksums and the isolated backup restore before publication.

Stop the three writers, temporarily disable timer functions and merge the new
`DIRECTORY_EXPECTED_CORE_DIGEST` into every current setting map. Review a what-if limited to
those appsettings. Publish and read back all three compatible packages before restoring the
recorded timer settings and application states. Never leave an old package running with a
new expected digest. A package/configuration failure must restore the matching previous digest,
compatible trio and complete settings; retain the active database directory and additive schema.
Verify real login, directory data, disabled identities, billing baselines and Function indexing
after both rollback and republish. Restoring a database backup is not part of this drill.

## Synchronization And Login

The Control-plane `synchronize_directory` timer is independently disabled by default.
Connections select a tenant/cloud and read-only credential, then map Group object IDs to existing
departments/teams. Direct and transitive memberships are explicit; hidden-membership Groups
require a separately approved permission policy and are currently rejected.

Full and delta jobs produce durable staged previews. Selected Groups use complete member
snapshots even in delta mode because Group deltas alone cannot prove nested membership or
user-profile completeness. Cursors advance only after reviewed application; failed paging,
missing permissions and expired cursors do not remove people. A full preview can recover an
expired delta cursor. Same-email collisions and multi-department membership require resolution.
Missing source members require approval, with an additional confirmation above 20% reduction.
Source-disabled and manually disabled people remain in history. Manual disable overrides
are never cleared by synchronization; source status remains authoritative for source disables.
Previews expire after 30 minutes and must be regenerated before application. The applying worker
rechecks the approving account's enabled Owner role. Source `last_seen_at` records the read time,
not a delayed approval time; directory review cannot make an old Graph snapshot look fresh.
Reads process bounded segments and persist encrypted resource/page progress, source rows and
analysis cursors. A recovered lease resumes that job. Missing-source members are staged only
after every source resource is complete. Application uses one set-based transaction, preserving
membership, profile and source-status audit snapshots; a failed application advances no cursor.

Application read permissions and tenant administrator consent are external prerequisites,
not Bicep side effects. Import does not automatically create login accounts or grant Owner,
department administration, budgets or model access. An explicit source-person association and
optional login-identity authorization are separate audited choices.

Existing email-only Microsoft accounts require explicit `(cloud, tid, oid)` association in
database-directory mode. Configure client ID, redirect URI, domains and optional login tenant
allow-list separately. Directory synchronization is not a completed SSO deployment.

## Transfers And Rollback

### Directory Identity APIM Upgrade

Directory policy upgrades have an independent `directory-identity-v1` plan/journal directory
beside the private deployment state. They reuse the existing ledger endpoint and table.
The upgrade may only clone the selected API revision, replace its parent policy and create
the corresponding infrastructure release. It cannot modify existing operations or initialize
image policies, and rejects Delete or writes to any other resource in what-if.

```bash
python -m scripts.directory_gateway_upgrade plan --subscription <subscription> \
  --parameters <private-parameters> --state <private-state>
python -m scripts.directory_gateway_upgrade prepare --subscription <subscription> \
  --parameters <private-parameters> --state <private-state> --yes
python -m scripts.directory_gateway_upgrade promote --subscription <subscription> \
  --parameters <private-parameters> --state <private-state> --yes \
  --candidate-evidence <private-identity-probes>
python -m scripts.directory_gateway_upgrade rollback --subscription <subscription> \
  --parameters <private-parameters> --state <private-state> --yes
```

Drain publication/rollback work and stop API/Control-plane for the maintenance window.
If source code changes after a failed prepare, do not delete or overwrite the bound plan,
journal or failed candidate. Use a new `--plan-id <reviewed-run-name>` on `plan`, then use
that same ID on `prepare`, `promote` and `rollback`. Each run has a separate private evidence
directory; all runs share the environment lock and still verify the current source revision.
Review the new what-if before any writes. The previous candidate remains noncurrent.
`prepare` verifies the complete candidate but does not promote it. Run real signed employee-token
probes against the candidate with the configured publisher/employee client and scope. Read back
the identity projection and all existing budget partitions. Do not use a CLI user's Graph token
or mocked HTTP responses as evidence of employee gateway admission.

The private 0600 candidate evidence records UTC `captured_at` (within 15 minutes),
`version: directory-identity-v1`, the exact `candidate_fingerprint`,
`real_signed_employee_token: true`, `identity_projection_readback_verified: true`, and
these `cases`: `unmapped_identity: 403`, `disabled_identity: 403`, `expired_mapping: 503`,
`header_spoofing_rejected: true`, `stable_governance_id: true`, `budget_ledger_unchanged: true`.
These values must describe actual probe results. Preparation, ARM policy validation or unit
tests alone cannot manufacture them.

The upgrade leaves application states unchanged. Rollback re-reads the original and current
revisions and refuses a changed source or later operation/policy drift. Preserve the independent
journal; an image upgrade's successful journal is not directory identity evidence.

### Month Boundary

Directory-mode text and image BFF requests seal a separate immutable attribution admission
before provider dispatch. The snapshot shares the directory control/person locking order with
transfer activation. It is not a token measurement and creates no `Q/C/M/R` ledger write.
A due scheduled transfer blocks new BFF admission until activation or conflict resolution;
old metadata cannot pass after a successful move.
Incomplete or cross-boundary prior calls block activation rather than allowing old attribution
to be resealed in the new month. Terminal markers are separate immutable records.

Transfers remain unavailable while `person_admission_mode` is `unverified`. After independently
reading back the deployed API package and complete APIM parent policy, record private 0600 evidence:

```bash
python -m scripts.directory_upgrade configure-admission --approve \
  --admission-evidence <private-directory>/admission-evidence.json
```

Evidence contains UTC `captured_at` (within 15 minutes), current `instance_digest`,
`expected_directory_version`, matching `core_digest`, `api_directory_admission_protocol: 1`,
the exact 64-character `apim_policy_fingerprint`, and `person_admission_mode`.
`bff_only` requires verified `employee_token_enabled: false`. `identity_v1` requires
`directory_identity_policy_protocol: 1`, `identity_gateway_probes_verified: true`, and current
Table projection readback. These are actual readback/probe assertions, not intended configuration.
The gateway identity snapshot's validity is capped by each scheduled transfer's UTC boundary,
so old employee mappings expire before the new assignment can be admitted.
Identity Table snapshots expire within 5 minutes and are refreshed by the independent projection
timer. Failed refresh cannot preserve an enabled mapping indefinitely. The directory policy seals
the employee budget partition and reservation timestamp to `requestStarted`, including calls
whose metadata lookup crosses a UTC month boundary.

Owner-scheduled transfers take effect at a future UTC month boundary, after budget inheritance
and before ledger projection. Activation revalidates revisions, active target units, target
allowances and the absence of current-period admission. A late or conflicting transfer is
recorded as conflicted rather than rewriting existing billing evidence. Previous months retain
their original budget parents and requests retain admission-time attribution.
Transfer previews include a digest of the personnel/target revisions and future budgets;
HTTP scheduling requires that digest. Future budgets created or changed after approval cause
activation conflicts. Owners can inspect plans and cancel scheduled/conflicted transfers;
cancellation preserves the current assignment and budgets.

Historical identity candidates are visible to Owners. Ignore is explicit and audited.
Linking requires the same stable governance ID; merging different historical IDs remains
a separately reviewed identity migration, not a contact-email edit.

Stop new writers and independent sync/projection workers before rollback. Keep additive tables,
staging, audit and migration ledger. After new IDs are used, do not switch back to a seed-only
package or legacy authority. Use a verified compatible package trio and preserve recorded
application states. A database restore is a separate approved recovery operation; it is not a
normal package rollback.

Static employee APIM attribution does not automatically follow directory edits or disables.
Treat it as pending alignment until the separately gated dynamic identity policy and projection
have been deployed and verified. Never infer gateway revocation from a successful directory save.
