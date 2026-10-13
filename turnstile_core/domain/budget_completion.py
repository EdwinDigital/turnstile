# ruff: noqa: UP017 - Shared with the Observer's Python 3.10 runtime.
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import UUID

UTC = timezone.utc
COUNTERS = ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "total_tokens")
MAX_TOKENS = 2**63 - 1
REJECTION_OPERATIONS = frozenset({
    "responses", "responses-compact", "chat-completions", "anthropic-messages",
    "images-generations",
    "microsoft-decision",
})
REJECTION_FIELDS = ("error_type", "error_code", "error_param")


def _error_field(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("state") in ("absent", "null"):
        return set(value) == {"state"}
    return (
        set(value) == {"state", "value"} and value["state"] == "value"
        and isinstance(value["value"], str)
        and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,255}", value["value"]) is not None
    )


def _rejection_digest(value: dict[str, Any]) -> str:
    parts = [value["operation"], value["status_code"]]
    for name in REJECTION_FIELDS:
        parts.extend((value[name]["state"], value[name].get("value")))
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def normalize_terminal_rejection(
    operation: str, status_code: int, content_type: str, body: Any,
) -> dict[str, Any] | None:
    if (
        not isinstance(operation, str) or operation not in REJECTION_OPERATIONS
        or type(status_code) is not int
        or not 400 <= status_code < 500 or status_code in {408, 499}
        or not isinstance(content_type, str)
        or content_type.split(";", 1)[0].strip().lower() != "application/json"
        or not isinstance(body, dict) or not isinstance(body.get("error"), dict)
        or set(body) - {"error", "type", "request_id"}
        or ("type" in body and body["type"] != "error")
        or ("request_id" in body and not isinstance(body["request_id"], str))
    ):
        return None
    error = body["error"]
    if set(error) & {"usage", "output", "content", "partial_response"}:
        return None
    observation: dict[str, Any] = {"version": 2, "operation": operation,
                                   "status_code": status_code}
    for name in REJECTION_FIELDS:
        field = name.removeprefix("error_")
        observation[name] = (
            {"state": "absent"} if field not in error
            else {"state": "null"} if error[field] is None
            else {"state": "value", "value": error[field]}
        )
    rule = {key: observation[key] for key in ("status_code", *REJECTION_FIELDS)}
    if not _rejection_rule(rule, 2):
        return None
    observation["observation_sha256"] = _rejection_digest(observation)
    return observation


def validate_terminal_rejection(value: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "operation", "status_code", *REJECTION_FIELDS,
                       "observation_sha256"}
        or type(value["version"]) is not int or value["version"] != 2
        or not isinstance(value["operation"], str) or value["operation"] not in REJECTION_OPERATIONS
        or not _rejection_rule({key: value[key] for key in (
            "status_code", *REJECTION_FIELDS,
        )}, 2)
        or value["observation_sha256"] != _rejection_digest(value)
    ):
        raise ValueError("Terminal response evidence has an invalid protocol or field shape")
    return {**value, **{name: dict(value[name]) for name in REJECTION_FIELDS}}


def validate_terminal_observation(value: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict) or set(value) != {"source", "observed_at", "rejection"}
        or value["source"] not in ("apim", "observer")
    ):
        raise ValueError("Terminal observations require a trusted producer and exact fields")
    return {
        "source": value["source"], "observed_at": utc_time(value["observed_at"]).isoformat(),
        "rejection": validate_terminal_rejection(value["rejection"]),
    }


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
    fields = {*COUNTERS, "source", "observed_at"}
    if "rejection" in value:
        fields.add("rejection")
    if set(value) != fields or value["source"] not in (
        "apim",
        "observer",
    ):
        raise ValueError("Only a complete trusted producer receipt can settle execution")
    counters = {key: exact_integer(value[key]) for key in COUNTERS}
    if (
        counters["cache_write_tokens"] > counters["cached_tokens"]
        or counters["input_tokens"] + counters["cached_tokens"] + counters["output_tokens"]
        != counters["total_tokens"]
    ):
        raise ValueError("Completion usage breakdown does not match its total")
    result = {
        **counters,
        "source": value["source"],
        "observed_at": utc_time(value["observed_at"]).isoformat(),
    }
    if "rejection" in value:
        rejection = value["rejection"]
        if (
            any(counters.values()) or not isinstance(rejection, dict)
            or not isinstance(rejection.get("policy_evidence_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", rejection["policy_evidence_sha256"])
        ):
            raise ValueError("Rejection evidence must retain a declared zero-charge decision")
        observation = {key: item for key, item in rejection.items()
                       if key != "policy_evidence_sha256"}
        if observation.get("version") == 2:
            validate_terminal_rejection(observation)
        elif not _rejection_rule(observation):
            raise ValueError("Rejection evidence has an invalid error contract")
        result["rejection"] = dict(rejection)
    return result


def _rejection_rule(value: Any, version: int = 1) -> bool:
    if version == 2:
        return (
            isinstance(value, dict) and set(value) == {"status_code", *REJECTION_FIELDS}
            and type(value["status_code"]) is int and 400 <= value["status_code"] < 500
            and value["status_code"] not in {408, 499}
            and all(_error_field(value[name]) for name in REJECTION_FIELDS)
            and any(value[name]["state"] == "value" for name in ("error_type", "error_code"))
        )
    return (
        isinstance(value, dict)
        and set(value) == {"status_code", "error_type", "error_code"}
        and type(value["status_code"]) is int and value["status_code"] in {400, 422}
        and all(isinstance(value[key], str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value[key])
                for key in ("error_type", "error_code"))
    )


def validate_rejection_policy(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Rejection settlement requires an object contract")
    version = value.get("version")
    fields = {"version", "evidence_sha256", "verified_at", "rules"}
    if version == 2:
        fields.add("operation")
    if (
        set(value) != fields or type(version) is not int or version not in {1, 2}
        or (version == 2 and (
            not isinstance(value["operation"], str)
            or value["operation"] not in REJECTION_OPERATIONS
        ))
        or not isinstance(value["evidence_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["evidence_sha256"])
        or not isinstance(value["rules"], list) or not 1 <= len(value["rules"]) <= 16
        or not all(_rejection_rule(rule, version) for rule in value["rules"])
    ):
        raise ValueError("Rejection settlement requires an explicit versioned provider contract")
    rules = value["rules"]
    if len({json.dumps(rule, sort_keys=True) for rule in rules}) != len(rules):
        raise ValueError("Rejection settlement rules must be unique")
    return {**value, "verified_at": utc_time(value["verified_at"]).isoformat(),
            "rules": [dict(rule) for rule in rules]}


def rejection_completion(
    policy: dict[str, Any] | None, rejection: dict[str, Any], *, source: str,
    observed_at: str | datetime, admitted_at: str | datetime,
) -> dict[str, Any] | None:
    if policy is None:
        return None
    checked = validate_rejection_policy(policy)
    if not utc_time(checked["verified_at"]) <= utc_time(admitted_at) <= utc_time(observed_at):
        raise ValueError("Rejection policy must precede admission and evidence must follow it")
    if checked["version"] == 1:
        if rejection.get("version") == 2:
            observation = validate_terminal_rejection(rejection)
            if any(observation[name]["state"] != "value" for name in (
                "error_type", "error_code",
            )):
                return None
            rejection = {"status_code": observation["status_code"],
                         **{name: observation[name]["value"]
                            for name in ("error_type", "error_code")}}
        if not _rejection_rule(rejection):
            return None
        rule = rejection
    else:
        observation = validate_terminal_rejection(rejection)
        if observation["operation"] != checked["operation"]:
            return None
        rule = {key: observation[key] for key in ("status_code", *REJECTION_FIELDS)}
    if rule not in checked["rules"]:
        return None
    return validate_completion({
        **dict.fromkeys(COUNTERS, 0), "source": source, "observed_at": observed_at,
        "rejection": {**rejection, "policy_evidence_sha256": checked["evidence_sha256"]},
    })


def validate_rejection_evidence(
    policy: dict[str, Any] | None, evidence: dict[str, Any], admitted_at: str | datetime,
) -> None:
    checked = validate_completion(evidence)
    rejected = checked.get("rejection")
    if not isinstance(rejected, dict):
        raise ValueError("Rejection settlement requires rejection evidence")
    expected = rejection_completion(
        policy, {key: item for key, item in rejected.items() if key != "policy_evidence_sha256"},
        source=checked["source"], observed_at=checked["observed_at"], admitted_at=admitted_at,
    )
    if expected != checked:
        raise ValueError("Rejection evidence does not match the immutable admission policy")


def normalized_identity(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "scope",
        "gateway_profile_id",
        "native_gateway_id",
        "correlation_id",
        "model_id",
        "admitted_at",
    }:
        raise ValueError("Execution identity fields do not match the protocol")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {"scope_type", "scope_id", "period_start"}:
        raise ValueError("Execution scope is malformed")
    if scope["scope_type"] not in {"person", "application"}:
        raise ValueError("Execution scope type is invalid")
    for text in (
        scope["scope_id"],
        value["native_gateway_id"],
        value["correlation_id"],
        value["model_id"],
    ):
        if not isinstance(text, str) or not text.strip() or len(text) > 255:
            raise ValueError("Execution identity is blank or too long")
    if scope["scope_type"] == "application" and str(UUID(scope["scope_id"])) != scope["scope_id"]:
        raise ValueError("Application scope must use its canonical UUID")
    admitted = utc_time(value["admitted_at"])
    month = date.fromisoformat(scope["period_start"])
    if month != admitted.date().replace(day=1):
        raise ValueError("Execution scope must retain the UTC admission month")
    return {
        **value,
        "gateway_profile_id": str(UUID(value["gateway_profile_id"])),
        "admitted_at": admitted.isoformat(),
    }


def validate_resolution(value: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "version", "id", "receipt", "decision", "actor", "reason",
            "evidence_sha256", "approved_at",
        }
        or type(value["version"]) is not int or value["version"] != 1
        or value["decision"] != "release_unverified"
        or not isinstance(value["id"], str)
        or not isinstance(value["actor"], str)
        or not 1 <= len(value["actor"]) <= 255 or value["actor"] != value["actor"].strip()
        or not value["actor"].isprintable()
        or value["reason"] not in (
            "gateway_validation_failure", "provider_result_unavailable", "evidence_irrecoverable",
        )
        or not isinstance(value["evidence_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["evidence_sha256"]) is None
        or not isinstance(value["receipt"], dict)
    ):
        raise ValueError("Reservation resolution requires exact persisted approval evidence")
    receipt = value["receipt"]
    mode = receipt.get("budget_mode", "tokens")
    receipt_fields = {"identity", "epoch", "reserved_tokens"}
    if mode == "usd":
        receipt_fields.update({"budget_mode", "reserved_usd", "price_snapshot"})
    if mode not in {"tokens", "usd"} or set(receipt) != receipt_fields:
        raise ValueError("Reservation resolution requires its original budget mode and evidence")
    identity = normalized_identity(receipt["identity"])
    approved = utc_time(value["approved_at"])
    if (
        exact_integer(receipt["reserved_tokens"]) == 0
        or approved < utc_time(identity["admitted_at"])
    ):
        raise ValueError("Reservation resolution must follow its original admission")
    normalized_receipt = {
        "identity": identity, "epoch": str(UUID(receipt["epoch"])),
        "reserved_tokens": receipt["reserved_tokens"],
    }
    if mode == "usd":
        projection = receipt["price_snapshot"]
        if not isinstance(projection, dict) or projection.get("model_id") != identity["model_id"]:
            raise ValueError("USD resolution requires its admitted model price")
        usd_completion_charge(projection, {
            "input_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0,
        })
        normalized_receipt.update({
            "budget_mode": "usd",
            "reserved_usd": format(exact_usd_amount(receipt["reserved_usd"]), ".14f"),
            "price_snapshot": projection,
        })
    return {
        **value, "id": str(UUID(value["id"])), "approved_at": approved.isoformat(),
        "receipt": normalized_receipt,
    }


def exact_usd_amount(value: Any) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValueError("USD accounting requires an exact decimal amount")
    try:
        amount = Decimal(value)
        with localcontext() as context:
            context.prec = 50
            valid = (
                amount.is_finite() and Decimal(0) <= amount < Decimal("1e16")
                and amount.quantize(Decimal("0.00000000000001")) == amount
            )
    except (InvalidOperation, ValueError) as error:
        raise ValueError("USD amount does not match NUMERIC(30,14)") from error
    if not valid:
        raise ValueError("USD amount does not match NUMERIC(30,14)")
    return amount


def usd_completion_charge(projection: dict[str, Any], evidence: dict[str, Any]) -> Decimal:
    price = projection.get("price")
    schedules = projection.get("rate_schedules")
    if (projection.get("version") != 1 or not isinstance(price, dict)
        or not isinstance(schedules, list) or not 1 <= len(schedules) <= 32):
        raise ValueError("USD completion requires its admitted price projection")
    digest = hashlib.sha256(json.dumps(
        {"price": price, "rate_schedules": schedules}, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if projection.get("price_sha256") != digest:
        raise ValueError("USD completion price projection digest differs")
    expected = []
    tiers = price.get("context_tiers") or [price]
    price_fields = (
        "input_price_per_million", "cached_price_per_million",
        "cache_write_price_per_million", "output_price_per_million",
    )
    rate_fields = (
        "input_rate_units", "cached_rate_units", "cache_write_rate_units", "output_rate_units",
    )
    for tier in tiers:
        rates = []
        for field in price_fields:
            rate = Decimal(str(tier[field])) * Decimal(100_000_000)
            if not rate.is_finite() or rate < 0 or rate != rate.to_integral_value():
                raise ValueError("USD unit prices must retain eight-decimal precision")
            rates.append(int(rate))
        expected.append({
            "min_input_tokens": tier.get("min_input_tokens", 0),
            "max_input_tokens": tier.get("max_input_tokens"),
            **dict(zip(rate_fields, rates, strict=True)),
        })
    if schedules != expected:
        raise ValueError("USD rate schedule differs from the admitted prices")
    billed_input = evidence["input_tokens"] + evidence["cached_tokens"]
    minimum = 0
    selected = None
    for index, tier in enumerate(schedules):
        if (set(tier) != {"min_input_tokens", "max_input_tokens", *rate_fields}
            or exact_integer(tier["min_input_tokens"]) != minimum):
            raise ValueError("USD price tiers must be complete and contiguous")
        maximum = tier["max_input_tokens"]
        if maximum is None:
            if index != len(schedules) - 1:
                raise ValueError("Only the final USD price tier may be unbounded")
        else:
            if exact_integer(maximum) < minimum or index == len(schedules) - 1:
                raise ValueError("USD price tiers must cover all input lengths")
            minimum = maximum + 1
        if billed_input >= tier["min_input_tokens"] and (
            maximum is None or billed_input <= maximum
        ):
            selected = tier
    if selected is None:
        raise ValueError("USD completion has no matching input price tier")
    with localcontext() as context:
        context.prec = 50
        amount = (
            Decimal(evidence["input_tokens"]) * exact_integer(selected["input_rate_units"])
            + Decimal(evidence["cached_tokens"] - evidence["cache_write_tokens"])
            * exact_integer(selected["cached_rate_units"])
            + Decimal(evidence["cache_write_tokens"])
            * exact_integer(selected["cache_write_rate_units"])
            + Decimal(evidence["output_tokens"]) * exact_integer(selected["output_rate_units"])
        ) / Decimal(100_000_000_000_000)
    return exact_usd_amount(amount)


def completion_transaction(
    summary: dict[str, Any],
    attempt: dict[str, Any],
    receipt: dict[str, Any],
    completion: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = validate_completion(completion)
    identity = normalized_identity(receipt["identity"])
    rejection = "rejection" in evidence
    if rejection:
        validate_rejection_evidence(
            attempt.get("rejection_policy"), evidence, identity["admitted_at"],
        )
    if (
        summary.get("protocol_version") != 2
        or type(summary.get("protocol_version")) is not int
        or summary.get("scope") != identity["scope"]
        or normalized_identity(attempt["identity"]) != identity
        or str(UUID(summary["epoch"])) != str(UUID(receipt["epoch"]))
        or str(UUID(attempt["epoch"])) != str(UUID(receipt["epoch"]))
        or exact_integer(attempt["reserved_tokens"]) != exact_integer(receipt["reserved_tokens"])
        or receipt["reserved_tokens"] == 0
    ):
        raise ValueError("Completion does not match its immutable admission")
    if utc_time(evidence["observed_at"]) < utc_time(identity["admitted_at"]):
        raise ValueError("Completion cannot precede admission")
    mode = summary.get("budget_mode", "tokens")
    if mode not in {"tokens", "usd"} or attempt.get("budget_mode", "tokens") != mode:
        raise ValueError("Completion budget mode differs from its admission")
    actual_usd = Decimal(0)
    previous_usd = Decimal(0)
    if mode == "usd":
        projection = attempt.get("price_snapshot")
        if not isinstance(projection, dict) or projection.get("model_id") != identity["model_id"]:
            raise ValueError("USD completion requires the admitted model price")
        reserved_usd = exact_usd_amount(attempt.get("reserved_usd"))
        if (receipt.get("budget_mode", "tokens") != "usd"
            or receipt.get("price_snapshot") != projection
            or exact_usd_amount(receipt.get("reserved_usd")) != reserved_usd):
            raise ValueError("USD completion receipt differs from its reservation")
        actual_usd = usd_completion_charge(projection, evidence)
        if attempt["state"] == "settled":
            previous_usd = exact_usd_amount(attempt.get("actual_usd"))
    for field in (
        "confirmed_tokens",
        "active_reserved_tokens",
        "upper_bound_tokens",
        "token_limit",
        "version",
        "policy_revision",
    ):
        exact_integer(summary[field])
    if type(summary["enforce"]) is not bool or type(summary["enabled"]) is not bool:
        raise ValueError("Execution policy booleans are malformed")
    state = attempt["state"]
    if state not in {
        "reserved", "dispatching", "uncertain", "upper_bound", "settled", "cancelled", "released",
    }:
        raise ValueError("Execution state is invalid")
    resolution = attempt.get("resolution_evidence")
    if resolution is not None:
        checked_resolution = validate_resolution(resolution)
        expected_receipt = {
            "identity": identity, "epoch": str(UUID(receipt["epoch"])),
            "reserved_tokens": receipt["reserved_tokens"],
        }
        if mode == "usd":
            expected_receipt.update({
                "budget_mode": "usd",
                "reserved_usd": format(exact_usd_amount(attempt["reserved_usd"]), ".14f"),
                "price_snapshot": attempt["price_snapshot"],
            })
        if checked_resolution["receipt"] != expected_receipt:
            raise ValueError("Reservation resolution belongs to another admission")
    if state == "released":
        if resolution is None or attempt.get("evidence_rank") != 2:
            raise ValueError("Released reservations require persisted approval evidence")
        if rejection:
            return summary, attempt
    previous = exact_integer(attempt["actual_tokens"]) if state == "settled" else 0
    if state != "settled" and attempt.get("actual_tokens") is not None:
        raise ValueError("Only settled attempts may have actual charges")
    if rejection and state == "settled":
        if attempt.get("evidence_rank", 3) == 3:
            return summary, attempt
        if previous != 0:
            raise ValueError("Rejection evidence cannot replace an existing nonzero charge")
        if attempt.get("rejection_evidence") is not None:
            validate_rejection_evidence(
                attempt.get("rejection_policy"), attempt["rejection_evidence"],
                identity["admitted_at"],
            )
            if attempt["rejection_evidence"]["rejection"] != evidence["rejection"]:
                raise ValueError("Conflicting rejection evidence requires review")
            return summary, attempt
    prior_evidence = attempt.get("completion_evidence")
    if prior_evidence is not None:
        retained = validate_completion(prior_evidence)
        if (
            state != "settled"
            or previous != retained["total_tokens"]
            or attempt.get("evidence_rank") != 3
        ):
            raise ValueError("Retained completion must match the actual settled charge")
        if any(retained[key] != evidence[key] for key in COUNTERS):
            raise ValueError("Conflicting complete producer evidence requires review")
        if mode == "usd" and previous_usd != actual_usd:
            raise ValueError("Conflicting complete USD producer evidence requires review")
        return summary, attempt
    if (
        state == "settled"
        and attempt.get("evidence_rank", 3) == 3
        and previous != evidence["total_tokens"]
    ):
        raise ValueError("Conflicting complete accounting evidence requires review")
    reserved = receipt["reserved_tokens"]
    active = reserved if state in {"reserved", "dispatching", "uncertain"} else 0
    upper = reserved if state == "upper_bound" else 0
    changed: dict[str, Any] = {"version": summary["version"] + 1}
    if mode == "tokens":
        changed.update({
            "confirmed_tokens": summary["confirmed_tokens"] + evidence["total_tokens"] - previous,
            "active_reserved_tokens": summary["active_reserved_tokens"] - active,
            "upper_bound_tokens": summary["upper_bound_tokens"] - upper,
        })
    for value in changed.values():
        exact_integer(value)
    if mode == "usd":
        held_usd = exact_usd_amount(attempt["reserved_usd"])
        with localcontext() as context:
            context.prec = 50
            usd_balances = {
                "confirmed_usd": exact_usd_amount(summary.get("confirmed_usd", "0"))
                + actual_usd - previous_usd,
                "active_reserved_usd": exact_usd_amount(summary.get("active_reserved_usd", "0"))
                - (held_usd if state in {"reserved", "dispatching", "uncertain"} else Decimal(0)),
                "upper_bound_usd": exact_usd_amount(summary.get("upper_bound_usd", "0"))
                - (held_usd if state == "upper_bound" else Decimal(0)),
            }
        changed.update({field: format(exact_usd_amount(amount), "f")
                        for field, amount in usd_balances.items()})
    updated_at = max(
        utc_time(now), utc_time(summary["updated_at"]), utc_time(attempt["updated_at"])
    ).isoformat()
    key = hashlib.sha256(
        json.dumps([evidence[name] for name in COUNTERS], separators=(",", ":")).encode()
    ).hexdigest()
    return (
        {**summary, **changed, "updated_at": updated_at},
        {
            **attempt,
            "state": "settled",
            "actual_tokens": evidence["total_tokens"],
            **({"actual_usd": format(actual_usd, "f")} if mode == "usd" else {}),
            "evidence_revision": max(exact_integer(attempt["evidence_revision"]), 1),
            "evidence_key": ("rejection:" if rejection else "completion:") + key,
            "evidence_rank": 2 if rejection else 3,
            ("rejection_evidence" if rejection else "completion_evidence"): evidence,
            "updated_at": updated_at,
        },
    )
