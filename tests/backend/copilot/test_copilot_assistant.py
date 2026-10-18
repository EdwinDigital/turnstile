from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx

from backend.data_sources.github_copilot.assistant import (
    COPILOT_AGENT_ID,
    COPILOT_OWNER_PREFIX,
    BreakdownArguments,
    CopilotAssistantService,
)
from backend.data_sources.github_copilot.contracts import (
    CopilotBreakdownItem,
    CopilotBudgetRequestList,
    CopilotDashboard,
    CopilotStatus,
)
from backend.services.runtime_service import ModelRuntimeService
from turnstile_core.config import Settings
from turnstile_core.domain.assistant_models import AssistantAskRequest
from turnstile_core.integrations.gateway import GatewayRouter
from turnstile_core.persistence.in_memory import InMemoryRepository

USER_ID = uuid4()
USER_EMAIL = "member@example.com"


def dashboard() -> CopilotDashboard:
    return CopilotDashboard.model_validate(
        {
            "organization": "example-enterprise",
            "report_start_day": "2026-07-01",
            "report_end_day": "2026-07-28",
            "generated_at": "2026-07-29T00:00:00Z",
            "viewer_github_login": "alice",
            "can_view_members": False,
            "subscription": {
                "plan_type": "business",
                "seat_breakdown": {"total": 1},
                "estimated_monthly_seat_cost": 19,
                "price_per_seat": 19,
            },
            "totals": {
                "seats": 1,
                "active_users": 1,
                "interactions": 12,
                "generations": 20,
                "acceptances": 10,
                "acceptance_rate": 50,
                "loc_added": 100,
                "loc_deleted": 4,
                "ai_credits_used": 3,
                "gross_amount": 3,
                "net_amount": 3,
                "cli_prompt_tokens": 0,
                "cli_output_tokens": 0,
            },
            "daily": [],
            "models": [
                {
                    "key": "gpt-5.4",
                    "label": "gpt-5.4",
                    "interactions": 12,
                    "generations": 20,
                    "acceptances": 10,
                    "loc_added": 100,
                }
            ],
            "features": [],
            "languages": [],
            "ides": [],
            "members": [
                {
                    "login": "alice",
                    "plan_type": "business",
                    "budget_amount": 30,
                    "budget_consumed": 3,
                    "budget_remaining": 27,
                    "totals": {
                        "seats": 1,
                        "active_users": 1,
                        "interactions": 12,
                        "generations": 20,
                        "acceptances": 10,
                        "acceptance_rate": 50,
                        "loc_added": 100,
                        "loc_deleted": 4,
                        "ai_credits_used": 3,
                        "gross_amount": 3,
                        "net_amount": 3,
                        "cli_prompt_tokens": 0,
                        "cli_output_tokens": 0,
                    },
                }
            ],
            "warnings": [],
        }
    )


class StubCopilotService:
    def __init__(self) -> None:
        self.dashboard_reads = 0

    def status(self, user_id: UUID, role: str) -> CopilotStatus:
        assert user_id == USER_ID
        assert role == "member"
        return CopilotStatus(
            configured=True,
            oauth_configured=True,
            viewer_role="member",
            viewer_github_login="alice",
            connections=[],
        )

    async def dashboard(self, *, user_id: UUID, role: str) -> CopilotDashboard:
        assert user_id == USER_ID
        assert role == "member"
        self.dashboard_reads += 1
        return dashboard()

    def budget_requests(self, user_id: UUID, role: str) -> CopilotBudgetRequestList:
        assert user_id == USER_ID
        assert role == "member"
        return CopilotBudgetRequestList(
            items=[],
            can_review=False,
            pending_count=0,
            approved_count=0,
            rejected_count=0,
        )


def tool_call(name: str, call_id: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 4},
    }


def answer() -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": "Alice used 12 interactions."}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 8},
    }


def test_copilot_assistant_uses_only_read_tools_and_an_isolated_namespace() -> None:
    repository = InMemoryRepository()
    repository.gateways[0]["base_url"] = "https://gateway.example.test/turnstile/llm"
    sent: list[dict[str, Any]] = []
    responses = [
        tool_call("query_copilot_overview", "call-1"),
        tool_call("query_copilot_budget_requests", "call-2"),
        answer(),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        body["_headers"] = dict(request.headers)
        sent.append(body)
        return httpx.Response(200, json=responses.pop(0))

    runtime = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )
    copilot = StubCopilotService()
    service = CopilotAssistantService(repository, runtime, copilot)  # type: ignore[arg-type]

    reply = asyncio.run(
        service.ask(
            AssistantAskRequest(question="How is my Copilot usage?"),
            user_uuid=USER_ID,
            user_email=USER_EMAIL,
            user_name="Member",
            role="member",
        )
    )

    offered = {tool["function"]["name"] for tool in sent[0]["tools"]}
    assert offered == {
        "query_copilot_overview",
        "query_copilot_members",
        "query_copilot_breakdown",
        "query_copilot_budgets",
        "query_copilot_budget_requests",
    }
    assert all("create" not in name and "update" not in name for name in offered)
    assert sent[0]["_headers"]["x-request-source"] == "copilot-assistant"
    assert sent[0]["_headers"]["x-agent-id"] == COPILOT_AGENT_ID
    metadata = service._metadata(
        str(USER_ID),
        USER_EMAIL,
        "model-id",
        "model",
        reply.conversation_id,
        1,
    )
    assert metadata.usage_domain == "github_copilot"
    assert copilot.dashboard_reads == 1
    assert [step.tool for step in reply.steps] == [
        "query_copilot_overview",
        "query_copilot_budget_requests",
    ]

    owner = f"{COPILOT_OWNER_PREFIX}{USER_EMAIL}"
    stored = repository.get_conversation(UUID(reply.conversation_id), owner)
    assert stored is not None
    assert repository.get_conversation(UUID(reply.conversation_id), USER_EMAIL) is None
    assert service.list_conversations(USER_EMAIL)[0].owner_id == owner


def test_copilot_breakdown_tool_ranks_by_the_requested_metric() -> None:
    repository = InMemoryRepository()
    service = CopilotAssistantService(
        repository,
        ModelRuntimeService(repository, Settings()),
        StubCopilotService(),  # type: ignore[arg-type]
    )
    data = dashboard().model_copy(
        update={
            "models": [
                CopilotBreakdownItem(
                    key="interaction-leader",
                    label="Interaction leader",
                    interactions=100,
                    generations=20,
                    acceptances=10,
                    loc_added=50,
                ),
                CopilotBreakdownItem(
                    key="acceptance-leader",
                    label="Acceptance leader",
                    interactions=10,
                    generations=40,
                    acceptances=30,
                    loc_added=200,
                ),
            ]
        }
    )

    outcome = service._outcome(
        "query_copilot_breakdown",
        BreakdownArguments(dimension="model", metric="acceptances", limit=1),
        data,
        USER_ID,
        "member",
    )

    assert outcome.payload["items"][0]["key"] == "acceptance-leader"
