from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar, cast
from uuid import UUID, uuid4

from psycopg import errors, sql
from psycopg.types.json import Jsonb

from ..domain.directory import (
    AccountLinkWrite,
    AdministratorsWrite,
    DirectoryError,
    DirectoryPage,
    DirectoryPrincipal,
    DirectoryWrite,
    ObservationResolveWrite,
    OrganizationWrite,
    PersonCreate,
    PersonStatusWrite,
    PersonWrite,
    TeamsWrite,
    TransferCancelWrite,
    TransferWrite,
    UnitWrite,
)
from ..persistence.directory_store import DirectoryConnection, DirectoryStore

EntityKind = Literal["organization", "unit", "person"]
_TABLES: dict[EntityKind, str] = {
    "organization": "directory_organization",
    "unit": "directory_unit",
    "person": "directory_person",
}


_JSONValue = TypeVar("_JSONValue")


def json_value(value: _JSONValue) -> _JSONValue:
    return cast(_JSONValue, json.loads(json.dumps(value, default=str)))


class DirectoryService:
    def __init__(self, store: DirectoryStore) -> None:
        self.store = store

    def capabilities(self, principal: DirectoryPrincipal) -> dict[str, Any]:
        state = self.store.require_ready()
        return {
            "can_read": principal.owner or bool(principal.department_ids),
            "can_manage_organizations": principal.owner,
            "can_manage_connections": principal.owner,
            "capabilities": (
                ["directory.read", "directory.edit_people", "directory.edit_teams"]
                if principal.owner
                else list(principal.capabilities)
            ),
            "department_ids": list(principal.department_ids or ()),
            "permission_revision": state["permission_revision"],
            "directory_version": state["active_version"],
            "protocol_version": state["protocol_version"],
            "gateway_projected_version": state["projected_version"],
            "gateway_projection_state": (
                "confirmed" if state["projected_version"] >= state["active_version"] else "pending"
            ),
            "can_schedule_transfers": (
                principal.owner and state["person_admission_mode"] != "unverified"
            ),
        }

    @staticmethod
    def _entity(
        connection: DirectoryConnection,
        kind: EntityKind,
        entity_id: str | UUID,
    ) -> dict[str, Any]:
        row = connection.execute(
            sql.SQL("SELECT * FROM {} WHERE id = %s FOR UPDATE").format(
                sql.Identifier(_TABLES[kind])
            ),
            (entity_id,),
        ).fetchone()
        if row is None:
            raise DirectoryError("scope_not_found", "Directory scope not found", 404)
        return dict(row)

    @staticmethod
    def _revision(row: dict[str, Any], write: DirectoryWrite) -> None:
        if write.expected_revision is None or row["revision"] != write.expected_revision:
            raise DirectoryError(
                "revision_conflict", "Directory data changed; reload before saving"
            )

    @staticmethod
    def _active(row: dict[str, Any]) -> None:
        if row["status"] != "active":
            raise DirectoryError("scope_inactive", "Directory scope is not active")

    @staticmethod
    def _department_id(row: dict[str, Any]) -> str:
        return str(row["id"] if row["kind"] == "department" else row["parent_unit_id"])

    def _person(
        self,
        connection: DirectoryConnection,
        person_id: UUID,
    ) -> dict[str, Any]:
        self._entity(connection, "person", person_id)
        row = next(
            (row for row in self.store.people_rows(connection, person_id=person_id)),
            None,
        )
        if row is None:
            raise DirectoryError("person_not_found", "Person not found", 404)
        row.pop("_total_count", None)
        return row

    @staticmethod
    def _audit(
        connection: DirectoryConnection,
        principal: DirectoryPrincipal,
        kind: str,
        entity_id: str,
        action: str,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        organization_id: str | None = None,
        department_id: str | None = None,
        reason: str = "",
        source: str = "manual",
        job_id: UUID | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO directory_change_audit(
                 entity_type, entity_id, organization_id, department_id, action,
                 actor_account_id, actor_label, before_value, after_value, reason,source,job_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                kind,
                entity_id,
                organization_id,
                department_id,
                action,
                principal.account_id,
                principal.email,
                Jsonb(json_value(before)) if before is not None else None,
                Jsonb(json_value(after)) if after is not None else None,
                reason,
                source,
                job_id,
            ),
        )

    def _mutate(
        self,
        principal: DirectoryPrincipal,
        operation: str,
        value: dict[str, Any],
        callback: Callable[[DirectoryConnection], dict[str, Any]],
        key: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_ready()
        if key is not None and not 1 <= len(key) <= 128:
            raise DirectoryError("invalid_idempotency_key", "Invalid idempotency key", 422)
        digest = hashlib.sha256(json.dumps(json_value(value), sort_keys=True).encode()).hexdigest()
        try:
            with self.store.connection() as connection, connection.transaction():
                state = connection.execute(
                    "SELECT * FROM directory_control_state FOR UPDATE"
                ).fetchone()
                if state is None or state["source"] != "database":
                    raise DirectoryError(
                        "directory_not_ready", "Directory authority is not ready", 503
                    )
                if (
                    principal.account_id
                    and not principal.owner
                    and principal.permission_revision != state["permission_revision"]
                ):
                    raise DirectoryError("permission_changed", "Directory permissions changed", 403)
                if key:
                    existing = connection.execute(
                        """SELECT request_digest, result FROM directory_idempotency
                           WHERE actor_id = %s AND operation = %s AND key = %s""",
                        (str(principal.account_id or principal.email), operation, key),
                    ).fetchone()
                    if existing:
                        if existing["request_digest"] != digest:
                            raise DirectoryError(
                                "idempotency_conflict",
                                "Idempotency key belongs to another request",
                            )
                        return dict(existing["result"])
                result = callback(connection)
                navigation_only = result.pop("_navigation_only", False)
                updated_state = connection.execute(
                    """UPDATE directory_control_state SET active_version = active_version + %s,
                       permission_revision = permission_revision + 1, updated_at = now()
                       RETURNING active_version""",
                    (0 if navigation_only else 1,),
                ).fetchone()
                assert updated_state is not None
                version = updated_state["active_version"]
                result = {
                    **json_value(result),
                    "directory_state": "saved",
                    "directory_version": version,
                    "gateway_projection_state": (
                        "confirmed"
                        if navigation_only and state["projected_version"] >= version
                        else "pending"
                    ),
                }
                if not navigation_only:
                    connection.execute(
                        """INSERT INTO directory_outbox(entity_id, directory_version)
                           VALUES (%s, %s)""",
                        (str(result.get("id", operation)), version),
                    )
                if key:
                    connection.execute(
                        """INSERT INTO directory_idempotency(
                             actor_id, operation, key, request_digest, result)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (
                            str(principal.account_id or principal.email),
                            operation,
                            key,
                            digest,
                            Jsonb(result),
                        ),
                    )
                return result
        except (errors.UniqueViolation, errors.CheckViolation, errors.ForeignKeyViolation) as error:
            raise DirectoryError(
                "directory_constraint",
                "Directory identity, membership or reference conflicts",
            ) from error

    @staticmethod
    def _insert(
        connection: DirectoryConnection,
        table: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        columns = list(values)
        row = connection.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns),
            ),
            list(values.values()),
        ).fetchone()
        assert row is not None
        return dict(row)

    @staticmethod
    def _update(
        connection: DirectoryConnection,
        table: str,
        entity_id: str | UUID,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        row = connection.execute(
            sql.SQL(
                "UPDATE {} SET {}, revision=revision+1, updated_at=now() WHERE id=%s RETURNING *"
            ).format(
                sql.Identifier(table),
                sql.SQL(",").join(
                    sql.SQL("{}=%s").format(sql.Identifier(column)) for column in values
                ),
            ),
            [*values.values(), entity_id],
        ).fetchone()
        assert row is not None
        return dict(row)

    def organizations(self, principal: DirectoryPrincipal) -> list[dict[str, Any]]:
        self.store.require_ready()
        if not principal.owner and not principal.department_ids:
            raise DirectoryError("directory_forbidden", "Directory access is not granted", 403)
        with self.store.connection() as connection:
            if principal.owner:
                return [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM directory_organization ORDER BY name, id"
                    ).fetchall()
                ]
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT DISTINCT o.* FROM directory_organization o
                   JOIN directory_unit u ON u.organization_id=o.id
                   WHERE u.id=ANY(%s) ORDER BY o.name, o.id""",
                    (list(principal.department_ids or ()),),
                ).fetchall()
            ]

    def units(
        self,
        principal: DirectoryPrincipal,
        organization_id: str,
    ) -> list[dict[str, Any]]:
        allowed = {row["id"] for row in self.organizations(principal)}
        if organization_id not in allowed:
            raise DirectoryError("scope_not_found", "Directory scope not found", 404)
        with self.store.connection() as connection:
            clause = "" if principal.owner else " AND (id=ANY(%s) OR parent_unit_id=ANY(%s))"
            parameters: list[Any] = [organization_id]
            if not principal.owner:
                scope = list(principal.department_ids or ())
                parameters.extend([scope, scope])
            rows = connection.execute(
                f"""SELECT * FROM directory_unit WHERE organization_id=%s{clause}
                    ORDER BY kind, name, id""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def people(
        self,
        principal: DirectoryPrincipal,
        *,
        department_id: str | None = None,
        team_id: str | None = None,
        organization_id: str | None = None,
        query: str = "",
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> DirectoryPage:
        self.store.require_ready()
        if not principal.owner and not principal.department_ids:
            raise DirectoryError("directory_forbidden", "Directory access is not granted", 403)
        if department_id:
            principal.require("directory.read", department_id)
        with self.store.connection() as connection:
            rows = self.store.people_rows(
                connection,
                department_ids=None if principal.owner else principal.department_ids,
                department_id=department_id,
                team_id=team_id,
                organization_id=organization_id,
                query=query,
                status=status,
                offset=offset,
                limit=limit,
            )
            total = int(rows[0]["_total_count"]) if rows else 0
            if not rows and offset:
                first = self.store.people_rows(
                    connection,
                    department_ids=None if principal.owner else principal.department_ids,
                    department_id=department_id,
                    team_id=team_id,
                    organization_id=organization_id,
                    query=query,
                    status=status,
                    limit=1,
                )
                total = int(first[0]["_total_count"]) if first else 0
        for row in rows:
            row.pop("_total_count", None)
        return DirectoryPage(
            items=json_value(rows),
            total=total,
            next_cursor=str(offset + limit) if offset + limit < total else None,
            generated_at=datetime.now(UTC),
        )

    def create_organization(
        self,
        principal: DirectoryPrincipal,
        write: OrganizationWrite,
        key: str | None = None,
    ) -> dict[str, Any]:
        principal.require_owner()

        def create(connection: DirectoryConnection) -> dict[str, Any]:
            row = self._insert(
                connection,
                "directory_organization",
                {
                    "id": f"org-{uuid4().hex}",
                    **write.model_dump(
                        exclude={
                            "expected_revision",
                            "reason",
                        }
                    ),
                    "updated_by": principal.email,
                },
            )
            self._audit(
                connection,
                principal,
                "organization",
                row["id"],
                "created",
                after=row,
                organization_id=row["id"],
                reason=write.reason,
            )
            return row

        return self._mutate(principal, "create-organization", write.model_dump(), create, key)

    def update_organization(
        self,
        principal: DirectoryPrincipal,
        entity_id: str,
        write: OrganizationWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._entity(connection, "organization", entity_id)
            self._revision(before, write)
            if write.status != "active":
                active = connection.execute(
                    """SELECT 1 FROM directory_unit WHERE organization_id=%s
                       AND status='active' LIMIT 1""",
                    (entity_id,),
                ).fetchone()
                if active:
                    raise DirectoryError("active_children", "Deactivate dependent units first")
                jobs = connection.execute(
                    """SELECT 1 FROM directory_connection c JOIN directory_sync_job j
                       ON j.connection_id=c.id WHERE c.organization_id=%s
                       AND j.status IN ('queued','running','awaiting_review','applying') LIMIT 1""",
                    (entity_id,),
                ).fetchone()
                if jobs:
                    raise DirectoryError(
                        "organization_sync_pending", "Finish or cancel pending directory jobs first"
                    )
            row = self._update(
                connection,
                "directory_organization",
                entity_id,
                {
                    **write.model_dump(exclude={"expected_revision", "reason"}),
                    "updated_by": principal.email,
                },
            )
            self._audit(
                connection,
                principal,
                "organization",
                entity_id,
                "updated",
                before=before,
                after=row,
                organization_id=entity_id,
                reason=write.reason,
            )
            return row

        return self._mutate(
            principal, f"update-organization:{entity_id}", write.model_dump(), update
        )

    def create_unit(
        self,
        principal: DirectoryPrincipal,
        organization_id: str,
        write: UnitWrite,
        key: str | None = None,
    ) -> dict[str, Any]:
        if write.kind == "department":
            principal.require_owner()
        else:
            principal.require("directory.edit_teams", write.parent_unit_id)

        def create(connection: DirectoryConnection) -> dict[str, Any]:
            self._active(self._entity(connection, "organization", organization_id))
            if write.parent_unit_id:
                parent = self._entity(connection, "unit", write.parent_unit_id)
                self._active(parent)
                if parent["organization_id"] != organization_id or parent["kind"] != "department":
                    raise DirectoryError(
                        "invalid_parent", "Team parent is not in this organization"
                    )
            row = self._insert(
                connection,
                "directory_unit",
                {
                    "id": f"{write.kind}-{uuid4().hex}",
                    "organization_id": organization_id,
                    **write.model_dump(exclude={"expected_revision", "reason", "contact_email"}),
                    "updated_by": principal.email,
                },
            )
            self._audit(
                connection,
                principal,
                "unit",
                row["id"],
                "created",
                after=row,
                organization_id=organization_id,
                department_id=self._department_id(row),
                reason=write.reason,
            )
            return row

        return self._mutate(
            principal,
            f"create-unit:{organization_id}",
            write.model_dump(),
            create,
            key,
        )

    def update_unit(
        self,
        principal: DirectoryPrincipal,
        entity_id: str,
        write: UnitWrite,
    ) -> dict[str, Any]:
        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._entity(connection, "unit", entity_id)
            department_id = self._department_id(before)
            if before["kind"] == "department":
                principal.require_owner()
            else:
                principal.require("directory.edit_teams", department_id)
            self._revision(before, write)
            if write.kind != before["kind"] or write.parent_unit_id != before["parent_unit_id"]:
                raise DirectoryError("immutable_hierarchy", "Unit hierarchy cannot be edited")
            if write.status != "active":
                dependencies = connection.execute(
                    """SELECT EXISTS(SELECT 1 FROM directory_membership m
                         JOIN directory_person p ON p.id=m.person_id AND p.status='active'
                         WHERE m.unit_id=%s AND (m.valid_to IS NULL OR m.valid_to>now()))
                       OR EXISTS(SELECT 1 FROM directory_unit
                         WHERE parent_unit_id=%s AND status='active')
                       OR EXISTS(SELECT 1 FROM directory_project_reference WHERE department_id=%s)
                       OR EXISTS(SELECT 1 FROM gateway_application
                         WHERE department_id=%s AND status <> 'retired')
                       OR EXISTS(SELECT 1 FROM directory_transfer
                         WHERE status='scheduled'
                           AND (source_department_id=%s OR target_department_id=%s))
                       OR EXISTS(SELECT 1 FROM directory_group_mapping g
                         JOIN directory_sync_job j ON j.connection_id=g.connection_id
                         WHERE g.unit_id=%s AND g.enabled
                           AND j.status IN ('queued','running','awaiting_review','applying'))
                         AS occupied""",
                    (entity_id, entity_id, entity_id, entity_id, entity_id, entity_id, entity_id),
                ).fetchone()
                if dependencies is not None and dependencies["occupied"]:
                    raise DirectoryError(
                        "unit_in_use", "Unit has active members or referenced resources"
                    )
            row = self._update(
                connection,
                "directory_unit",
                entity_id,
                {
                    **write.model_dump(exclude={"expected_revision", "reason", "contact_email"}),
                    "updated_by": principal.email,
                },
            )
            self._audit(
                connection,
                principal,
                "unit",
                entity_id,
                "updated",
                before=before,
                after=row,
                organization_id=row["organization_id"],
                department_id=department_id,
                reason=write.reason,
            )
            return row

        return self._mutate(principal, f"update-unit:{entity_id}", write.model_dump(), update)

    def _teams(
        self,
        connection: DirectoryConnection,
        person_id: UUID,
        department: dict[str, Any],
        team_ids: list[str],
    ) -> None:
        for team_id in set(team_ids):
            team = self._entity(connection, "unit", team_id)
            self._active(team)
            if team["kind"] != "team" or team["parent_unit_id"] != department["id"]:
                raise DirectoryError("invalid_team", "Team is outside the primary department")
        current = connection.execute(
            """SELECT unit_id FROM directory_membership WHERE person_id=%s
               AND membership_kind='team' AND source_key='manual' AND valid_to IS NULL""",
            (person_id,),
        ).fetchall()
        old = {str(row["unit_id"]) for row in current}
        wanted = set(team_ids)
        external = {
            str(row["unit_id"])
            for row in connection.execute(
                """SELECT unit_id FROM directory_membership WHERE person_id=%s
               AND membership_kind='team' AND source_key<>'manual' AND valid_to IS NULL""",
                (person_id,),
            ).fetchall()
        }
        for team_id in old - wanted:
            connection.execute(
                """UPDATE directory_membership SET valid_to=clock_timestamp(), revision=revision+1
                   WHERE person_id=%s AND unit_id=%s AND membership_kind='team'
                     AND source_key='manual' AND valid_to IS NULL""",
                (person_id, team_id),
            )
        for team_id in wanted - old - external:
            connection.execute(
                """INSERT INTO directory_membership(
                     person_id, unit_id, organization_id, membership_kind)
                   VALUES (%s,%s,%s,'team')""",
                (person_id, team_id, department["organization_id"]),
            )

    @staticmethod
    def _employee_number(
        connection: DirectoryConnection,
        organization_id: str,
        employee_number: str | None,
        person_id: UUID | None = None,
    ) -> None:
        if not employee_number:
            return
        duplicate = connection.execute(
            """SELECT 1 FROM directory_person p JOIN directory_membership m ON m.person_id=p.id
               WHERE m.organization_id=%s AND m.membership_kind='primary_department'
                 AND m.valid_from<=now() AND (m.valid_to IS NULL OR m.valid_to>now())
                 AND lower(p.employee_number)=lower(%s) AND (%s::uuid IS NULL OR p.id<>%s)
               LIMIT 1""",
            (organization_id, employee_number, person_id, person_id),
        ).fetchone()
        if duplicate:
            raise DirectoryError(
                "employee_number_conflict", "Employee number already exists in this organization"
            )

    def create_person(
        self,
        principal: DirectoryPrincipal,
        write: PersonCreate,
        key: str | None = None,
    ) -> dict[str, Any]:
        principal.require("directory.edit_people", write.department_id)
        if write.requested_menu_groups not in (None, ["user"]):
            principal.require_owner()

        def create(connection: DirectoryConnection) -> dict[str, Any]:
            department = self._entity(connection, "unit", write.department_id)
            self._active(department)
            self._active(self._entity(connection, "organization", department["organization_id"]))
            if department["kind"] != "department":
                raise DirectoryError("department_required", "A primary department is required")
            self._employee_number(connection, department["organization_id"], write.employee_number)
            row = self._insert(
                connection,
                "directory_person",
                {
                    **write.model_dump(
                        exclude={
                            "expected_revision",
                            "reason",
                            "department_id",
                            "team_ids",
                            "menu_permission_group",
                            "menu_permission_groups",
                        }
                    ),
                    "menu_permission_group": (write.requested_menu_groups or ["user"])[0],
                    "menu_permission_groups": write.requested_menu_groups or ["user"],
                    "updated_by": principal.email,
                },
            )
            connection.execute(
                """INSERT INTO directory_membership(
                     person_id, unit_id, organization_id, membership_kind)
                   VALUES (%s,%s,%s,'primary_department')""",
                (row["id"], department["id"], department["organization_id"]),
            )
            self._teams(connection, row["id"], department, write.team_ids)
            result = self._person(connection, row["id"])
            self._audit(
                connection,
                principal,
                "person",
                str(row["id"]),
                "created",
                after=result,
                organization_id=department["organization_id"],
                department_id=department["id"],
                reason=write.reason,
            )
            return result

        return self._mutate(principal, "create-person", write.model_dump(), create, key)

    def update_person(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
        write: PersonWrite,
    ) -> dict[str, Any]:
        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._person(connection, person_id)
            principal.require("directory.edit_people", before["department_id"])
            self._revision(before, write)
            if (
                write.requested_menu_groups is not None
                and set(write.requested_menu_groups) != set(before["menu_permission_groups"])
            ):
                principal.require_owner()
            if before["externally_managed"] and any(
                getattr(write, field) != before[field]
                for field in ("display_name", "contact_email", "job_title")
                if field in write.model_fields_set
            ):
                raise DirectoryError(
                    "external_field_readonly",
                    "External profile fields must be changed at the source",
                )
            self._employee_number(
                connection,
                before["organization_id"],
                write.employee_number,
                person_id,
            )
            self._update(
                connection,
                "directory_person",
                person_id,
                {
                    **write.model_dump(
                        exclude={
                            "expected_revision", "reason",
                            "menu_permission_group", "menu_permission_groups",
                        },
                        exclude_unset=True,
                    ),
                    **(
                        {
                            "menu_permission_group": write.requested_menu_groups[0],
                            "menu_permission_groups": write.requested_menu_groups,
                        }
                        if write.requested_menu_groups is not None
                        else {}
                    ),
                    "updated_by": principal.email,
                },
            )
            result = self._person(connection, person_id)
            self._audit(
                connection,
                principal,
                "person",
                str(person_id),
                "updated",
                before=before,
                after=result,
                organization_id=before["organization_id"],
                department_id=before["department_id"],
                reason=write.reason,
            )
            # A menu-only save invalidates session navigation without publishing APIM identity.
            result["_navigation_only"] = all(
                before[field] == result[field]
                for field in ("display_name", "contact_email", "employee_number", "job_title")
            )
            return result

        return self._mutate(principal, f"update-person:{person_id}", write.model_dump(), update)

    def set_teams(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
        write: TeamsWrite,
    ) -> dict[str, Any]:
        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._person(connection, person_id)
            principal.require("directory.edit_people", before["department_id"])
            self._revision(before, write)
            department = self._entity(connection, "unit", before["department_id"])
            self._teams(connection, person_id, department, write.team_ids)
            self._update(connection, "directory_person", person_id, {"updated_by": principal.email})
            result = self._person(connection, person_id)
            self._audit(
                connection,
                principal,
                "person",
                str(person_id),
                "teams_updated",
                before=before,
                after=result,
                organization_id=before["organization_id"],
                department_id=before["department_id"],
                reason=write.reason,
            )
            return result

        return self._mutate(principal, f"teams:{person_id}", write.model_dump(), update)

    def link_account(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
        write: AccountLinkWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def link(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._person(connection, person_id)
            self._revision(before, write)
            account = connection.execute(
                "SELECT id, enabled FROM app_user WHERE id=%s FOR UPDATE",
                (write.app_user_id,),
            ).fetchone()
            if not account or not account["enabled"]:
                raise DirectoryError("account_not_found", "Enabled account not found", 404)
            existing = connection.execute(
                "SELECT app_user_id FROM directory_account_link WHERE person_id=%s",
                (person_id,),
            ).fetchone()
            if existing and existing["app_user_id"] != write.app_user_id:
                raise DirectoryError(
                    "account_already_linked", "Person is already linked to an account"
                )
            connection.execute(
                """INSERT INTO directory_account_link(person_id,app_user_id,verified_by)
                   VALUES (%s,%s,%s) ON CONFLICT(person_id) DO NOTHING""",
                (person_id, write.app_user_id, principal.email),
            )
            self._update(connection, "directory_person", person_id, {"updated_by": principal.email})
            result = self._person(connection, person_id)
            self._audit(
                connection,
                principal,
                "person",
                str(person_id),
                "account_linked",
                after={"app_user_id": str(write.app_user_id)},
                reason=write.reason,
                organization_id=before["organization_id"],
                department_id=before["department_id"],
            )
            return result

        return self._mutate(principal, f"link:{person_id}", write.model_dump(), link)

    def administrators(
        self,
        principal: DirectoryPrincipal,
        department_id: str,
    ) -> list[dict[str, Any]]:
        principal.require("directory.read", department_id)
        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT g.id,g.app_user_id,g.department_id,g.capabilities,g.valid_from,
                          g.valid_to,a.email,a.display_name
                   FROM directory_department_grant g JOIN app_user a ON a.id=g.app_user_id
                   WHERE g.department_id=%s AND g.valid_to IS NULL ORDER BY a.email""",
                (department_id,),
            ).fetchall()
        return json_value(rows)

    def set_administrators(
        self,
        principal: DirectoryPrincipal,
        department_id: str,
        write: AdministratorsWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def update(connection: DirectoryConnection) -> dict[str, Any]:
            department = self._entity(connection, "unit", department_id)
            self._revision(department, write)
            self._active(department)
            if department["kind"] != "department":
                raise DirectoryError(
                    "department_required", "Administrator scope must be a department"
                )
            valid = {
                row["app_user_id"]
                for row in self.store.people_rows(connection, department_id=department_id)
                if row["department_id"] == department_id
                and row["status"] == "active"
                and row["account_enabled"]
                and not row["manual_disabled"]
                and not row["source_disabled"]
            }
            if not set(write.account_ids).issubset(valid):
                raise DirectoryError(
                    "invalid_administrator",
                    "Administrators must be active linked department members",
                )
            connection.execute(
                """UPDATE directory_department_grant SET valid_to=now()
                   WHERE department_id=%s AND valid_to IS NULL""",
                (department_id,),
            )
            for account_id in set(write.account_ids):
                connection.execute(
                    """INSERT INTO directory_department_grant(app_user_id,department_id,granted_by)
                       VALUES (%s,%s,%s)""",
                    (account_id, department_id, principal.email),
                )
            result = self._update(
                connection,
                "directory_unit",
                department_id,
                {"updated_by": principal.email},
            )
            self._audit(
                connection,
                principal,
                "unit",
                department_id,
                "administrators_updated",
                after={"account_ids": [str(value) for value in write.account_ids]},
                organization_id=department["organization_id"],
                department_id=department_id,
                reason=write.reason,
            )
            return result

        return self._mutate(
            principal,
            f"administrators:{department_id}",
            write.model_dump(),
            update,
        )

    def status_preview(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
    ) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection:
            person = self._person(connection, person_id)
            budgets = connection.execute(
                """SELECT period_start,parent_scope_id,token_limit FROM token_budget
                   WHERE scope_type='user' AND scope_id=%s ORDER BY period_start""",
                (person["governance_user_id"],),
            ).fetchall()
        return {
            "person": json_value(person),
            "budgets": json_value(budgets),
            "governance_identity_preserved": True,
            "gateway_projection_state": "pending",
        }

    def set_status(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
        write: PersonStatusWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def update(connection: DirectoryConnection) -> dict[str, Any]:
            before = self._person(connection, person_id)
            self._revision(before, write)
            if write.disable_account and before["app_user_id"]:
                account = connection.execute(
                    "SELECT role FROM app_user WHERE id=%s FOR UPDATE",
                    (before["app_user_id"],),
                ).fetchone()
                if account and account["role"] == "owner":
                    owners = connection.execute(
                        "SELECT count(*) AS total FROM app_user WHERE role='owner' AND enabled"
                    ).fetchone()
                    if owners is None or owners["total"] <= 1:
                        raise DirectoryError("last_owner", "Cannot disable the last enabled Owner")
                connection.execute(
                    "UPDATE app_user SET enabled=FALSE WHERE id=%s",
                    (before["app_user_id"],),
                )
                connection.execute(
                    "DELETE FROM user_session WHERE user_id=%s",
                    (before["app_user_id"],),
                )
            self._update(
                connection,
                "directory_person",
                person_id,
                {
                    "status": write.status,
                    "manual_disabled": write.status != "active",
                    "updated_by": principal.email,
                },
            )
            result = self._person(connection, person_id)
            self._audit(
                connection,
                principal,
                "person",
                str(person_id),
                "status_changed",
                before=before,
                after=result,
                organization_id=before["organization_id"],
                department_id=before["department_id"],
                reason=write.reason,
            )
            return result

        return self._mutate(principal, f"status:{person_id}", write.model_dump(), update)

    def transfer_preview(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
        write: TransferWrite,
    ) -> dict[str, Any]:
        principal.require_owner()
        state = self.store.require_ready()
        if state["person_admission_mode"] == "unverified":
            raise DirectoryError(
                "transfer_admission_unverified",
                "Verify every employee admission path before scheduling transfers",
                503,
            )
        with self.store.connection() as connection:
            person = self._person(connection, person_id)
            self._revision(person, write)
            target = self._entity(connection, "unit", write.target_department_id)
            self._active(target)
            now = datetime.now(UTC)
            current_month = now.date().replace(day=1)
            if write.effective_month <= current_month:
                raise DirectoryError(
                    "same_period_transfer_not_supported",
                    "Transfer must take effect in a future month",
                )
            if target["kind"] != "department" or target["id"] == person["department_id"]:
                raise DirectoryError("invalid_transfer_target", "Choose another active department")
            budgets = self._future_budgets(connection, person["governance_user_id"], write)
            digest = self._transfer_digest(person, target, write, budgets)
        return {
            "person": json_value(person),
            "target_department": json_value(target),
            "effective_month": str(write.effective_month),
            "future_budgets": json_value(budgets),
            "revalidate_at_activation": True,
            "historical_usage_preserved": True,
            "preview_digest": digest,
        }

    @staticmethod
    def _future_budgets(
        connection: DirectoryConnection,
        governance_id: str,
        write: TransferWrite,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT period_start,parent_scope_id,token_limit FROM token_budget
               WHERE scope_type='user' AND scope_id=%s AND period_start>=%s
               ORDER BY period_start""",
                (governance_id, write.effective_month),
            ).fetchall()
        ]

    @staticmethod
    def _transfer_digest(
        person: dict[str, Any],
        target: dict[str, Any],
        write: TransferWrite,
        budgets: list[dict[str, Any]],
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                json_value(
                    {
                        "person_id": person["id"],
                        "person_revision": person["revision"],
                        "source_department_id": person["department_id"],
                        "target_department_id": target["id"],
                        "target_revision": target["revision"],
                        "effective_month": write.effective_month,
                        "future_budgets": budgets,
                    }
                ),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def schedule_transfer(
        self,
        principal: DirectoryPrincipal,
        person_id: UUID,
        write: TransferWrite,
        key: str,
    ) -> dict[str, Any]:
        principal.require_owner()
        self.transfer_preview(principal, person_id, write)

        def schedule(connection: DirectoryConnection) -> dict[str, Any]:
            person = self._person(connection, person_id)
            self._revision(person, write)
            target = self._entity(connection, "unit", write.target_department_id)
            self._active(target)
            self._active(self._entity(connection, "organization", target["organization_id"]))
            budgets = self._future_budgets(connection, person["governance_user_id"], write)
            if write.preview_digest is not None and write.preview_digest != self._transfer_digest(
                person,
                target,
                write,
                budgets,
            ):
                raise DirectoryError(
                    "transfer_preview_stale", "Transfer impact changed; generate a new preview"
                )
            row = self._insert(
                connection,
                "directory_transfer",
                {
                    "person_id": person_id,
                    "source_department_id": person["department_id"],
                    "target_department_id": write.target_department_id,
                    "effective_month": write.effective_month,
                    "expected_person_revision": person["revision"],
                    "approved_future_budgets": Jsonb(json_value(budgets)),
                    "reason": write.reason,
                    "requested_by": principal.email,
                    "idempotency_key": key,
                },
            )
            self._audit(
                connection,
                principal,
                "transfer",
                str(row["id"]),
                "scheduled",
                after=row,
                organization_id=person["organization_id"],
                department_id=person["department_id"],
                reason=write.reason,
            )
            return row

        return self._mutate(
            principal,
            f"transfer:{person_id}",
            write.model_dump(),
            schedule,
            key,
        )

    def transfers(
        self,
        principal: DirectoryPrincipal,
        *,
        person_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        principal.require_owner()
        self.store.require_ready()
        with self.store.connection() as connection:
            return json_value(
                [
                    dict(row)
                    for row in connection.execute(
                        """SELECT t.*,p.display_name,p.governance_user_id,
                     source.name AS source_department_name,target.name AS target_department_name,
                     source.organization_id AS source_organization_id,
                     target.organization_id AS target_organization_id
                   FROM directory_transfer t JOIN directory_person p ON p.id=t.person_id
                   JOIN directory_unit source ON source.id=t.source_department_id
                   JOIN directory_unit target ON target.id=t.target_department_id
                   WHERE %s::uuid IS NULL OR t.person_id=%s
                   ORDER BY t.created_at DESC,t.id LIMIT 100""",
                        (person_id, person_id),
                    ).fetchall()
                ]
            )

    def cancel_transfer(
        self,
        principal: DirectoryPrincipal,
        transfer_id: UUID,
        write: TransferCancelWrite,
    ) -> dict[str, Any]:
        principal.require_owner()

        def cancel(connection: DirectoryConnection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM directory_transfer WHERE id=%s FOR UPDATE",
                (transfer_id,),
            ).fetchone()
            if row is None or row["status"] not in {"scheduled", "conflicted"}:
                raise DirectoryError(
                    "transfer_not_cancellable",
                    "Only scheduled or conflicted transfers can be cancelled",
                )
            connection.execute(
                """UPDATE directory_transfer SET status='cancelled',completed_at=now()
                   WHERE id=%s""",
                (transfer_id,),
            )
            self._audit(
                connection,
                principal,
                "transfer",
                str(transfer_id),
                "cancelled",
                before=dict(row),
                after={"status": "cancelled"},
                reason=write.reason,
                department_id=row["source_department_id"],
            )
            return {"id": str(transfer_id), "status": "cancelled"}

        return self._mutate(
            principal,
            f"cancel-transfer:{transfer_id}",
            write.model_dump(),
            cancel,
        )

    def audit(
        self,
        principal: DirectoryPrincipal,
        *,
        department_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if department_id:
            principal.require("directory.read", department_id)
        if not principal.owner and not principal.department_ids:
            raise DirectoryError("directory_forbidden", "Directory access is not granted", 403)
        with self.store.connection() as connection:
            clauses: list[str] = []
            values: list[Any] = []
            if not principal.owner:
                clauses.append("department_id=ANY(%s)")
                values.append(list(principal.department_ids or ()))
            if department_id:
                clauses.append("department_id=%s")
                values.append(department_id)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            return json_value(
                connection.execute(
                    f"""SELECT * FROM directory_change_audit{where}
                    ORDER BY changed_at DESC,id LIMIT %s""",
                    [*values, limit],
                ).fetchall()
            )

    def identity_conflicts(
        self,
        principal: DirectoryPrincipal,
        *,
        query: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        principal.require_owner()
        self.store.require_ready()
        with self.store.connection() as connection:
            return json_value(
                [
                    dict(row)
                    for row in connection.execute(
                        """SELECT * FROM directory_observation_candidate
                   WHERE status IN ('pending','conflict') AND original_user_id ILIKE %s
                   ORDER BY last_seen_at DESC,id LIMIT %s""",
                        (f"%{query}%", limit),
                    ).fetchall()
                ]
            )

    def resolve_observation(
        self,
        principal: DirectoryPrincipal,
        candidate_id: UUID,
        write: ObservationResolveWrite,
    ) -> dict[str, Any]:
        principal.require_owner()
        if write.ignore == (write.person_id is not None):
            raise DirectoryError(
                "observation_decision_required",
                "Select a person or explicitly ignore this candidate",
                422,
            )

        def resolve(connection: DirectoryConnection) -> dict[str, Any]:
            candidate = connection.execute(
                "SELECT * FROM directory_observation_candidate WHERE id=%s FOR UPDATE",
                (candidate_id,),
            ).fetchone()
            if candidate is None or candidate["status"] not in {"pending", "conflict"}:
                raise DirectoryError(
                    "candidate_not_found", "Unresolved identity candidate not found", 404
                )
            if write.person_id is not None:
                person = self._person(connection, write.person_id)
                if person["governance_user_id"] != candidate["original_user_id"]:
                    raise DirectoryError(
                        "historical_alias_requires_review",
                        "Different historical governance IDs require a separate identity migration",
                    )
            status = "ignored" if write.ignore else "linked"
            connection.execute(
                "UPDATE directory_observation_candidate SET status=%s WHERE id=%s",
                (status, candidate_id),
            )
            self._audit(
                connection,
                principal,
                "candidate",
                str(candidate_id),
                "resolved",
                before=dict(candidate),
                after={
                    "status": status,
                    "person_id": str(write.person_id) if write.person_id else None,
                },
                reason=write.reason,
            )
            return {"id": str(candidate_id), "status": status}

        return self._mutate(
            principal,
            f"candidate:{candidate_id}",
            write.model_dump(),
            resolve,
        )
