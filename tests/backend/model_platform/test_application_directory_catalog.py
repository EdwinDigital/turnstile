from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from tests.backend.model_platform.control_plane_support import APIM_ID
from tests.backend.model_platform.test_application_access import _discovery
from tests.backend.model_platform.test_application_provisioning import UnitLedger
from turnstile_core.domain.application_access import (
    GatewayApplicationBudgetUpdate,
    GatewayApplicationOrganizationUpdate,
    application_id_for,
)
from turnstile_core.domain.enterprise import enterprise_catalog
from turnstile_core.domain.models import EnterpriseEntity, EnterpriseEntityCatalog
from turnstile_core.integrations.ledger import (
    LedgerStore,
    LedgerSyncService,
    TableStorageLedger,
    application_map_partition_key,
    application_partition_key,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_applications import PostgreSqlApplicationRepositoryMixin
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.application_provisioning_ledger import prepare_application_ledger


@pytest.fixture
def custom_catalog(monkeypatch: pytest.MonkeyPatch) -> EnterpriseEntityCatalog:
    base = enterprise_catalog()
    catalog = base.model_copy(update={
        "organizations": [*base.organizations, EnterpriseEntity(
            id="custom-org", name="Custom Org",
        )],
        "departments": [*base.departments, EnterpriseEntity(
            id="custom-dept", name="Custom Department", parent_id="custom-org",
        )],
        "users": [*base.users, EnterpriseEntity(
            id="custom@example.test", name="Custom Person", parent_id="custom-dept",
        )],
    })
    for module in (
        "turnstile_core.services.application_access",
        "turnstile_core.services.application_provisioning_ledger",
        "turnstile_core.persistence.directory_catalog",
    ):
        monkeypatch.setattr(module + ".catalog_for", lambda *args, **kwargs: catalog)
    return catalog


def assigned_application() -> tuple[InMemoryRepository, ApplicationAccessService]:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=False)
    service.sync_discovery(_discovery("custom-client"), "owner@example.test")
    service.update_application_organization(
        application_id_for(APIM_ID, "custom-client"),
        GatewayApplicationOrganizationUpdate(
            organization_id="custom-org", department_id="custom-dept",
            person_id="custom@example.test",
        ),
        "owner@example.test",
    )
    service.update_application_budget(
        application_id_for(APIM_ID, "custom-client"),
        GatewayApplicationBudgetUpdate(
            token_limit=100_000, tokens_per_minute=10_000, enforce=True,
            warning_threshold_percent=80,
        ),
        "owner@example.test",
    )
    return repository, service


def test_application_assignment_and_summary_use_the_authoritative_directory(
    custom_catalog: EnterpriseEntityCatalog,
) -> None:
    repository, service = assigned_application()
    result = service.application(application_id_for(APIM_ID, "custom-client"))
    assert result.organization_id == "custom-org"
    assert result.organization_name == "Custom Org"
    assert result.department_id == "custom-dept"
    assert result.department_name == "Custom Department"
    assert result.person_id == "custom@example.test"
    assert result.person_name == "Custom Person"
    assert repository.token_budgets == {}
    with pytest.raises(ValueError, match="Unknown application organization"):
        service.update_application_organization(
            result.id, GatewayApplicationOrganizationUpdate(organization_id="missing"),
            "owner@example.test",
        )


@pytest.mark.parametrize("initial_admission", [False, True])
def test_application_ledger_uses_the_same_directory_without_resetting_consumption(
    custom_catalog: EnterpriseEntityCatalog, initial_admission: bool,
) -> None:
    repository, _ = assigned_application()
    application = application_id_for(APIM_ID, "custom-client")
    ledger = UnitLedger()
    partition = application_partition_key(application, datetime.now(UTC).strftime("%Y-%m"))
    ledger.rows[(partition, "C")] = {"ConfirmedUsed": 123}
    ledger.rows[("unrelated", "C")] = {"ConfirmedUsed": 456}
    budgets = deepcopy(repository.gateway_application_budgets)
    if initial_admission:
        prepare_application_ledger(
            repository, cast(TableStorageLedger, ledger), APIM_ID, application,
        )
    else:
        sync = LedgerSyncService(repository, cast(LedgerStore, ledger))
        assert sync.project_application_mappings(
            application,
        ) == 1
    mapping = ledger.rows[(application_map_partition_key(APIM_ID), "custom-client")]
    assert mapping["OrganizationId"] == "custom-org"
    assert mapping["OrganizationName"] == "Custom Org"
    assert mapping["DepartmentId"] == "custom-dept"
    assert mapping["DepartmentName"] == "Custom Department"
    assert ledger.rows[(partition, "C")] == {"ConfirmedUsed": 123}
    assert ledger.rows[("unrelated", "C")] == {"ConfirmedUsed": 456}
    assert repository.gateway_application_budgets == budgets


@pytest.mark.parametrize("department_organization", ["custom-org", "other-org"])
def test_sql_assignment_validates_the_database_directory_under_the_directory_lock(
    department_organization: str,
) -> None:
    application = uuid4()
    connection = MagicMock()

    def execute(statement: str, parameters: object = None) -> MagicMock:
        cursor = MagicMock()
        row: dict[str, object] | None
        if "to_regclass" in statement:
            row = {"present": "directory_control_state"}
        elif "SELECT source FROM directory_control_state" in statement:
            assert "FOR UPDATE" in statement
            row = {"source": "database"}
        elif "SELECT id, department_id, owner_id" in statement:
            row = {"id": application, "department_id": None, "owner_id": None}
        elif "SELECT tags" in statement:
            row = {"tags": {}}
        elif "SELECT id FROM directory_organization" in statement:
            assert parameters == ("custom-org",)
            assert "status='active'" in statement and "FOR SHARE" in statement
            row = {"id": "custom-org"}
        elif "SELECT organization_id,kind,status" in statement:
            assert parameters == ("custom-dept",)
            row = {"organization_id": department_organization,
                   "kind": "department", "status": "active"}
        else:
            row = None
        cursor.fetchone.return_value = row
        return cursor

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlApplicationRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        if department_organization != "custom-org":
            with pytest.raises(ValueError, match="does not belong"):
                repository.update_gateway_application_organization(
                    application, "custom-org", "owner", department_id="custom-dept",
                    update_department=True,
                )
            assert not any("INSERT" in call.args[0] or "UPDATE gateway_application" in call.args[0]
                           for call in connection.execute.call_args_list)
        else:
            result = repository.update_gateway_application_organization(
                application, "custom-org", "owner", department_id="custom-dept",
                update_department=True,
            )
            assert result == {"organization_id": "custom-org", "department_id": "custom-dept",
                              "owner_id": None}
    assert "to_regclass" in connection.execute.call_args_list[0].args[0]
