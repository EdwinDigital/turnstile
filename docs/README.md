# Turnstile documentation

## Version scope

**v1.1 is the current public release; v1.2 is in preparation.** The v1.2 documentation below
describes implemented workflows and prerequisites, not a declaration of release readiness.
Model availability still requires complete protocol installation and end-to-end acceptance.

| v1.2 task | Start here | Important boundary |
| --- | --- | --- |
| Configure list prices, discounts and context tiers | [Pricing](configuration.md#context-tier-pricing) | Unknown thresholds and missing prices are not zero; historical requests retain their price snapshots. |
| Register and inspect an APIM target | [Registered targets](configuration.md#registered-apim-targets) | Registration references an existing Azure service; it does not create one. |
| Create or associate a Self-hosted Gateway | [Create a Gateway](configuration.md#create-a-self-hosted-gateway-from-turnstile) | An APIM Gateway configuration is not a running container; HTTPS, persistent proxy configuration and token renewal remain operator responsibilities. |
| Diagnose a Connection | [Connection diagnostics](configuration.md#connection-diagnostics) | Planning is non-inference; a confirmed check may incur provider charges. |
| Select a Gateway and inspect a failed invocation | [Platform invocation trace](configuration.md#platform-invocation-trace) | A request-attempt record is not billing evidence; unknown usage must remain unknown. |
| Understand a disabled model switch | [Availability](configuration.md#versioned-model-availability) and [troubleshooting](troubleshooting.md#model-switch-is-disabled-or-unverified) | Accepted intent is not verified Gateway enforcement; the v1.2 availability rollout remains pending acceptance. |
| Prepare an existing installation | [v1.2 preparation](deployment.md#prepare-for-v12) | Review application, migration and Gateway protocol changes separately; retain existing state and history. |

## Operator references

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Deployment](deployment.md)
- [Testing](testing.md)
- [End-to-End Validation](e2e-validation.md)
- [Security](security.md)
- [Troubleshooting](troubleshooting.md)

The root [README](../README.md) is the primary onboarding document. The [website user manual](https://turn-stile.com/docs)
provides task-oriented UI guidance. These repository pages contain maintainer and operator details;
environment-specific deployment history does not belong in either public documentation surface.
