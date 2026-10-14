# Cross-platform image bridge lifecycle

## Approved scope

Keep one user plugin for macOS and Windows. Replace the POSIX-only leading-slash
check with the host's `node:path.isAbsolute()`, retaining canonical `realpath()`
comparison and ownership checks. Relative, malformed, missing, unrelated and
disabled registrations must not enable the bridge. Signed-out or foreign-user
accounts must remain rejected. Requests and the existing shutdown timer must use
the same check.

Update the download generator and locally installed plugin to 0.4.5. Do not change
Magpie source, OAuth bootstrap identity, subscriptions, budgets, APIM, or Agent
configuration. Do not deploy, push changes, or perform paid generation requests.

## Alternatives

- Native absolute-path recognition (selected): one package, minimal change,
  preserves filesystem-based verification on each supported host.
- Separate OS packages: duplicates lifecycle/security code without fixing an
  OS-specific dependency; unnecessary for this path handling issue.
- Remove the registration check: unsafe because disabled/removed plugins could
  retain an image capability; rejected.

## Verification

- Node regression tests for registration, account ownership, malformed paths,
  spaces/non-ASCII directories and directory links.
- Isolated native Bun bridge tests with fake credentials and a loopback platform:
  authenticated discovery/MCP, stable lifetime, and disable/remove/logout cleanup.
- Existing plugin, pricing, quota, authorization and download tests.
- Local Magpie restart; read-only text model, bridge health, image model and MCP
  discovery checks. Never copy private configuration into the repository.

Actual macOS execution is not available on this Windows host. POSIX path contracts
and platform-neutral code/tests are not a substitute for a macOS runtime test.

## Implementation sequence

1. Add a failing native-path regression test.
2. Extract the shared enabled check and replace its path predicate.
3. Update version/package regression assertions and integration coverage.
4. Run isolated tests before patching the installed plugin and restarting Magpie.
5. Record verified results and remaining limitations here.
