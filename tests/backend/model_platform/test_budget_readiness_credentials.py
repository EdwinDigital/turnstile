from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

import backend.services.runtime_service as runtime_module
from backend.services.budget_diagnostics import BudgetDiagnostics
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_budget_diagnostics import configured as configured
from tests.backend.model_platform.test_gateway_targets import TARGET
from turnstile_core.domain.budget_delegation import BudgetDelegationReadiness


def _readiness(
    service: BudgetDiagnostics,
    implementation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], BudgetDelegationReadiness]:
    monkeypatch.setattr(
        service.runtime, "_decrypt_route",
        lambda route: ModelRuntimeService._decrypt_route(service.runtime, route),
    )
    if implementation == "diagnostics":
        return lambda: service.readiness(TARGET, "unit-owner")
    client = service.client_factory()
    monkeypatch.setattr(
        runtime_module, "AtomicTableLedger", lambda *_a, **_kw: service.store_factory(),
    )
    monkeypatch.setattr(runtime_module, "ManagedIdentityTokenProvider", lambda: service.tokens)
    monkeypatch.setattr(httpx, "Client", lambda **_kw: client)
    return lambda: service.runtime.check_budget_delegation(TARGET, "unit-owner")


def _platform_settings(service: BudgetDiagnostics, **changes: Any) -> None:
    route = service._route(TARGET)
    settings = service.settings.model_copy(update={
        "apim_gateway_url": str(route["gateway_base_url"]) + "/",
        "apim_dashboard_subscription_key": SecretStr("unit-dashboard-key"),
        "apim_probe_subscription_key": SecretStr("unit-probe-key-must-not-be-used"),
        **changes,
    })
    service.settings = settings
    service.runtime._settings = settings


@pytest.mark.parametrize("implementation", ["runtime", "diagnostics"])
@pytest.mark.parametrize("mode", ["prepared", "paused", "active"])
def test_budget_readiness_uses_platform_default_subscription_without_inference(
    configured: tuple[Any, ...], implementation: str, mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fence, requests, _write, _applications, repository, *_ = configured
    fence["mode"] = mode
    _platform_settings(service)
    check = _readiness(service, implementation, monkeypatch)
    result = check()
    assert result.ready and result.protocol_mode == mode
    assert [item.actual_status for item in result.checks] == (
        [503, 503] if mode == "paused" else [200, 401, 403]
    )
    assert len(requests) == (2 if mode == "paused" else 3)
    assert all(
        request.method == "GET"
        and request.url.path.endswith("/v1/models")
        and request.headers["ocp-apim-subscription-key"] == "unit-dashboard-key"
        and "authorization" not in request.headers
        for request in requests
    )
    assert not repository.invocation_attempts and not repository.usage_records
    assert not any(row.get("credential_ciphertext") for row in repository.gateways)
    if mode == "paused":
        assert result.verification is not None
        assert result.verification.model_ids == [_write.model_id]


@pytest.mark.parametrize("implementation", ["runtime", "diagnostics"])
def test_budget_readiness_preserves_explicit_gateway_subscription(
    configured: tuple[Any, ...], implementation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fence, requests, _write, _applications, repository, *_ = configured
    _platform_settings(service)
    gateway = next(row for row in repository.gateways if row["id"] == TARGET)
    gateway["credential_ciphertext"] = service.runtime._cipher.encrypt("unit-explicit-key")
    check = _readiness(service, implementation, monkeypatch)
    assert check().ready
    assert all(
        request.headers["ocp-apim-subscription-key"] == "unit-explicit-key"
        for request in requests
    )


@pytest.mark.parametrize("implementation", ["runtime", "diagnostics"])
@pytest.mark.parametrize("defect", [
    "missing", "different_origin", "different_api_path", "provider_only", "bearer", "wrong_header",
])
def test_budget_readiness_never_substitutes_untrusted_or_upstream_credentials(
    configured: tuple[Any, ...], implementation: str, defect: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fence, requests, _write, _applications, repository, *_ = configured
    _platform_settings(service)
    gateway = next(row for row in repository.gateways if row["id"] == TARGET)
    if defect == "different_origin":
        _platform_settings(service, apim_gateway_url="https://another.example.test/unit/llm")
    elif defect == "different_api_path":
        endpoint = service._route(TARGET)["gateway_base_url"]
        _platform_settings(service, apim_gateway_url=endpoint + "/other")
    elif defect in {"missing", "provider_only"}:
        _platform_settings(service, apim_dashboard_subscription_key=None)
        if defect == "provider_only":
            for provider in repository.providers:
                provider["credential_ciphertext"] = service.runtime._cipher.encrypt(
                    "unit-upstream-key-must-not-be-used",
                )
    elif defect == "bearer":
        gateway["credential_ciphertext"] = service.runtime._cipher.encrypt("unit-explicit-key")
        gateway["auth_type"] = "bearer"
    else:
        gateway["config"] = {"header_name": "api-key"}
    check = _readiness(service, implementation, monkeypatch)
    with pytest.raises(HTTPException) as failure:
        check()
    assert failure.value.status_code == (503 if implementation == "runtime" else 409)
    assert failure.value.headers == {
        "X-Turnstile-Budget-Readiness-Stage": "subscription_credential",
    }
    assert not requests and not repository.invocation_attempts


@pytest.mark.parametrize("implementation", ["runtime", "diagnostics"])
@pytest.mark.parametrize("stage", [
    "ledger_fence_read", "managed_identity_token", "subscription_credential",
])
def test_budget_readiness_diagnostic_stage_does_not_disclose_exception_details(
    configured: tuple[Any, ...], implementation: str, stage: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    service, _fence, requests, _write, _applications, _repository, store = configured
    _platform_settings(service)
    check = _readiness(service, implementation, monkeypatch)
    failure = RuntimeError("unit-private-key-and-sensitive-url")
    if stage == "ledger_fence_read":
        store.read_entity.side_effect = failure
    elif stage == "managed_identity_token":
        service.tokens.token.side_effect = failure
    else:
        def fail_decryption(_route: dict[str, Any]) -> dict[str, Any]:
            raise failure

        monkeypatch.setattr(service.runtime, "_decrypt_route", fail_decryption)
    with pytest.raises(HTTPException) as result:
        check()
    assert result.value.status_code == 503
    assert result.value.headers == {"X-Turnstile-Budget-Readiness-Stage": stage}
    assert "unit-private" not in str(result.value.detail) + caplog.text
    assert f"stage={stage} error_type=RuntimeError" in caplog.text
    assert not requests


@pytest.mark.parametrize("implementation", ["runtime", "diagnostics"])
def test_budget_readiness_requires_registered_revision_list(
    configured: tuple[Any, ...], implementation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fence, requests, *_ = configured
    fence["gateways"]["managed"] = "candidate"
    _platform_settings(service)
    check = _readiness(service, implementation, monkeypatch)
    with pytest.raises(HTTPException):
        check()
    service.tokens.token.assert_not_called()
    assert not requests
