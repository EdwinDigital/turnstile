from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from turnstile_core.domain.enterprise import (
    configured_invocation_testers,
)
from turnstile_core.domain.models import (
    AuditFinding,
    AuditFindingListResponse,
    AuditFindingUpdate,
    DistributionResponse,
    EnterpriseEntityCatalog,
    ExecutiveOverviewResponse,
    OptimizationEvent,
    OptimizationEventCreate,
    OptimizationEventListResponse,
    OverviewResponse,
    PageInfo,
    RunDetail,
    RunListResponse,
    RunSummary,
    TrendResponse,
    UsageAnomaly,
    UsageAnomalyListResponse,
    UsageRequestDetail,
    UsageRequestListResponse,
    UsageRequestSummary,
)
from turnstile_core.persistence.repository import UsageFilters
from turnstile_core.services.directory_catalog import catalog_for

from .dependencies import Repository
from .session import (
    Config,
    CurrentSession,
    OwnerSession,
    require_allowed_write_origin,
    require_authenticated_session,
)

router = APIRouter(
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ]
)


def usage_filters(
    identity: CurrentSession,
    organization_id: str | None = None,
    department_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    model_id: str | None = None,
    user_id: str | None = None,
    runtime: Annotated[list[str] | None, Query()] = None,
    status_code: Annotated[int | None, Query(ge=100, le=599)] = None,
) -> UsageFilters:
    return UsageFilters(
        organization_id=organization_id,
        department_id=department_id,
        project_id=project_id,
        agent_id=agent_id,
        model_id=model_id,
        user_id=user_id,
        runtime=tuple(runtime) if runtime else None,
        status_code=status_code,
        allowed_department_ids=identity.directory_department_ids,
    )


UsageFilterSet = Annotated[UsageFilters, Depends(usage_filters)]


@router.get("/api/v1/enterprise/entities", response_model=EnterpriseEntityCatalog)
def get_enterprise_entities(
    repository: Repository,
    settings: Config,
    identity: CurrentSession,
) -> EnterpriseEntityCatalog:
    catalog = catalog_for(repository, settings)
    if identity.directory_department_ids is not None:
        allowed = set(identity.directory_department_ids)
        departments = [row for row in catalog.departments if row.id in allowed]
        organization_ids = {row.parent_id for row in departments}
        projects = [row for row in catalog.projects if row.parent_id in allowed]
        project_ids = {row.id for row in projects}
        catalog = catalog.model_copy(
            update={
                "organizations": [
                    row for row in catalog.organizations if row.id in organization_ids
                ],
                "departments": departments,
                "projects": projects,
                "agents": [row for row in catalog.agents if row.parent_id in project_ids],
                "users": [row for row in catalog.users if row.parent_id in allowed],
            }
        )
    return catalog.model_copy(
        update={
            "invocation_testers": configured_invocation_testers(
                catalog, settings.delegated_invocation_tester_ids
            )
        }
    )


@router.get("/api/v1/enterprise/query-entities", response_model=EnterpriseEntityCatalog)
def get_query_entities(
    repository: Repository,
    settings: Config,
    identity: CurrentSession,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
) -> EnterpriseEntityCatalog:
    if from_.tzinfo is None or to.tzinfo is None or to <= from_ or to - from_ > timedelta(days=366):
        raise HTTPException(
            status_code=422, detail="Select an aware query window of at most 366 days"
        )
    current = get_enterprise_entities(repository, settings, identity)
    historical = repository.historical_entities(from_, to, filters)
    merged = {}
    for key in ("organizations", "departments", "projects", "agents", "users"):
        by_id = {row.id: row for row in getattr(current, key)}
        by_id.update({row.id: row for row in getattr(historical, key)})
        merged[key] = sorted(by_id.values(), key=lambda row: (row.name.casefold(), row.id))
    # Query-only snapshots never become selectable invocation identities.
    merged["invocation_testers"] = []
    return EnterpriseEntityCatalog.model_validate(merged)


@router.get(
    "/api/v1/observability/executive-overview",
    response_model=ExecutiveOverviewResponse,
)
def get_executive_overview(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
) -> ExecutiveOverviewResponse:
    if to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")
    return ExecutiveOverviewResponse.model_validate(
        repository.executive_overview(from_, to, filters)
    )


@router.get("/api/v1/observability/distribution", response_model=DistributionResponse)
def get_distribution(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    dimension: Literal[
        "organization", "department", "project", "agent", "model", "user", "runtime"
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    split_by: Literal["organization", "department", "project", "agent", "model", "user", "runtime"]
    | None = None,
) -> DistributionResponse:
    return DistributionResponse.model_validate(
        {
            "from": from_,
            "to": to,
            "dimension": dimension,
            "items": repository.distribution(from_, to, dimension, filters, limit, split_by),
        }
    )


@router.get("/api/v1/observability/requests", response_model=UsageRequestListResponse)
def list_usage_requests(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UsageRequestListResponse:
    return UsageRequestListResponse(
        items=[
            UsageRequestSummary.model_validate(row)
            for row in repository.list_usage_requests(from_, to, filters, limit)
        ],
        page=PageInfo(next_cursor=None),
    )


@router.get("/api/v1/observability/requests/{request_id}", response_model=UsageRequestDetail)
def get_usage_request(
    request_id: str,
    repository: Repository,
    identity: CurrentSession,
) -> UsageRequestDetail:
    row = repository.get_usage_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Usage request not found")
    if (
        identity.directory_department_ids is not None
        and row["department_id"] not in identity.directory_department_ids
    ):
        raise HTTPException(status_code=404, detail="Usage request not found")
    return UsageRequestDetail.model_validate(row)


@router.get("/api/v1/observability/anomalies", response_model=UsageAnomalyListResponse)
def list_usage_anomalies(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UsageAnomalyListResponse:
    return UsageAnomalyListResponse(
        items=[
            UsageAnomaly.model_validate(row)
            for row in repository.list_usage_anomalies(from_, to, filters, limit)
        ]
    )


@router.get("/api/v1/observability/overview", response_model=OverviewResponse)
def get_overview(
    repository: Repository,
    identity: CurrentSession,
    timezone: str = "UTC",
) -> dict[str, object]:
    if identity.directory_department_ids is not None:
        raise HTTPException(status_code=403, detail="Use scoped executive overview")
    return repository.overview(timezone)


@router.get("/api/v1/observability/trends", response_model=TrendResponse)
def get_trends(
    repository: Repository,
    filters: UsageFilterSet,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    interval: Literal["hour", "day", "week"],
    group_by: Literal[
        "organization",
        "department",
        "project",
        "agent",
        "user",
        "model",
        "runtime",
        "workflow",
        "team",
        "none",
    ],
    timezone: str = "UTC",
) -> dict[str, object]:
    return repository.trends(from_, to, interval, group_by, timezone, filters)


@router.get("/api/v1/observability/runs", response_model=RunListResponse)
def list_runs(
    repository: Repository,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunListResponse:
    if identity.directory_department_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="Run collections are not available to scoped viewers",
        )
    rows = repository.list_runs(from_, to, limit)
    summaries = [{key: value for key, value in row.items() if key != "turns"} for row in rows]
    return RunListResponse(
        items=[RunSummary.model_validate(row) for row in summaries],
        page=PageInfo(next_cursor=None),
    )


@router.get("/api/v1/observability/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: str,
    repository: Repository,
    identity: CurrentSession,
) -> RunDetail:
    if identity.directory_department_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="Run collections are not available to scoped viewers",
        )
    row = repository.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunDetail.model_validate(row)


@router.get("/api/v1/observability/audit-findings", response_model=AuditFindingListResponse)
def list_findings(
    repository: Repository,
    identity: CurrentSession,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditFindingListResponse:
    if identity.directory_department_ids is not None:
        raise HTTPException(status_code=403, detail="Use scoped anomaly queries")
    rows = repository.list_findings(from_, to, limit)
    return AuditFindingListResponse(
        items=[AuditFinding.model_validate(row) for row in rows], page=PageInfo(next_cursor=None)
    )


@router.patch("/api/v1/observability/audit-findings/{finding_id}", response_model=AuditFinding)
def update_finding(
    finding_id: UUID,
    update: AuditFindingUpdate,
    repository: Repository,
    identity: OwnerSession,
) -> AuditFinding:
    row = repository.update_finding(finding_id, update)
    if row is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return AuditFinding.model_validate(row)


@router.get(
    "/api/v1/observability/optimization-events",
    response_model=OptimizationEventListResponse,
)
def list_optimizations(
    repository: Repository,
    identity: CurrentSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OptimizationEventListResponse:
    if identity.directory_department_ids is not None:
        raise HTTPException(status_code=403, detail="Optimization history is Owner-scoped")
    rows = repository.list_optimizations(limit)
    return OptimizationEventListResponse(
        items=[OptimizationEvent.model_validate(row) for row in rows],
        page=PageInfo(next_cursor=None),
    )


@router.post(
    "/api/v1/observability/optimization-events", response_model=OptimizationEvent, status_code=201
)
def create_optimization(
    create: OptimizationEventCreate,
    repository: Repository,
    identity: OwnerSession,
) -> OptimizationEvent:
    return OptimizationEvent.model_validate(repository.create_optimization(create))
