from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT, read_frontend_styles


def test_pool_affinity_legacy_payload_unit_cases() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("pool-session-affinity.test.mjs"))],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pool_affinity_editor_is_capability_and_owner_gated() -> None:
    editor = (
        FRONTEND_SOURCE / "components/model-management/deployment-resilience-editor.tsx"
    ).read_text(encoding="utf-8")
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    assert "registry.data?.backend_pool_session_affinity_supported === true" in editor
    assert "poolPublicationPayload(draft, affinitySupported)" in editor
    assert "session_affinity: pool.session_affinity === true" in editor
    assert "session_affinity: false" in editor
    assert "disabled={!canManage || busy || !affinitySupported || !validMemberCount}" in editor
    assert 'const canManage = user?.role === "owner"' in editor
    assert (
        "const canConfigure = canManage && "
        "(hasEffectivePool || compatibleRuntimes.length >= 2)" in editor
    )
    assert "disabled={busy || !validDraft}" in editor
    assert "registry.backend_pool_session_affinity_supported === true" in api
    assert ".router-resilience-affinity { min-width: 0;" in read_frontend_styles()