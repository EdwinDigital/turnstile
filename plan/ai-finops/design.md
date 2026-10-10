# AI FinOps And Report Center

The global sidebar contains a fixed **AI FinOps** module. **FinOps助手**
(FinOps Assistant) uses the existing `assistant` route. **报表中心** (Report Center)
uses the existing APIM-only `pinned-report` route and menu permission. Individual
report titles are no longer global navigation items.

Report Center follows the organization directory's master/detail interaction:
the searchable directory occupies the left pane and the selected report appears
on the right. The directory preserves stored report position, supports title and
description searches, and indicates the selected report. On mobile the directory
is above the viewer. Search does not discard the report being viewed.

Existing `?source=apim&page=pinned-report&chart=<report-id>` links remain valid.
Without `chart`, the first accessible report is selected. An unavailable explicit
ID shows a not-found/access-denied state rather than silently opening another
report. Browser back/forward restores report selection. Deleting the selected
report keeps Report Center open and selects the first remaining accessible report.

The assistant's save dialog now saves to Report Center. Directory loading occurs
only in Report Center or when opening the assistant's save dialog; the global
sidebar has no report-list query or asynchronous report group. Existing shared
TanStack Query keys keep viewer edits and directory titles consistent.

Report ownership, public visibility, backend authorization and `can_manage`
remain unchanged. Read-only reports do not expose mutation controls. The viewer
retains renaming, visibility, explicit data refresh, chart removal, reorder and
layout sizing. Directory deletion uses the existing API with a confirmation.
There are no new endpoints, permission groups, database tables or migrations.

Future report types should be registered inside Report Center, not inserted into
global navigation. Source-specific storage and access scopes must remain explicit;
this change does not add APIM reports to GitHub Copilot.

## Validation

- `tests/platform/frontend/test_report_center.py` runs production directory helpers
  for search, persisted ordering, explicit-ID selection and URL compatibility,
  plus source checks for cache sharing, mutation gates and localization.
- `test_sidebar_initial_layout.py` checks that navigation does not consume report
  data and the assistant's report query is disabled until its save dialog opens.
- Browser checks must distinguish real local API workflows from response fixtures
  used for delayed-loading, empty, missing-ID or failure-state visual coverage.
  Neither constitutes production deployment or paid assistant-model validation.

## Local Acceptance (2026-10-10)

- Frontend build, Ruff and mypy passed. The build retains the existing large-chunk
  warning.
- Final focused run passed 189 tests covering frontend, assistant behavior,
  API contracts, documentation and assistant-settings API. This is not the full
  repository suite.
- Playwright/Edge validated real authenticated local entry and report persistence:
  create two temporary reports, add a chart, rename, change visibility, refresh
  against actual empty telemetry, reload, confirm deletion and select the remaining
  report. The temporary reports were deleted; no usage records or model calls were
  created. Report-list state before and after was retained outside the repository.
- Browser response fixtures covered delayed lists with unchanged global navigation
  coordinates, search, back navigation, read-only controls, unavailable old links,
  empty lists and permission-error retry. Five locales, Light/Dark and desktop
  1440px/mobile 390px layouts passed without horizontal overflow or relevant
  console/page errors. These fixture checks are UI evidence, not backend E2E.
- Full-suite PostgreSQL fixtures could not initialize because local System V
  shared-memory segments were exhausted (`No space left on device`). This is an
  environment limitation, not a passed full-suite result. No running PostgreSQL
  service was stopped and no system IPC settings were changed.
- This change has not been committed or deployed. No migrations or backend API
  contracts were changed by this Report Center work.

## Release Preflight (2026-10-10)

The user authorized committing and releasing all pending usage-filter and AI FinOps
changes. The failed-test IPC segments created by this task were individually checked
for zero attachments and exited creator processes, then removed. Running services and
unrelated IPC were preserved. The complete PostgreSQL-backed repository suite then
passed: **1758 passed, 7 skipped**. Ruff, mypy (257 files), frontend build, XML,
Bicep compilation and OpenAPI validation passed with existing warnings retained.
Both local browser suites passed again. Release and deployed acceptance are recorded
separately after execution; this preflight is not production verification.
