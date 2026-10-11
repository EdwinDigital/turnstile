"""Narrow, idempotent upgrade of subscription TPM from the application ledger."""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from .apim_control_plane_contract import PolicyCompilationError

TPM_EXPRESSION = '@((int)context.Variables["effectiveApplicationTpm"])'


def compose_application_tpm_policy(source: str) -> str:
    # Azure's primary table endpoint ends in '/'; older bootstrap policies appended another.
    # Normalize only the known ledger origin, preserving policy sections and customer targets.
    mapping = re.search(r'<set-variable name="applicationMapUrl".*?/>', source, re.DOTALL)
    if mapping is None:
        raise PolicyCompilationError("Application TPM upgrade has no ledger mapping URL")
    endpoint = re.search(r'https://[^/\s&"<>]+\.table\.[^/\s&"<>]+', mapping.group())
    if endpoint:
        source = source.replace(endpoint.group() + "//", endpoint.group() + "/")
    if 'name="effectiveApplicationTpm"' in source:
        return source
    text = re.compile(r"<llm-token-limit\b[^>]*finops:subscription:[^>]*?/>", re.DOTALL)
    matches = list(text.finditer(source))
    if len(matches) != 1:
        raise PolicyCompilationError(
            "Application TPM upgrade requires one subscription token limit"
        )
    old = matches[0].group()
    rate = re.search(r'tokens-per-minute="([^"]+)"', old)
    if rate is None or not (rate[1].isdigit() or rate[1] == "__TOKENS_PER_MINUTE__"):
        raise PolicyCompilationError("Application TPM fallback must be a reviewed integer")
    fallback = rate[1]
    expression = escape(TPM_EXPRESSION, {'"': "&quot;"})
    updated = old.replace(rate.group(), f'tokens-per-minute="{expression}"')
    source = source[: matches[0].start()] + updated + source[matches[0].end() :]
    images = re.compile(r"<rate-limit-by-key\b[^>]*finops:images:subscription:[^>]*?/>", re.DOTALL)
    matches = list(images.finditer(source))
    if len(matches) != 1:
        raise PolicyCompilationError(
            "Application TPM upgrade requires one image subscription limit"
        )
    old = matches[0].group()
    rate = re.search(r'calls="([^"]+)"', old)
    if rate is None or rate[1] != fallback:
        raise PolicyCompilationError("Text and image subscription limits have different fallbacks")
    source = (
        source[: matches[0].start()]
        + old.replace(
            rate.group(),
            f'calls="{expression}"',
        )
        + source[matches[0].end() :]
    )
    anchor = re.search(r'<set-variable name="consumerId"[^>]*?/>', source)
    if anchor is None:
        raise PolicyCompilationError("Application TPM upgrade has no subscription boundary")
    value = """@{
          var fallback = (long)FALLBACK;
          var state = context.Variables.GetValueOrDefault<JObject>("applicationLedgerState", null);
          var admission = context.Variables.GetValueOrDefault<string>("applicationAdmission", "");
          var configured = state != null && ((bool?)state["available"] ?? false);
          var requested = configured ? ((long?)state["tokensPerMinute"] ?? fallback) : fallback;
          return (int)(admission == "ok" && configured && requested > 0 && requested <= int.MaxValue
            ? requested : fallback);
        }""".replace("FALLBACK", fallback)
    variable = (
        '\n        <set-variable name="effectiveApplicationTpm" value="'
        + escape(
            value,
            {'"': "&quot;"},
        )
        + '" />'
    )
    return source[: anchor.end()] + variable + source[anchor.end() :]
