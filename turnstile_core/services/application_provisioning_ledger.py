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
    application_quota_entity,
    model_access_value,
)
from ..persistence.repository import QueryRepository


def prepare_application_ledger(
    repository: QueryRepository,
    store: TableStorageLedger,
    gateway_id: UUID,
    application_id: UUID,
) -> None:
    now = datetime.now(UTC)
    service = LedgerSyncService(repository, store)
    snapshot = next(
        (
            item
            for item in service.application_snapshot(now)
            if item.application_id == str(application_id)
        ),
        None,
    )
    mappings = [
        item
        for item in repository.gateway_application_ledger_mappings()
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
    policy = next(
        (
            item
            for item in service.application_model_access()
            if item.application_id == str(application_id)
        ),
        None,
    )
    identifiers = model_access_value(policy.identifiers) if policy else ""
    if len(identifiers) > MODEL_PROPERTY_LIMIT:
        raise PolicyCompilationError("Application model access exceeds the ledger property limit")
    partition = application_partition_key(application_id, now.strftime("%Y-%m"))
    quota = application_quota_entity(snapshot)
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
