from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

UTC = timezone.utc  # noqa: UP017
COUNTERS = ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "total_tokens")
MAX_TOKENS = 2**63 - 1


def exact_integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TOKENS:
        raise ValueError("Execution accounting requires an exact nonnegative int64")
    return value


def utc_time(value: Any) -> datetime:
    if not isinstance(value, (str, datetime)):
        raise ValueError("Execution evidence requires an aware timestamp")
    if isinstance(value, str):
        value = re.sub(
            r"(\d{2}:\d{2}:\d{2})\.(\d+)(?=[+-]\d{2}:\d{2}$)",
            lambda match: match[1] + "." + match[2][:6].ljust(6, "0"),
            value.replace("Z", "+00:00"),
        )
        moment = datetime.fromisoformat(value)
    else:
        moment = value
    if moment.utcoffset() is None:
        raise ValueError("Execution evidence requires an aware timestamp")
    return moment.astimezone(UTC)


def validate_completion(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {*COUNTERS, "source", "observed_at"} or value["source"] not in {
        "apim", "observer",
    }:
        raise ValueError("Only a complete trusted producer receipt can settle execution")
    counters = {key: exact_integer(value[key]) for key in COUNTERS}
    if (counters["cache_write_tokens"] > counters["cached_tokens"]
        or counters["input_tokens"] + counters["cached_tokens"] + counters["output_tokens"]
        != counters["total_tokens"]):
        raise ValueError("Completion usage breakdown does not match its total")
    return {**counters, "source": value["source"],
            "observed_at": utc_time(value["observed_at"]).isoformat()}


def normalized_identity(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "scope", "gateway_profile_id", "native_gateway_id", "correlation_id", "model_id",
        "admitted_at",
    }:
        raise ValueError("Execution identity fields do not match the protocol")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {"scope_type", "scope_id", "period_start"}:
        raise ValueError("Execution scope is malformed")
    if scope["scope_type"] not in {"person", "application"}:
        raise ValueError("Execution scope type is invalid")
    for text in (scope["scope_id"], value["native_gateway_id"], value["correlation_id"],
                 value["model_id"]):
        if not isinstance(text, str) or not text.strip() or len(text) > 255:
            raise ValueError("Execution identity is blank or too long")
    if scope["scope_type"] == "application" and str(UUID(scope["scope_id"])) != scope["scope_id"]:
        raise ValueError("Application scope must use its canonical UUID")
    admitted = utc_time(value["admitted_at"])
    if date.fromisoformat(scope["period_start"]) != admitted.date().replace(day=1):
        raise ValueError("Execution scope must retain the UTC admission month")
    return {**value, "gateway_profile_id": str(UUID(value["gateway_profile_id"])),
            "admitted_at": admitted.isoformat()}


def completion_transaction(
    summary: dict[str, Any], attempt: dict[str, Any], receipt: dict[str, Any],
    completion: dict[str, Any], now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = validate_completion(completion)
    identity = normalized_identity(receipt["identity"])
    if (summary.get("protocol_version") != 2 or type(summary.get("protocol_version")) is not int
        or summary.get("scope") != identity["scope"]
        or normalized_identity(attempt["identity"]) != identity
        or str(UUID(summary["epoch"])) != str(UUID(receipt["epoch"]))
        or str(UUID(attempt["epoch"])) != str(UUID(receipt["epoch"]))
        or exact_integer(attempt["reserved_tokens"]) != exact_integer(receipt["reserved_tokens"])
        or receipt["reserved_tokens"] == 0):
        raise ValueError("Completion does not match its immutable admission")
    if utc_time(evidence["observed_at"]) < utc_time(identity["admitted_at"]):
        raise ValueError("Completion cannot precede admission")
    for field in ("confirmed_tokens", "active_reserved_tokens", "upper_bound_tokens",
                  "token_limit", "version", "policy_revision"):
        exact_integer(summary[field])
    if type(summary["enforce"]) is not bool or type(summary["enabled"]) is not bool:
        raise ValueError("Execution policy booleans are malformed")
    state = attempt["state"]
    if state not in {"reserved", "dispatching", "uncertain", "upper_bound", "settled", "cancelled"}:
        raise ValueError("Execution state is invalid")
    previous = exact_integer(attempt["actual_tokens"]) if state == "settled" else 0
    if state != "settled" and attempt.get("actual_tokens") is not None:
        raise ValueError("Only settled attempts may have actual charges")
    prior_evidence = attempt.get("completion_evidence")
    if prior_evidence is not None:
        retained = validate_completion(prior_evidence)
        if (state != "settled" or previous != retained["total_tokens"]
            or attempt.get("evidence_rank") != 3):
            raise ValueError("Retained completion must match the actual settled charge")
        if any(retained[key] != evidence[key] for key in COUNTERS):
            raise ValueError("Conflicting complete producer evidence requires review")
        return summary, attempt
    if (state == "settled" and attempt.get("evidence_rank", 3) == 3
        and previous != evidence["total_tokens"]):
        raise ValueError("Conflicting complete accounting evidence requires review")
    reserved = receipt["reserved_tokens"]
    active = reserved if state in {"reserved", "dispatching", "uncertain"} else 0
    upper = reserved if state == "upper_bound" else 0
    changed = {
        "confirmed_tokens": summary["confirmed_tokens"] + evidence["total_tokens"] - previous,
        "active_reserved_tokens": summary["active_reserved_tokens"] - active,
        "upper_bound_tokens": summary["upper_bound_tokens"] - upper,
        "version": summary["version"] + 1,
    }
    for value in changed.values():
        exact_integer(value)
    updated_at = max(utc_time(now), utc_time(summary["updated_at"]),
                     utc_time(attempt["updated_at"])).isoformat()
    key = hashlib.sha256(json.dumps(
        [evidence[name] for name in COUNTERS], separators=(",", ":"),
    ).encode()).hexdigest()
    return (
        {**summary, **changed, "updated_at": updated_at},
        {**attempt, "state": "settled", "actual_tokens": evidence["total_tokens"],
         "evidence_revision": max(exact_integer(attempt["evidence_revision"]), 1),
         "evidence_key": "completion:" + key, "evidence_rank": 3,
         "completion_evidence": evidence, "updated_at": updated_at},
    )