from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.http.dependencies import get_repository
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from tests.platform.api.api_support import _usage_record
from tests.platform.api.test_directory_api import directory_http as directory_http
from turnstile_core.domain.directory import AdministratorsWrite, TeamsWrite, UnitWrite
from turnstile_core.domain.menu_permissions import (
    ALL_MENUS,
    MenuPermissionGroup,
    PermissionPolicyWrite,
)
from turnstile_core.services.directory_catalog import directory_store
from turnstile_core.services.directory_service import DirectoryService


def test_owner_policy_atomic_save_conflict_and_existing_session_revocation(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    configuration = client.get("/api/v1/permission-management").json()
    assert len(configuration["groups"]) == 4
    groups = configuration["groups"]
    groups["department_admin"].remove("organization-management")
    write = {"groups": groups, "expected_revision": configuration["revision"]}
    saved = client.put("/api/v1/permission-management", json=write)
    assert saved.status_code == 200
    assert saved.json()["revision"] == configuration["revision"] + 1
    assert (
        client.put(
            "/api/v1/permission-management",
            json={
                "groups": saved.json()["groups"],
                "expected_revision": saved.json()["revision"],
            },
        ).json()["revision"]
        == saved.json()["revision"]
    )
    assert client.put("/api/v1/permission-management", json=write).status_code == 409
    client.cookies.set(data["settings"].session_cookie_name, "directory-member")
    assert client.get("/api/v1/permission-management").status_code == 403
    assert client.put("/api/v1/permission-management", json=write).status_code == 403
    assert client.get("/api/v1/organization-management/people").status_code == 403
    profile = client.get("/api/v1/auth/me").json()
    assert "organization-management" not in profile["menu_permissions"]
    assert "permission-management" not in profile["menu_permissions"]
    assert client.get("/api/v1/user-settings/me/account").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_organization_department_and_ordinary_users_have_automatic_read_scopes(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    store = directory_store(data["settings"].database_url)
    service = DirectoryService(store)
    with store.connection() as db:
        owner_row = db.execute("SELECT * FROM app_user WHERE role='owner'").fetchone()
        member = db.execute("SELECT * FROM app_user WHERE email='member@example.com'").fetchone()
    assert owner_row is not None and member is not None
    owner = store.principal(owner_row["id"], owner_row["email"], "owner")
    policy = client.get("/api/v1/permission-management").json()
    policy["groups"]["user"] += ["organization-management", "budgets", "finops-requests"]
    assert (
        client.put(
            "/api/v1/permission-management",
            json={
                "groups": policy["groups"],
                "expected_revision": policy["revision"],
            },
        ).status_code
        == 200
    )
    client.cookies.set(data["settings"].session_cookie_name, "directory-member")
    assert client.get("/api/v1/organization-management/people").json()["total"] == 1
    service.set_menu_administrators(
        owner,
        "organization",
        data["organization"]["id"],
        AdministratorsWrite(
            account_ids=[member["id"]], expected_revision=data["organization"]["revision"]
        ),
    )
    assert client.get("/api/v1/organization-management/people").json()["total"] == 2
    assert client.get("/api/v1/enterprise/entities").json()["users"]
    organization = next(
        row for row in service.organizations(owner) if row["id"] == data["organization"]["id"]
    )
    service.set_menu_administrators(
        owner,
        "organization",
        organization["id"],
        AdministratorsWrite(account_ids=[], expected_revision=organization["revision"]),
    )
    department = next(
        row for row in service.units(owner, organization["id"]) if row["id"] == data["first"]["id"]
    )
    service.set_administrators(
        owner,
        department["id"],
        AdministratorsWrite(
            account_ids=[],
            expected_revision=department["revision"],
        ),
    )
    people = client.get("/api/v1/organization-management/people").json()
    assert people["total"] == 1 and people["items"][0]["app_user_id"] == str(member["id"])
    scope = store.principal(member["id"], member["email"], "member").data_scope
    assert scope is not None and scope.department_ids == ()
    assert scope.user_ids == (member["email"],)
    assert (
        client.patch(
            f"/api/v1/organization-management/people/{data['outsider']['id']}",
            json={"display_name": "Forged", "expected_revision": 1},
        ).status_code
        == 404
    )
    assert client.get("/api/v1/permission-management").status_code == 403


def test_team_member_scope_never_expands_to_parent_department_or_other_team(
    directory_http: tuple[TestClient, dict[str, Any]],
) -> None:
    client, data = directory_http
    store = directory_store(data["settings"].database_url)
    service = DirectoryService(store)
    with store.connection() as connection:
        owner_row = connection.execute("SELECT * FROM app_user WHERE role='owner'").fetchone()
        member = connection.execute(
            "SELECT * FROM app_user WHERE email='member@example.com'"
        ).fetchone()
    assert owner_row is not None and member is not None
    owner = store.principal(owner_row["id"], owner_row["email"], "owner")
    department = service.units(owner, data["organization"]["id"])[0]
    # Remove department-wide appointment; retain only an exact team appointment.
    service.set_administrators(
        owner,
        data["first"]["id"],
        AdministratorsWrite(
            account_ids=[],
            expected_revision=next(
                row["revision"]
                for row in service.units(owner, data["organization"]["id"])
                if row["id"] == data["first"]["id"]
            ),
        ),
    )
    team = service.create_unit(
        owner,
        data["organization"]["id"],
        UnitWrite(
            kind="team",
            name="Team",
            code="team",
            parent_unit_id=data["first"]["id"],
        ),
    )
    other = service.create_unit(
        owner,
        data["organization"]["id"],
        UnitWrite(
            kind="team",
            name="Other",
            code="other",
            parent_unit_id=data["first"]["id"],
        ),
    )
    person = service.people(owner).items[0]
    person = next(
        row for row in service.people(owner).items if row["app_user_id"] == str(member["id"])
    )
    service.set_teams(
        owner,
        UUID(person["id"]),
        TeamsWrite(
            team_ids=[team["id"]],
            expected_revision=person["revision"],
        ),
    )
    service.set_menu_administrators(
        owner,
        "team",
        team["id"],
        AdministratorsWrite(
            account_ids=[member["id"]],
            expected_revision=team["revision"],
        ),
    )
    policy = client.get("/api/v1/permission-management").json()
    policy["groups"]["team_admin"].append("budgets")
    assert client.put("/api/v1/permission-management", json={
        "groups": policy["groups"], "expected_revision": policy["revision"],
    }).status_code == 200
    period = datetime.now(UTC).strftime("%Y-%m")
    for kind, scope_id, amount in (
        ("organization", data["organization"]["id"], 100_000),
        ("department", data["first"]["id"], 10_000),
        ("department", data["second"]["id"], 90_000),
        ("user", member["email"], 1_000),
    ):
        assert client.put(
            f"/api/v1/budgets/{kind}/{scope_id}", params={"period": period},
            json={"token_limit": amount},
        ).status_code == 200
    now = datetime.now(UTC)
    repository = app.dependency_overrides[get_repository]()
    for user in (member["email"], "outsider@example.com"):
        record = _usage_record(str(uuid4()), "Runtime", 10).model_copy(
            update={
                "ts": now,
                "request_id": user,
                "organization_id": data["organization"]["id"],
                "department_id": data["first"]["id"],
                "user_id": user,
                "user": user,
            }
        )
        repository.write_token_usage(record)
    client.cookies.set(data["settings"].session_cookie_name, "directory-member")
    people = client.get(
        "/api/v1/organization-management/people", params={"department_id": data["first"]["id"]}
    ).json()
    assert people["total"] == 1
    assert people["items"][0]["governance_user_id"] == member["email"]
    assert (
        client.get(
            "/api/v1/organization-management/people", params={"team_id": other["id"]}
        ).json()["total"]
        == 0
    )
    assert (
        client.get(f"/api/v1/organization-management/people/{data['outsider']['id']}").status_code
        == 404
    )
    assert service.audit(store.principal(member["id"], member["email"], "member")) == []
    window = {
        "from": (now - timedelta(hours=1)).isoformat(),
        "to": (now + timedelta(hours=1)).isoformat(),
        "directory_status": "active",
    }
    response = client.get("/api/v1/observability/requests", params=window)
    assert response.status_code == 200
    assert all(row["user_id"] == member["email"] for row in response.json()["items"])
    budget = client.get("/api/v1/budgets", params={"period": period, "include_users": True})
    assert budget.status_code == 200
    assert {row["scope_id"] for row in budget.json()["items"] if row["scope_type"] == "user"} == {
        member["email"]
    }
    parent = next(row for row in budget.json()["items"] if row["scope_id"] == data["first"]["id"])
    assert parent["token_limit"] == 1_000
    people_budget = client.get("/api/v1/budgets/users", params={
        "period": period, "department_id": data["first"]["id"],
    })
    assert people_budget.status_code == 200 and people_budget.json()["total"] == 1
    assert people_budget.json()["department_token_limit"] == 1_000
    assert (
        client.put(
            "/api/v1/budgets/department/" + department["id"],
            params={"period": "2026-10"},
            json={"token_limit": 1000},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("invalid", ["settings", "permission-management", "unknown"])
def test_policy_rejects_unknown_and_owner_delegation(invalid: str) -> None:
    groups: dict[MenuPermissionGroup, list[str]] = {
        role: ["user-settings", invalid]
        for role in (
            "organization_admin",
            "department_admin",
            "team_admin",
            "user",
        )
    }
    with pytest.raises(ValueError):
        PermissionPolicyWrite(expected_revision=1, groups=groups)
    assert "permission-management" in ALL_MENUS
