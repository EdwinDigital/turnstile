import subprocess
import sys
import typing
from datetime import timedelta
from inspect import signature
from pathlib import Path
from typing import get_args, get_origin
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

from functions.telemetry.function_app import (
    app,
    process_usage_events,
    reconcile_stream_usage,
    sync_budget_ledger,
    telemetry_health,
)
from turnstile_core.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def test_metadata_indexing_does_not_import_backend_modules() -> None:
    script = """
import builtins

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "backend" or name.startswith("backend."):
        raise AssertionError(f"backend import during metadata indexing: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
from functions.telemetry import function_app

assert len(function_app.app.get_functions()) == 4
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_trigger_binding_names_match_function_parameters() -> None:
    for function in app.get_functions():
        trigger = function.get_trigger()
        parameters = signature(function.get_user_function()).parameters

        assert trigger is not None
        assert trigger.name in parameters


def test_event_hub_batch_annotation_is_available_at_runtime() -> None:
    user_function = process_usage_events.build().get_user_function()
    annotation = signature(user_function).parameters["events"].annotation

    assert annotation == typing.List[func.EventHubEvent]  # noqa: UP006
    assert get_origin(annotation) is list
    assert get_args(annotation) == (func.EventHubEvent,)


def test_telemetry_health_proves_the_python_worker_is_running() -> None:
    response = telemetry_health.build().get_user_function()(None)

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_body() == b'{"status": "ok"}'


@pytest.mark.parametrize(
    "workspace,enabled", [(None, True), ("workspace", False), ("workspace", True)]
)
@pytest.mark.parametrize("fails", (False, True))
def test_ledger_timer_only_enables_evidence_when_configured(
    workspace: str | None, enabled: bool, fails: bool,
) -> None:
    settings = Settings.model_construct(
        ledger_sync_enabled=True,
        ledger_table_endpoint="https://ledger.example.com",
        log_analytics_workspace_id=workspace,
        reconciliation_enabled=enabled,
    )
    repository = MagicMock()
    repository.roll_forward_budgets.return_value = None
    repository.roll_forward_gateway_application_budgets.return_value = 0
    with (
        patch("turnstile_core.config.get_settings", return_value=settings),
        patch("turnstile_core.persistence.factory.create_repository", return_value=repository),
        patch("turnstile_core.integrations.ledger.TableStorageLedger") as store,
        patch("turnstile_core.integrations.ledger.LedgerSyncService") as service,
        patch(
            "turnstile_core.integrations.reconciliation.LogAnalyticsReservationTerminalLog"
        ) as log,
    ):
        if fails:
            service.return_value.run.side_effect = RuntimeError("ledger failure")
            with pytest.raises(RuntimeError, match="ledger failure"):
                sync_budget_ledger.build().get_user_function()(None)
        else:
            sync_budget_ledger.build().get_user_function()(None)
        repository.close.assert_called_once_with()
        assert log.call_count == (1 if workspace and enabled else 0)
        assert service.call_args.args == (repository, store.return_value.__enter__.return_value)
        assert service.call_args.kwargs["terminal_log"] == (
            log.return_value if workspace and enabled else None
        )
        assert service.call_args.kwargs["recovery_lag"] == timedelta(minutes=10)
        assert service.call_args.kwargs["finalization_lag"] == timedelta(hours=24)


@pytest.mark.parametrize("enabled,endpoint", [(False, "https://ledger.example.com"), (True, None)])
def test_ledger_timer_closes_repository_on_early_return(
    enabled: bool, endpoint: str | None,
) -> None:
    repository = MagicMock()
    repository.roll_forward_budgets.return_value = None
    repository.roll_forward_gateway_application_budgets.return_value = 0
    settings = Settings.model_construct(ledger_sync_enabled=enabled, ledger_table_endpoint=endpoint)
    with (
        patch("turnstile_core.config.get_settings", return_value=settings),
        patch("turnstile_core.persistence.factory.create_repository", return_value=repository),
    ):
        sync_budget_ledger.build().get_user_function()(None)
    repository.close.assert_called_once_with()


@pytest.mark.parametrize("fails", (False, True))
def test_event_batch_closes_repository_without_swallowing_processing_failure(fails: bool) -> None:
    repository = MagicMock()
    event = MagicMock()
    event.get_body.return_value = b'{"model":"test"}'
    with (
        patch("turnstile_core.config.get_settings", return_value=Settings.model_construct()),
        patch("turnstile_core.persistence.factory.create_repository", return_value=repository),
        patch("turnstile_core.ingestion.processor.UsageProcessor") as processor,
    ):
        if fails:
            processor.return_value.process.side_effect = RuntimeError("batch failure")
            with pytest.raises(RuntimeError, match="batch failure"):
                process_usage_events.build().get_user_function()([event])
        else:
            process_usage_events.build().get_user_function()([event])
        processor.return_value.process.assert_called_once_with({"model":"test"})
    repository.close.assert_called_once_with()


@pytest.mark.parametrize("fails", (False, True))
def test_reconciliation_closes_repository(fails: bool) -> None:
    repository = MagicMock()
    settings = Settings.model_construct(
        reconciliation_enabled=True, log_analytics_workspace_id="workspace",
    )
    with (
        patch("turnstile_core.config.get_settings", return_value=settings),
        patch("turnstile_core.persistence.factory.create_repository", return_value=repository),
        patch("turnstile_core.integrations.reconciliation.ReconciliationService") as service,
        patch("turnstile_core.integrations.reconciliation.CacheReadSyncService"),
        patch("turnstile_core.integrations.reconciliation.LogAnalyticsGatewayUsageLog"),
        patch("turnstile_core.integrations.reconciliation.LogAnalyticsCacheReadLog"),
    ):
        if fails:
            service.return_value.run.side_effect = RuntimeError("reconciliation failure")
            with pytest.raises(RuntimeError, match="reconciliation failure"):
                reconcile_stream_usage.build().get_user_function()(None)
        else:
            reconcile_stream_usage.build().get_user_function()(None)
    repository.close.assert_called_once_with()
