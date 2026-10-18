from __future__ import annotations

from fastapi import HTTPException

from turnstile_core.config import Settings
from turnstile_core.domain.enterprise import enterprise_catalog
from turnstile_core.domain.models import EnterpriseEntity
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    InvocationMetadata,
    ModelInvocationRequest,
    TrafficExecutionItem,
    TrafficGenerationPlan,
    TrafficGenerationRequest,
    TrafficGenerationResult,
    TrafficPlanItem,
)

from .runtime_service import ModelRuntimeService

PROMPTS = (
    "Summarize the operational risk in one short sentence.",
    "Return one concise recommendation for reducing model cost.",
    "Classify this request as low, medium, or high priority.",
    "Give one short action item for the project owner.",
)


class TrafficGenerator:
    def __init__(self, service: ModelRuntimeService, settings: Settings) -> None:
        self._service = service
        self._settings = settings

    def plan(self, request: TrafficGenerationRequest) -> TrafficGenerationPlan:
        registry = self._service.registry()
        selected = [
            model
            for model in registry.models
            if model.enabled and (not request.model_ids or model.id in request.model_ids)
            and "image_generation" not in model.capabilities
        ]
        if not selected:
            raise HTTPException(status_code=409, detail="No enabled models match the request")
        request_count = len(selected) * request.requests_per_model
        if request_count > self._settings.traffic_max_requests:
            raise HTTPException(
                status_code=422, detail="Traffic request count exceeds server limit"
            )
        max_output = min(request.max_output_tokens, self._settings.traffic_max_output_tokens)
        budget = min(request.budget_usd, self._settings.traffic_generation_budget_usd)
        # Deliberately the seeded catalog, not the merged one. Generated calls are real and
        # billable, so attributing them to a person discovered from live telemetry would
        # falsify that person's usage and consume their budget.
        catalog = enterprise_catalog()
        departments_by_id = {item.id: item for item in catalog.departments}
        agents_by_project: dict[str, list[EnterpriseEntity]] = {}
        for agent in catalog.agents:
            agents_by_project.setdefault(str(agent.parent_id), []).append(agent)

        items: list[TrafficPlanItem] = []
        for sequence in range(1, request_count + 1):
            model = selected[(sequence - 1) % len(selected)]
            project = catalog.projects[(sequence - 1) % len(catalog.projects)]
            department = departments_by_id[str(project.parent_id)]
            project_agents = agents_by_project[project.id]
            agent = project_agents[(sequence - 1) % len(project_agents)]
            user = catalog.users[(sequence - 1) % len(catalog.users)]
            prompt = PROMPTS[(sequence - 1) % len(PROMPTS)]
            ceiling = round(
                (len(prompt) / 4 + max_output)
                / 1_000_000
                * self._settings.traffic_price_ceiling_per_million_usd,
                8,
            )
            items.append(
                TrafficPlanItem(
                    sequence=sequence,
                    model_id=model.id,
                    model_name=model.model_key,
                    organization_id=catalog.organizations[0].id,
                    organization_name=catalog.organizations[0].name,
                    department_id=department.id,
                    department_name=department.name,
                    project_id=project.id,
                    project_name=project.name,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    user_id=user.id,
                    user_name=user.name,
                    workflow=f"{project.id}-validation",
                    prompt=prompt,
                    max_output_tokens=max_output,
                    conservative_cost_ceiling=ceiling,
                )
            )
        total_ceiling = round(sum(item.conservative_cost_ceiling for item in items), 8)
        if total_ceiling > budget:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Conservative traffic ceiling ${total_ceiling:.4f} exceeds "
                    f"budget ${budget:.4f}"
                ),
            )
        return TrafficGenerationPlan(
            dry_run=request.dry_run,
            request_count=len(items),
            budget_usd=budget,
            conservative_cost_ceiling=total_ceiling,
            items=items,
        )

    def execute(self, request: TrafficGenerationRequest, role: str) -> TrafficGenerationResult:
        plan = self.plan(request)
        if plan.dry_run:
            return TrafficGenerationResult(
                planned_requests=plan.request_count,
                completed_requests=0,
                successful_requests=0,
                failed_requests=0,
                total_estimated_cost=0,
                stopped_reason="dry_run",
                items=[],
            )
        results: list[TrafficExecutionItem] = []
        total_cost = 0.0
        stopped_reason: str | None = None
        registry = self._service.registry()
        runtimes = {model.id: model.runtime_id for model in registry.models}
        # Display values, so they carry names. Sending the runtime UUID here would be the
        # same defect the invocation console had: a successful call is labelled from the
        # route, but a denied one traces whatever the caller asserted.
        runtime_names = {runtime.id: runtime.name for runtime in registry.runtimes}
        for item in plan.items:
            if total_cost + item.conservative_cost_ceiling > plan.budget_usd:
                stopped_reason = "budget_limit"
                break
            invocation = ModelInvocationRequest(
                runtime_id=runtimes[item.model_id],
                model_id=item.model_id,
                metadata=InvocationMetadata(
                    organization_id=item.organization_id,
                    organization=item.organization_name,
                    department_id=item.department_id,
                    department=item.department_name,
                    project_id=item.project_id,
                    project=item.project_name,
                    agent_id=item.agent_id,
                    agent=item.agent_name,
                    user_id=item.user_id,
                    user=item.user_name,
                    workflow=item.workflow,
                    model_id=str(item.model_id),
                    model=str(item.model_id),
                    runtime=runtime_names[runtimes[item.model_id]],
                    request_source="enterprise-traffic-generator",
                    run_id=f"traffic-{item.sequence:04d}",
                    turn_index=1,
                ),
                messages=[ChatMessage(role="user", content=item.prompt)],
                max_output_tokens=item.max_output_tokens,
                temperature=0,
            )
            try:
                response = self._service.invoke(invocation)
                cost = response.estimated_cost or item.conservative_cost_ceiling
                total_cost += cost
                results.append(
                    TrafficExecutionItem(
                        sequence=item.sequence,
                        model_id=item.model_id,
                        request_id=response.request_id,
                        correlation_id=response.correlation_id,
                        status_code=200,
                        estimated_cost=cost,
                    )
                )
            except HTTPException as error:
                results.append(
                    TrafficExecutionItem(
                        sequence=item.sequence,
                        model_id=item.model_id,
                        status_code=error.status_code,
                        estimated_cost=0,
                        error_message=str(error.detail)[:2000],
                    )
                )
        successful = sum(item.status_code < 400 for item in results)
        return TrafficGenerationResult(
            planned_requests=plan.request_count,
            completed_requests=len(results),
            successful_requests=successful,
            failed_requests=len(results) - successful,
            total_estimated_cost=round(total_cost, 8),
            stopped_reason=stopped_reason,
            items=results,
        )
