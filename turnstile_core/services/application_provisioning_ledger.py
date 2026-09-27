from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.ledger import (
    MODEL_PROPERTY_LIMIT,
    LedgerSyncService,
    TableStorageLedger,
    application_map_partition_key,
    application_partition_key,
    model_access_value,
)
from ..persistence.repository import QueryRepository


def read_application_rate_limit(store: TableStorageLedger, application_id: UUID) -> int | None:
    partition = application_partition_key(application_id, datetime.now(UTC).strftime("%Y-%m"))
    current = store.read_versioned_entity(partition, "Q")
    if current is None:
        return None
    value = current[0].get("TokensPerMinute")
    if type(value) is not int or not 0 < value <= 2147483647:
        raise ValueError("Application rate limit configuration is invalid")
    return value


def project_application_rate_limit(
    repository: QueryRepository, store: TableStorageLedger, application_id: UUID,
) -> int | None:
    for _attempt in range(3):
        period = datetime.now(UTC).date().replace(day=1)
        partition = application_partition_key(application_id, period.strftime("%Y-%m"))
        current = store.read_versioned_entity(partition, "Q")
        budgets = repository.list_gateway_application_budgets(period, [application_id])
        if not budgets:
            return None
        if len(budgets) != 1 or str(budgets[0]["application_id"]) != str(application_id):
            raise ValueError("Application rate limit identity is ambiguous")
        budget = budgets[0]
        rate = budget["tokens_per_minute"]
        if type(rate) is not int or not 0 < rate <= 2147483647:
            raise ValueError("Application rate limit configuration is invalid")
        if current is None:
            raise RetryablePublicationError("Application admission ledger is not ready")
        unchanged = current[0].get("TokensPerMinute") == rate
        entity = {key: value for key, value in current[0].items()
                  if key not in {"Timestamp", "odata.etag", "@odata.etag"}}
        if unchanged or store.replace_if_match(partition, "Q", {
            **entity, "TokensPerMinute": rate,
        }, current[1]):
            if read_application_rate_limit(store, application_id) != rate:
                continue
            if (datetime.now(UTC).date().replace(day=1) == period
                    and repository.list_gateway_application_budgets(period, [application_id])
                    == budgets):
                return rate
    raise RetryablePublicationError("Application rate limit projection is pending")


def prepare_application_ledger(
    repository: QueryRepository,
    store: TableStorageLedger,
    gateway_id: UUID,
    application_id: UUID,
) -> None:
    now = datetime.now(UTC)
    service = LedgerSyncService(repository, store)
    snapshot = next((
        item for item in service.application_snapshot(now)
        if item.application_id == str(application_id)
    ), None)
    mappings = [
        item for item in repository.gateway_application_ledger_mappings()
        if str(item["application_id"]) == str(application_id)
        and str(item["gateway_profile_id"]) == str(gateway_id)
    ]
    if snapshot is None or not mappings:
        raise RetryablePublicationError("Application budget or attribution is not ready")
    if len(mappings) != 1:
        raise PolicyCompilationError("Application creation must have exactly one subscription")
    mapping = mappings[0]
    if (
        mapping["application_status"] != "active"
        or mapping["subscription_state"] != "active"
        or not mapping["scope_exists"]
    ):
        raise PolicyCompilationError("Application was disabled or its subscription scope changed")
    policy = next((
        item for item in service.application_model_access()
        if item.application_id == str(application_id)
    ), None)
    identifiers = model_access_value(policy.identifiers) if policy else ""
    if len(identifiers) > MODEL_PROPERTY_LIMIT:
        raise PolicyCompilationError("Application model access exceeds the ledger property limit")
    partition = application_partition_key(application_id, now.strftime("%Y-%m"))
    quota = {
        "Limit": snapshot.token_limit,
        "TokensPerMinute": snapshot.tokens_per_minute,
        "Enforce": snapshot.enforce,
    }
    access = {"Configured": policy is not None, "Models": identifiers}
    identity = {
        "ApplicationId": str(application_id),
        "ApplicationSlug": str(mapping["application_slug"]),
        "ApplicationName": str(mapping["application_name"]),
        "ApplicationType": str(mapping["application_type"]),
        "ApplicationStatus": str(mapping["application_status"]),
        "SubscriptionState": str(mapping["subscription_state"]),
        "ScopeExists": bool(mapping["scope_exists"]),
    }
    map_partition = application_map_partition_key(gateway_id)
    subscription_id = str(mapping["apim_subscription_id"])
    store.upsert(partition, "Q", quota)
    store.insert_if_missing(partition, "C", {"ConfirmedUsed": snapshot.confirmed_tokens})
    store.upsert(partition, "M", access)
    store.upsert(map_partition, subscription_id, identity)
    for key, expected in (("Q", quota), ("M", access)):
        actual = store.read_entity(partition, key)
        if actual is None or any(actual.get(field) != value for field, value in expected.items()):
            raise RetryablePublicationError("Application admission ledger readback is incomplete")
    confirmed = store.read_entity(partition, "C")
    actual_mapping = store.read_entity(map_partition, subscription_id)
    if (
        confirmed is None
        or int(confirmed.get("ConfirmedUsed", -1)) < 0
        or actual_mapping is None
        or any(actual_mapping.get(field) != value for field, value in identity.items())
    ):
        raise RetryablePublicationError("Application attribution ledger readback is incomplete")