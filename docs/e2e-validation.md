# End-to-End Validation

Use this checklist for each release. Record resource identifiers and request IDs in a private deployment record, not in the repository.

## Environment

- [ ] New or intentionally empty Turnstile PostgreSQL database
- [ ] Fresh dependency installation from lockfiles
- [ ] Infrastructure preview reviewed
- [ ] Turnstile resources deployed successfully
- [ ] No Foundry project or provider model created by the deployment

## Platform bootstrap

- [ ] Apply `001_initial_schema`
- [ ] Re-run the migration command and confirm zero pending migrations
- [ ] Start the API and frontend
- [ ] Create the first password account with `python -m backend.accounts <email> --role owner`
- [ ] Sign in through the real web page with that email and password
- [ ] Confirm `/api/v1/auth/me` reports `role=owner` and `method=password`
- [ ] Confirm health, authentication, registry, and empty-state pages

## Customer-owned model onboarding

- [ ] Add a connection to an existing Foundry project through the authenticated web UI
- [ ] Add a model that references an existing provider deployment through the authenticated web UI
- [ ] Preserve Playwright evidence for both submissions; direct API calls do not satisfy these two checks
- [ ] Configure an APIM native backend pool if multiple equivalent runtimes exist
- [ ] Publish and verify the gateway release
- [ ] Make one small attributed invocation
- [ ] Confirm status, runtime, model, token usage, cost, and correlation ID in Request Trace
- [ ] Confirm no prompt or completion content is persisted

## Governance

- [ ] Configure an application budget
- [ ] Configure model access
- [ ] Verify an allowed request
- [ ] Verify a denied request with the expected reason
- [ ] Verify Owner-only application key actions without logging a key

## Browser acceptance

- [ ] Desktop screenshot reviewed
- [ ] 390 px mobile screenshot reviewed
- [ ] No horizontal overflow
- [ ] Loading, empty, error, disabled, hover, and focus states reviewed

## Result record

| Field | Value |
| --- | --- |
| Date | 2026-08-28 |
| Commit | Initial repository commit |
| Clean dependency install | Passed from an exported Git index tree with `uv sync --frozen` and `npm ci` |
| Automated validation | 606 tests passed in the working tree; Ruff, mypy, frontend build, Bicep, OpenAPI, and APIM XML passed; clean-tree rerun remains pending |
| Local process smoke | Clean-tree API, Vite frontend, and Vite `/health` proxy returned HTTP 200 in explicit demo mode; not persistence evidence |
| Infrastructure preview | Subscription what-if passed: 62 Create, 2 Unsupported, 0 Modify/Delete; unsupported types were Storage Account and Event Hubs |
| Infrastructure deployment | Fresh Azure platform is live, but it was assembled during engineering validation; the new `scripts.deploy` workflow has not yet completed from a clean clone in a second empty resource group |
| Application deployment | API, telemetry Function, control-plane Function, observer, and APIM publication are live in the engineering environment; this is not yet evidence for the self-service command |
| Connection/model/pool validation | Boundary connection and model were created through authenticated APIs and returned a real HTTP 200 inference; authenticated Playwright has inspected but not submitted the frontend creation flow, so the frontend onboarding gate remains open |
| Known limitations | See root README |

Do not mark a row passed without evidence from the real configured environment. Local unit tests and mocks are not E2E evidence.
