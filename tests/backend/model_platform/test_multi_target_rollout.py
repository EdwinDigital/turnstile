from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from tests.backend.model_platform.control_plane_support import (
    TEST_CIPHER,
    activate_publication,
    bedrock_publication,
    foundry_publication,
    transition_publication,
)
from tests.backend.model_platform.test_gateway_targets import NOW, TARGET, target
from turnstile_core.domain.control_plane import (
    GatewayPublicationCreate,
    ModelCreateTarget,
    MultiTargetPublicationChildCreate,
    MultiTargetPublicationCreate,
)
from turnstile_core.domain.gateway_targets import (
    MultiTargetRollout,
    TargetRolloutChild,
    TargetRolloutChildStatus,
    TargetRolloutKind,
    TargetRolloutStatus,
    derive_target_rollout_status,
)
from turnstile_core.domain.runtime_models import (
    PriceCatalogContextTier,
    PriceCatalogInputRange,
    PriceSource,
    ProviderTarget,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_support import (
    _activation_runtime_config,
    _require_multi_target_model_identity,
)
from turnstile_core.pricing.catalog import (
    CatalogEntry,
    CatalogModel,
    CatalogOption,
    CatalogOptions,
    CompositeCatalog,
)
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
    GatewayControlPlaneService,
)


def multi_request(target_ids: list[UUID]) -> MultiTargetPublicationCreate:
    return MultiTargetPublicationCreate(
        children=tuple(
            MultiTargetPublicationChildCreate(
                publication=GatewayPublicationCreate.model_validate(
                    {
                        **bedrock_publication().model_dump(),
                        "gateway_profile_id": target_id,
                    }
                )
            )
            for target_id in target_ids
        )
    )


def test_multi_target_service_binds_stable_identity_and_canonical_manual_prices() -> None:
    repository, children = rollout_fixture()
    service = GatewayControlPlaneService(repository, TEST_CIPHER, multi_target_rollout_enabled=True)
    write = multi_request([child["gateway_profile_id"] for child in children])
    price_fields = (
        "input_cost_per_million",
        "output_cost_per_million",
        "cached_cost_per_million",
        "cache_write_cost_per_million",
    )
    for index, child in enumerate(write.children):
        for offset, name in enumerate(price_fields):
            setattr(child.publication.model, name, float(index + offset + 1))
    rollout, publications = service.publish_multi_target(write, "unit-owner")
    assert rollout.status == "queued" and len(publications) == 2
    models = [publication.desired_spec.bindings[-1].model for publication in publications]
    canonical_input = min(
        write.children, key=lambda child: str(child.publication.gateway_profile_id)
    )
    for model in models:
        assert all(
            getattr(model, name) == getattr(canonical_input.publication.model, name)
            for name in price_fields
        )
    assert (
        len({publication.desired_spec.bindings[-1].runtime_name for publication in publications})
        == 2
    )
    assert {
        publication.desired_spec.bindings[-1].logical_model_id for publication in publications
    } == {rollout.logical_model_id}
    repeated, _ = service.publish_multi_target(
        MultiTargetPublicationCreate(children=tuple(reversed(write.children))),
        "other-owner",
    )
    assert repeated.id == rollout.id and len(repository.gateway_publications) == 2
    assert len(service.multi_target_rollouts()) == 1
    repository.gateway_publications[0]["status"] = "active"
    repository.gateway_publications[1]["status"] = "failed"
    assert service.multi_target_rollout(rollout.id)[0].status == "partial"


def test_multi_target_canonical_prices_include_complete_catalog_tiers_and_discounts() -> None:
    repository, children = rollout_fixture()
    catalog = MagicMock(spec=CompositeCatalog)
    entries = {}
    references = []
    for index in range(2):
        candidate = CatalogModel(
            key=f"azure_retail:Unit:Tier{index}",
            label=f"Tier {index}",
            product="Unit",
            source=PriceSource.AZURE_RETAIL,
            context_tier=PriceCatalogContextTier(
                group_key="unit-model",
                model_label="Unit model",
                key=str(index),
                label=f"Tier {index}",
                order=index,
                input_range=PriceCatalogInputRange(
                    min_input_tokens=0 if index == 0 else 1201,
                    max_input_tokens=1200 if index == 0 else None,
                ),
            ),
        )
        reference = f"{candidate.key}:Global:*"
        references.append(reference)
        price = CatalogEntry(
            reference=reference,
            label=candidate.label,
            source=candidate.source,
            input_per_million=2 + index,
            output_per_million=8 + index,
            cached_per_million=0.5 + index,
            cache_write_per_million=3 + index,
        )
        entries[candidate.key] = CatalogOptions(
            model=candidate,
            options=(
                CatalogOption(
                    reference=reference,
                    deployment="Global",
                    entry=price,
                    regions=("unit-region",),
                    region_required=False,
                ),
            ),
        )
    catalog.options.side_effect = entries.get
    requests = []
    for index, child in enumerate(children):
        write = foundry_publication("unit-context-model")
        write.gateway_profile_id = child["gateway_profile_id"]
        write.provider = ProviderTarget(
            existing_id=next(
                row["id"] for row in repository.providers if row["brand_key"] == "microsoft_foundry"
            )
        )
        write.model = ModelCreateTarget.model_validate(
            {
                **write.model.model_dump(),
                "price_source": "azure_retail",
                "context_price_references": references,
                "price_discount_percent": 80 - index * 20,
            }
        )
        requests.append(MultiTargetPublicationChildCreate(publication=write))
    service = GatewayControlPlaneService(
        repository,
        TEST_CIPHER,
        apim_principal_id=str(UUID(int=99)),
        price_catalog=catalog,
        multi_target_rollout_enabled=True,
    )
    _, publications = service.publish_multi_target(
        MultiTargetPublicationCreate(children=tuple(requests)),
        "owner",
    )
    models = [publication.desired_spec.bindings[-1].model for publication in publications]
    assert all(model.price_source == PriceSource.AZURE_RETAIL for model in models)
    assert all(model.price_discount_percent == 60 for model in models)
    assert all(model.input_cost_per_million is None for model in models)
    assert models[0].context_price_tiers == models[1].context_price_tiers
    assert models[0].context_price_tiers is not models[1].context_price_tiers
    assert [tier.input_price_per_million for tier in models[0].context_price_tiers] == [1.2, 1.8]
    assert models[0].context_price_references == models[1].context_price_references == references
    promote_and_activate(repository, publications[0].id)
    original = deepcopy(repository.models[0])
    promote_and_activate(repository, publications[1].id)
    assert repository.models == [original]
    assert len(repository.list_apim_target_route_bindings()) == 2


def promote_and_activate(repository: InMemoryRepository, publication_id: UUID) -> None:
    for before, after in zip(
        ("queued", "validating", "provisioning", "building_revision", "verifying"),
        ("validating", "provisioning", "building_revision", "verifying", "promoting"),
        strict=True,
    ):
        transition_publication(repository, publication_id, before, after, {}, "unit-worker")
    activate_publication(repository, publication_id, "unit-worker")


def test_multi_target_activation_creates_one_model_and_independent_routes() -> None:
    repository, children = rollout_fixture()
    service = GatewayControlPlaneService(repository, TEST_CIPHER, multi_target_rollout_enabled=True)
    rollout, publications = service.publish_multi_target(
        multi_request([child["gateway_profile_id"] for child in children]),
        "owner",
    )
    promote_and_activate(repository, publications[0].id)
    original = deepcopy(repository.models[0])
    promote_and_activate(repository, publications[1].id)
    assert len(repository.models) == 1 and repository.models[0] == original
    assert repository.models[0]["id"] == rollout.logical_model_id
    routes = repository.list_apim_target_route_bindings()
    assert len(routes) == 2
    assert sum(row["is_default_serving"] for row in routes) == 1
    assert {row["current_publication_id"] for row in routes} == {row.id for row in publications}
    assert len(repository.effective_gateway_releases) == 2
    assert service.multi_target_rollout(rollout.id)[0].status == "succeeded"


@pytest.mark.parametrize(
    "field",
    [
        "price_source",
        "price_reference",
        "price_discount_percent",
        "input_cost_per_million",
        "output_cost_per_million",
        "cached_cost_per_million",
        "cache_write_cost_per_million",
        "list_input_cost_per_million",
        "list_output_cost_per_million",
        "list_cached_cost_per_million",
        "list_cache_write_cost_per_million",
        "context_price_tiers",
    ],
)
def test_multi_target_activation_rejects_any_price_identity_drift_without_repricing(
    field: str,
) -> None:
    repository, children = rollout_fixture()
    service = GatewayControlPlaneService(repository, TEST_CIPHER, multi_target_rollout_enabled=True)
    _, publications = service.publish_multi_target(
        multi_request([child["gateway_profile_id"] for child in children]),
        "owner",
    )
    promote_and_activate(repository, publications[0].id)
    original = deepcopy(repository.models[0])
    second = next(row for row in repository.gateway_publications if row["id"] == publications[1].id)
    values = second["desired_spec"]["bindings"][-1]["model"]
    values[field] = (
        [{"different": True}]
        if field == "context_price_tiers"
        else (
            "anthropic"
            if field == "price_source"
            else "anthropic:unit:other"
            if field == "price_reference"
            else 77.0
        )
    )
    with pytest.raises(ValueError, match="logical model identity"):
        promote_and_activate(repository, publications[1].id)
    assert repository.models == [original]
    assert len(repository.list_apim_target_route_bindings()) == 1
    assert publications[1].gateway_profile_id not in repository.effective_gateway_releases


@pytest.mark.parametrize("price_changed", [False, True])
def test_postgres_activation_reads_full_price_identity_and_never_reprices_existing_model(
    price_changed: bool,
) -> None:
    memory, children = rollout_fixture()
    parent = memory.create_multi_target_rollout(
        UUID(int=200),
        "model_add",
        UUID(int=201),
        "a" * 64,
        "owner",
        children,
    )
    publication = deepcopy(parent["children"][0]["publication"])
    publication["status"] = "promoting"
    binding = publication["desired_spec"]["bindings"][-1]
    model = {**deepcopy(binding["model"]), "id": UUID(int=201), "publication_id": UUID(int=900)}
    if price_changed:
        model["price_source"] = "anthropic"
    provider_id = UUID(int=700)
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    statements = []

    def execute(sql: str, values: Any = None) -> MagicMock:
        statements.append(sql)
        result = MagicMock()
        row: dict[str, Any] | None = None
        if "FROM gateway_publication_outbox" in sql:
            row = {"id": UUID(int=701)}
        elif "SELECT * FROM gateway_publication WHERE id" in sql:
            row = publication
        elif "INSERT INTO model_provider (" in sql:
            row = {
                "id": provider_id,
                "enabled": True,
                "provider_kind": binding["provider_kind"],
                "config": binding["provider_config"],
            }
        elif "INSERT INTO model_runtime (" in sql:
            row = {
                "id": UUID(int=702),
                "enabled": True,
                "provider_id": provider_id,
                "gateway_profile_id": publication["gateway_profile_id"],
                "runtime_kind": binding["runtime_kind"],
                "config": _activation_runtime_config(binding),
            }
        elif "LEFT JOIN managed_model_price price" in sql:
            row = model
        result.fetchone.return_value = row
        return result

    connection.execute.side_effect = execute
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with (
        patch.object(repository, "_connection", return_value=context),
        patch.object(
            repository, "_complete_gateway_activation", return_value=publication
        ) as complete,
        patch.object(repository, "_invalidate_identities"),
    ):
        if price_changed:
            with pytest.raises(ValueError, match="logical model identity"):
                repository.activate_gateway_publication(publication["id"], "worker")
            complete.assert_not_called()
        else:
            assert (
                repository.activate_gateway_publication(publication["id"], "worker") == publication
            )
            complete.assert_called_once()
    query = next(sql for sql in statements if "LEFT JOIN managed_model_price price" in sql)
    for field_name in (
        "price_source",
        "price_reference",
        "price_discount_percent",
        "list_input_cost_per_million",
        "list_output_cost_per_million",
        "list_cached_cost_per_million",
        "list_cache_write_cost_per_million",
        "context_price_tiers",
    ):
        assert field_name in query
    assert query.count("DOUBLE PRECISION") == 5
    assert all(
        not sql.startswith(("INSERT INTO managed_model", "UPDATE managed_model"))
        for sql in statements
    )


def test_multi_target_removal_preserves_model_prices_and_access_until_last_target() -> None:
    repository, children = rollout_fixture()
    service = GatewayControlPlaneService(repository, TEST_CIPHER, multi_target_rollout_enabled=True)
    rollout, publications = service.publish_multi_target(
        multi_request([child["gateway_profile_id"] for child in children]),
        "owner",
    )
    for publication in publications:
        promote_and_activate(repository, publication.id)
    model_id = rollout.logical_model_id
    assert model_id is not None
    first_runtime = repository.models[0]["runtime_id"]
    original_prices = {
        key: value
        for key, value in repository.models[0].items()
        if "price" in key or "cost_per_million" in key
    }
    repository.user_model_policies["unit-person"] = {"model_ids": [model_id]}
    repository.assistant_setting["model_id"] = model_id
    removal = service.remove_model(model_id, "owner")
    promote_and_activate(repository, removal.id)
    assert len(repository.models) == 1
    assert repository.models[0]["runtime_id"] != first_runtime
    assert {
        key: value
        for key, value in repository.models[0].items()
        if "price" in key or "cost_per_million" in key
    } == original_prices
    assert repository.user_model_policies["unit-person"]["model_ids"] == [model_id]
    assert repository.assistant_setting["model_id"] == model_id
    routes = repository.list_apim_target_route_bindings()
    assert len(routes) == 1 and routes[0]["is_default_serving"]
    final = service.remove_model(model_id, "owner")
    assert final.gateway_profile_id != removal.gateway_profile_id
    promote_and_activate(repository, final.id)
    assert repository.models == [] and repository.list_apim_target_route_bindings() == []
    assert repository.user_model_policies["unit-person"]["model_ids"] == []
    assert repository.assistant_setting["model_id"] is None
    assert len(repository.gateway_publications) == 4


def test_logical_model_identity_comparison_never_accepts_only_a_matching_alias() -> None:
    repository, children = rollout_fixture()
    spec = children[0]["desired_spec"]["bindings"][-1]["model"]
    _require_multi_target_model_identity({**spec, "id": UUID(int=201)}, spec, UUID(int=201))
    for identity in (None, UUID(int=202)):
        with pytest.raises(ValueError, match="alias is already active"):
            _require_multi_target_model_identity({**spec, "id": UUID(int=201)}, spec, identity)


def test_multi_target_rejects_distinct_explicit_identities_before_queueing() -> None:
    repository, children = rollout_fixture()
    write = multi_request([child["gateway_profile_id"] for child in children])
    with pytest.raises(ControlPlaneUnavailableError):
        GatewayControlPlaneService(repository, TEST_CIPHER).publish_multi_target(write, "owner")
    write.children[1].publication.model.model_key = "different-model"
    with pytest.raises(ControlPlaneConflictError, match="one logical model identity"):
        GatewayControlPlaneService(
            repository, TEST_CIPHER, multi_target_rollout_enabled=True
        ).publish_multi_target(write, "owner")
    assert repository.gateway_publications == [] and repository.multi_target_rollouts == []


def rollout_fixture() -> tuple[InMemoryRepository, list[dict[str, Any]]]:
    repository = InMemoryRepository()
    repository.models = []
    service = GatewayControlPlaneService(repository, TEST_CIPHER)
    publication = service.publish(bedrock_publication(), "unit-owner")
    spec = publication.desired_spec.model_dump(mode="json")
    repository.gateway_publications.clear()
    repository.gateway_publication_outbox.clear()
    repository.gateway_publication_audit.clear()
    repository.gateway_publication_secrets.clear()
    other_id = UUID(int=92)
    repository.upsert_apim_target(target())
    repository.gateways.append({**repository.gateways[0], "id": other_id, "name": "Other APIM"})
    repository.upsert_apim_target(
        target(
            gateway_profile_id=other_id,
            service_name="other-service",
            arm_resource_id=target().arm_resource_id.replace("unit-service", "other-service"),
        )
    )
    children = []
    for index, identity in enumerate((TARGET, other_id)):
        desired = deepcopy(spec)
        desired["gateway_profile_id"] = str(identity)
        desired["bindings"][-1]["logical_model_id"] = str(UUID(int=201))
        children.append(
            {
                "gateway_profile_id": identity,
                "desired_spec": desired,
                "desired_spec_sha256": str(index) * 64,
                "required": index == 0,
                "credential_ciphertext": b"unit-encrypted",
            }
        )
    return repository, children


def test_multi_target_persistence_is_idempotent_and_returns_detached_current_children() -> None:
    repository, children = rollout_fixture()
    rollout = repository.create_multi_target_rollout(
        UUID(int=200),
        "model_add",
        UUID(int=201),
        "a" * 64,
        "unit-owner",
        children,
    )
    repeated = repository.create_multi_target_rollout(
        UUID(int=200),
        "model_add",
        UUID(int=201),
        "a" * 64,
        "unit-owner",
        children,
    )
    assert repeated == rollout
    assert len(repository.gateway_publications) == len(repository.gateway_publication_outbox) == 2
    assert len(repository.gateway_publication_secrets) == 2
    repository.gateway_publications[0]["status"] = "failed"
    current = repository.get_multi_target_rollout(rollout["id"])
    assert current is not None
    assert any(child["publication_status"] == "failed" for child in current["children"])
    current["children"][0]["publication"]["status"] = "not-persisted"
    assert all(row["status"] != "not-persisted" for row in repository.gateway_publications)
    assert repository.list_multi_target_rollouts(1)[0]["id"] == rollout["id"]


@pytest.mark.parametrize("failure", ["unregistered", "disabled", "intent", "mid_insert"])
def test_multi_target_persistence_never_leaves_partial_queue_state(failure: str) -> None:
    repository, children = rollout_fixture()
    second = children[1]["gateway_profile_id"]
    if failure == "unregistered":
        del repository.apim_targets[second]
    elif failure == "disabled":
        next(row for row in repository.gateways if row["id"] == second)["enabled"] = False
    elif failure == "intent":
        children[1]["desired_spec"]["gateway_profile_id"] = str(TARGET)
    original = repository.create_gateway_publication
    calls = []

    def create(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(args[0])
        if failure == "mid_insert" and len(calls) == 2:
            raise ValueError("Simulated second child failure")
        return original(*args, **kwargs)

    with (
        patch.object(repository, "create_gateway_publication", side_effect=create),
        pytest.raises(ValueError),
    ):
        repository.create_multi_target_rollout(
            UUID(int=200),
            "model_add",
            UUID(int=201),
            "a" * 64,
            "unit-owner",
            children,
        )
    assert repository.gateway_publications == []
    assert repository.gateway_publication_outbox == []
    assert repository.gateway_publication_audit == []
    assert repository.gateway_publication_secrets == {}
    assert repository.multi_target_rollouts == repository.multi_target_rollout_children == []


@pytest.mark.parametrize("second_available", [False, True])
def test_postgres_rollout_checks_all_targets_under_sorted_locks_before_inserts(
    second_available: bool,
) -> None:
    _, children = rollout_fixture()
    connection, context = MagicMock(), MagicMock()
    context.__enter__.return_value = connection
    calls = []
    publications: list[dict[str, Any]] = []

    def execute(sql: str, values: Any = None) -> MagicMock:
        calls.append((sql, values))
        result = MagicMock()
        actual = None
        if "FROM gateway_profile gateway" in sql:
            enabled = second_available or values[0] == TARGET
            actual = {"id": values[0], "enabled": enabled, "implementation": "apim"}
        elif "INSERT INTO multi_target_rollout (" in sql:
            actual = {"id": UUID(int=200)}
        elif "MAX(generation)" in sql:
            actual = {"generation": 1}
        elif "INSERT INTO gateway_publication (" in sql:
            actual = {"id": UUID(int=401 + len(publications))}
            publications.append(actual)
        result.fetchone.return_value = actual
        return result

    connection.execute.side_effect = execute
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with (
        patch.object(repository, "_connection", return_value=context),
        patch.object(repository, "_multi_target_rollout_view", return_value={"id": UUID(int=200)}),
    ):
        if second_available:
            result = repository.create_multi_target_rollout(
                UUID(int=200),
                "model_add",
                UUID(int=201),
                "a" * 64,
                "unit-owner",
                children,
            )
            assert result["id"] == UUID(int=200)
        else:
            with pytest.raises(ValueError, match="unavailable"):
                repository.create_multi_target_rollout(
                    UUID(int=200),
                    "model_add",
                    UUID(int=201),
                    "a" * 64,
                    "unit-owner",
                    children,
                )
    locks = [(sql, values) for sql, values in calls if "pg_advisory_xact_lock" in sql]
    assert [values for _, values in locks] == [
        (f"gateway-publication:{identity}",)
        for identity in sorted([child["gateway_profile_id"] for child in children], key=str)
    ]
    assert calls[:2] == locks
    connection.transaction.assert_called_once()
    inserts = [index for index, (sql, _) in enumerate(calls) if sql.startswith("INSERT")]
    if second_available:
        checks = [index for index, (sql, _) in enumerate(calls) if "FROM gateway_profile" in sql]
        assert len(checks) == 2 and max(checks) < min(inserts)
        assert len(publications) == 2
        assert sum("INSERT INTO gateway_publication_outbox" in sql for sql, _ in calls) == 2
        assert sum("INSERT INTO multi_target_rollout_child" in sql for sql, _ in calls) == 2
    else:
        assert inserts == []


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["queued", "queued"], "queued"),
        (["succeeded", "queued"], "in_progress"),
        (["failed", "in_progress"], "in_progress"),
        (["failed", "rolling_back"], "rolling_back"),
        (["succeeded", "succeeded"], "succeeded"),
        (["succeeded", "failed"], "partial"),
        (["rolled_back", "rolled_back"], "rolled_back"),
        (["failed", "rolled_back"], "failed"),
    ],
)
def test_rollout_status_is_derived_from_every_child(statuses: list[str], expected: str) -> None:
    children = tuple(
        TargetRolloutChild(
            apim_target_id=UUID(int=index + 1),
            publication_id=UUID(int=index + 101),
            status=TargetRolloutChildStatus(status),
            required=index == 0,
        )
        for index, status in enumerate(statuses)
    )
    assert derive_target_rollout_status(children).value == expected
    rollout = MultiTargetRollout(
        id=UUID(int=41),
        kind=TargetRolloutKind.MODEL_ADD,
        logical_model_id=UUID(int=42),
        status=TargetRolloutStatus(expected),
        children=children,
        created_by="unit-owner",
        created_at=NOW,
    )
    assert rollout.children == children
    if expected != "succeeded":
        with pytest.raises(ValueError, match="must be derived"):
            MultiTargetRollout.model_validate({**rollout.model_dump(), "status": "succeeded"})
    with pytest.raises(ValueError, match="logical model identity"):
        MultiTargetRollout.model_validate({**rollout.model_dump(), "logical_model_id": None})


def test_multi_target_request_requires_two_to_twenty_distinct_targets() -> None:
    children = tuple(
        MultiTargetPublicationChildCreate(
            publication=bedrock_publication().model_copy(
                update={"gateway_profile_id": UUID(int=index + 1)},
            )
        )
        for index in range(21)
    )
    assert len(MultiTargetPublicationCreate(children=children[:2]).children) == 2
    for values in ((), children[:1], (children[0], children[0]), children):
        with pytest.raises(ValueError):
            MultiTargetPublicationCreate(children=values)
    with pytest.raises(ValueError):
        derive_target_rollout_status(())
