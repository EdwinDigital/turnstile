from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from backend.services.client_user_policy import CONTEXT_KEY_NAME, compose_client_user_policy
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
    assert 'token.Claims.GetValueOrDefault("subscription"' in check
    assert 'token.Claims.GetValueOrDefault("model"' in check
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
