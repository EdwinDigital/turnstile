from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_application_creation_rules() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--test",
            str(Path(__file__).with_name("application-create-form.test.mjs")),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_creation_and_model_editor_short_labels_have_exact_translations() -> None:
    for language in ("en", "ja", "ko"):
        catalog = (FRONTEND_SOURCE / f"locales/{language}/phrases-core.ts").read_text(
            encoding="utf-8"
        )
        for phrase in ("输出单价", "订阅 ID", "重新检查"):
            assert f'"{phrase}":' in catalog


def test_creation_credentials_never_enter_dom_or_query_cache() -> None:
    dialog = (FRONTEND_SOURCE / "components/applications/application-create-dialog.tsx").read_text(
        encoding="utf-8"
    )
    page = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text(encoding="utf-8")
    assert "const primaryKey = useRef<string | null>(null)" in dialog
    assert "primaryKey.current = response.primary_key" in dialog
    assert (
        "const safeAccepted = { operation: response.operation, status_url: response.status_url }"
        in dialog
    )
    assert "useMutation" not in dialog
    assert "setQueryData" not in dialog
    assert "{primaryKey.current}" not in dialog
    assert "{response.primary_key}" not in dialog
    assert "primary_key" not in page
    assert 'window.addEventListener("beforeunload", protectUnsavedKey)' in dialog
    assert "if (failed) primaryKey.current = null" in dialog
    assert "applicationOperationIdFromUrl(window.location.href)" in page
    assert 'createOpen && user?.role === "owner"' in page
    assert "inventory?.provisioning_defaults?.monthly_token_limit" in dialog
    assert "inventory?.provisioning_defaults?.tokens_per_minute" in dialog
