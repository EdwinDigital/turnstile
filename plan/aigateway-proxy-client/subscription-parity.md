# Subscription plugin parity

Status: implemented and locally validated; online release pending. Not installed.

## Scope

Bring portable user-plugin fixes into the subscription plugin without changing its
APIM-key authorization, exported application/subscription/gateway binding, model
snapshot boundary, or another provider's state. Previous user-image pricing changes
in this worktree are retained. The follow-up request authorizes publishing to the
existing online test environment and committing/pushing all reviewed changes to
the user-owned repository. Installed-plugin updates, paid inference, budget edits,
permission changes and migrations remain outside scope. Online acceptance is
recorded separately from implementation and local checks.

## Audit

Compared the current user implementation and the history at `67b530e` (namespace,
prices and curves), `010d0f8`/`1c2f822` (personal control and icons), `e63465b`
(archive root), `b97ca72`/`10d3efd` (paths, native images and lifecycle),
`10fb56f`/`b10115a` (response aliases and helper isolation), `dd68660` (personal
USD/delegation) and the user 0.5.1 image-pricing patch. These locate source changes;
they are not evidence that any current installed or deployed package contains them.

| Capability | Subscription baseline | Action |
| --- | --- | --- |
| Readable namespace, ZIP root, offline icons, safe one-time import | Present | Preserve and regress |
| Four platform token prices and manual-price precedence | Present | Reuse for text and independent image provider |
| Published and authorized image catalog | Missing | Export image entries separately, including prices and aliases; support image-only subscriptions |
| Native images/MCP, Windows paths, ownership and lifecycle | Missing | Reuse user bridge/native-registration engine with explicit subscription mode |
| Response model aliases, split UTF-8 SSE, Messages | Missing alias adapter | Reuse exact-match streaming adapter; preserve tools, IDs, usage and cancellation |
| Explicit refresh and current-key dispatch | Missing refresh; loader captures old key | Refresh snapshot intersection without importing/rotating keys; re-read current authorization per request |
| Existing API account identity on explicit actions | Key callbacks lose top-level account names in Magpie | Stage full API auth via the host SDK and let its normal settlement reuse the account; real CLI verifies unchanged slots, key, name and manual image price |
| URL/credential boundaries and maintenance workers | Partial | Reject query/fragment and forged delegation headers; both worker flags prevent import, companions and discovery |
| Personal OAuth/profile/default subscription resolution | Different authority | Do not copy; keep existing subscription identity and key selection |
| Personal quota curves, USD local switch and budget download gate | No authenticated subscription allowance endpoint | Do not manufacture a personal/shared allowance or weaken APIM enforcement |

## Contracts

- A new subscription download remains version-1 config-compatible and retains the
  provider namespace. Version 0.2.0 includes separate `models`, `images` and exact
  `aliases`; discovery can remove or update bundled entries but never add an ID.
- A successful empty discovery is authoritative. Network failures use only the
  last accepted intersection, not a wider stale provider list. Failed/denied
  explicit refresh preserves stored login and never silently reimports a key.
  A corrupt saved intersection fails closed; several different saved keys make
  the explicit action refuse to guess (normal selected-account model refresh
  remains available). Logout or a changed key during discovery aborts auth completion.
- Images use a loopback companion and random local nonce. APIM keys are read from
  the current, exact saved account slot at invocation, not passed through CLI,
  MCP descriptors, ready/runtime files or URLs. The descriptor binds the canonical
  installed plugin, configured HTTPS gateway, subscription and snapshot IDs.
- No image capability or personal OAuth is created for subscription mode. Request
  forwarding uses only the configured APIM endpoint and subscription header;
  revocation/quotas/model authorization remain authoritative at APIM.
- Image registration, price import, conflicts/manual-disable preservation, stale
  lock recovery and cleanup reuse the user engine. User-mode behavior and existing
  private paths/manifests remain backward compatible. Subscriber state is separate.
- Personal allowance features remain explicitly unsupported without a trustworthy
  subscription-scoped API. No zero/fake counters, platform user impersonation or
  invented allowance is exposed.

## Verification

Backend: authorized text/image exports, prices/aliases, image-only download,
publication bindings, empty/foreign/revoked scopes, version and complete ZIP.
Node: both existing plugin suites plus key changes/logout, bounded discovery and
refresh, model aliases and hostile URLs/headers, both maintenance workers, shared
native ownership and subscription lifecycle. Real Magpie/Bun uses isolated config,
a temporary trusted loopback HTTPS upstream for subscriptions and no paid inference.
Its actual third CLI action verifies refresh without adding an unnamed account,
and the image-only subscription case has no conversation model as a hidden prerequisite.
Source lint/types and package
checks are distinct from online/download/client acceptance, which is not performed.

Fresh release preflight: 158 related Python tests and 107 Node tests pass; the Node
run has no skips and includes the installed Magpie/Bun binaries against isolated
simulated upstreams. Full Ruff and strict Mypy (376 source files), frontend build,
OpenAPI lint and `git diff --check` pass. OpenAPI lint reports 49 warnings; the
frontend build retains its large-chunk warning. These are not paid-inference,
native Windows-device, online browser or financial-settlement acceptance results.

The earlier `/tmp` evidence was removed by the user. This run recreates checks in
the private persistent directory
`/Users/edwin/Development/turnstile/.turnstile/deployments/subscription-parity-20261018/`.
No secret, runtime credential, private download or complete settings file belongs
in the public commit. Submitting an upstream PR or merging main is not implied by a push
to the existing `origin/codex/aigateway-proxy-client` branch.
