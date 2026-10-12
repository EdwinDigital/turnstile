from __future__ import annotations

from uuid import uuid4
from xml.etree import ElementTree as ET

import pytest

from backend.services.client_user_policy import (
    CONTEXT_KEY_NAME,
    compose_client_user_policy,
    upgrade_client_user_policy,
)
from tests.support.paths import REPOSITORY_ROOT


def test_trusted_client_preserves_employee_and_subscription_branches() -> None:
    original = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    before = ET.fromstring(original)
    after = ET.fromstring(compose_client_user_policy(original))
    prior = next(
        x
        for x in before.findall("./inbound/choose")
        if x.find("./when/validate-azure-ad-token") is not None
    )
    current = next(
        x
        for x in after.findall("./inbound/choose")
        if x.find("./when/validate-azure-ad-token") is not None
    )
    assert len(current.findall("when")) == 2
    old_employee = prior.find("when")
    old_subscription, new_subscription = prior.find("otherwise"), current.find("otherwise")
    assert old_employee is not None
    assert old_subscription is not None and new_subscription is not None
    assert ET.tostring(old_employee) == ET.tostring(current.findall("when")[1])
    assert ET.tostring(old_subscription) == ET.tostring(new_subscription)
    trusted = current.findall("when")[0]
    validator = trusted.find("validate-jwt")
    assert validator is not None
    assert validator.get("require-signed-tokens") == "true"
    assert validator.findtext("issuer-signing-keys/key") == "{{" + CONTEXT_KEY_NAME + "}}"
    predicate = trusted.find("./choose/when")
    assert predicate is not None
    check = predicate.get("condition", "")
    assert "context.Subscription != null" in check
    assert "context.Subscription == null" not in check
    assert 'token.Claims.GetValueOrDefault("subscription"' in check
    assert 'token.Claims.GetValueOrDefault("application_id"' in check
    assert 'token.Claims.GetValueOrDefault("model"' in check
    assert 'token.Claims.GetValueOrDefault("jti"' in check
    assert validator.findtext("required-claims/claim[@name='scope']/value") == "personal"
    assert validator.findtext("required-claims/claim[@name='scp']/value") == "Model.Invoke"
    assert trusted.find(".//set-variable[@name='ledgerUrl']") is not None
    assert trusted.find(".//set-variable[@name='applicationLedgerUrl']") is None
    common_application = after.find(".//set-variable[@name='applicationLedgerUrl']")
    assert common_application is not None
    assert (
        len(after.findall(".//send-request[@response-variable-name='applicationLedgerWrite']")) == 1
    )
    removed = after.find("./inbound/set-header[@name='x-turnstile-client-context']")
    assert removed is not None and removed.get("exists-action") == "delete"
    with pytest.raises(ValueError, match="already installed"):
        compose_client_user_policy(ET.tostring(after, encoding="unicode"))


def test_personal_upgrade_replaces_only_the_versioned_client_branch() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    gateway = "10000000-0000-4000-8000-000000000001"
    v1 = ET.fromstring(compose_client_user_policy(source))
    marker = v1.find("./inbound/set-variable[@name='clientContextPolicyVersion']")
    assert marker is not None
    marker.set("value", "@(1)")
    before = ET.tostring(v1, encoding="unicode")
    upgraded = upgrade_client_user_policy(before, gateway_id=gateway)
    after = ET.fromstring(upgraded)
    assert after.findtext(".//claim[@name='gateway_id']/value") == gateway
    new_marker = after.find(".//set-variable[@name='clientContextPolicyVersion']")
    assert new_marker is not None and new_marker.get("value") == "@(2)"
    assert upgrade_client_user_policy(upgraded, gateway_id=gateway) == upgraded

    def without_client(root: ET.Element) -> bytes:
        inbound = root.find("inbound")
        assert inbound is not None
        for parent in inbound.findall("choose"):
            for child in list(parent):
                if 'ContainsKey("x-turnstile-client-context")' in child.get("condition", ""):
                    parent.remove(child)
        for child in list(inbound):
            if child.get("name") in {"clientContextPolicyVersion", "x-turnstile-client-context"}:
                inbound.remove(child)
        return ET.tostring(root)

    assert without_client(v1) == without_client(after)


def test_personal_upgrade_rejects_ambiguous_policy_or_invalid_gateway() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    with pytest.raises(ValueError):
        upgrade_client_user_policy(source, gateway_id='invalid"gateway')
    root = ET.fromstring(compose_client_user_policy(source))
    marker = root.find(".//set-variable[@name='clientContextPolicyVersion']")
    assert marker is not None
    marker.set("value", "@(99)")
    with pytest.raises(ValueError, match="Unsupported"):
        upgrade_client_user_policy(ET.tostring(root, encoding="unicode"), gateway_id=str(uuid4()))
