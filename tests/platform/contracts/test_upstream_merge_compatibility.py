from __future__ import annotations

from uuid import UUID

import pytest

from tests.backend.telemetry.test_processor import event
from turnstile_core.domain.control_plane import ModelCreateTarget
from turnstile_core.domain.data_access import DataAccessScope
from turnstile_core.domain.runtime_models import ModelInvocationResponse, PriceSource
from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_support import UsageDirectoryScope, UsageFilters


def test_public_pricing_publication_preserves_the_forks_default_source_contract() -> None:
    target = ModelCreateTarget(
        model_key="merge-example", display_name="Merge example", upstream_model_id="example",
    )
    assert target.price_source is None
    public = target.model_copy(update={"price_source": PriceSource.MODELS_DEV})
    assert (
        ModelCreateTarget.model_validate(public.model_dump()).price_source is PriceSource.MODELS_DEV
    )


def test_platform_trace_filters_keep_person_and_department_scope_separate() -> None:
    row = {"organization_id": "org-a", "department_id": "dept-a", "user_id": "person-a"}
    assert InMemoryRepository._matches_trace(row, UsageFilters(
        data_scope=DataAccessScope(user_ids=("person-a",)),
    ))
    assert not InMemoryRepository._matches_trace(row, UsageFilters(
        data_scope=DataAccessScope(user_ids=("person-b",)),
    ))
    assert InMemoryRepository._matches_trace(row, UsageFilters(
        allowed_department_ids=("dept-a",),
    ))
    assert not InMemoryRepository._matches_trace(row, UsageFilters(
        allowed_department_ids=("dept-b",),
    ))


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("fenced_probe", [False, True])
def test_probe_filter_preserves_user_and_directory_scopes(
    allowed: bool, fenced_probe: bool,
) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(
        organization_id="org-a", department_id="dept-a", user_id="person-a",
        model_id="model-a", request_source="apim",
    ))
    assert record is not None
    if fenced_probe:
        record = record.model_copy(update={
            "ingest_error": "budget_pre_dispatch_denial",
            "budget_admission": "budget_protocol_fenced",
            "user_id": "unattributed", "model_id": "unattributed",
            "request_source": "unattributed",
        })
    for filters in (
        UsageFilters(data_scope=DataAccessScope(
            user_ids=(record.user_id if allowed else "other",),
        )),
        UsageFilters(directory_scope=UsageDirectoryScope(
            statuses=("active",) if allowed else ("archived",),
            active_organizations=("org-a",), active_departments={"dept-a": "org-a"},
            archived_organizations=(), archived_departments=(), archived_users=(),
        )),
        UsageFilters(allowed_department_ids=("dept-a" if allowed else "dept-b",)),
    ):
        assert repository._matches(record, filters) is (allowed and not fenced_probe)
        sql, _ = PostgreSqlOpsDbProxy._filter_sql(filters)
        assert "budget_pre_dispatch_denial" in sql
        assert "budget_protocol_fenced" in sql


def test_invocation_response_retains_documented_selected_model_fields() -> None:
    payload = {
        "request_id": "unit-merge-response", "content": "OK", "provider": "Unit",
        "runtime": "Unit runtime", "model": "Unit model", "gateway": "apim",
        "latency_ms": 1, "usage": None,
    }
    selected = ModelInvocationResponse.model_validate({
        **payload, "selected_model_id": str(UUID(int=42)), "selected_model_name": "Selected",
    })
    assert selected.model_dump(mode="json")["selected_model_id"] == str(UUID(int=42))
    assert selected.model_dump()["selected_model_name"] == "Selected"
    legacy = ModelInvocationResponse.model_validate(payload)
    assert legacy.model_dump()["selected_model_id"] is None
    assert legacy.model_dump()["selected_model_name"] is None
