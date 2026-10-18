import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_password_login_reports_authentication_and_service_failures_separately() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for login error tests"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("login-errors.test.mjs"))],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_frontend_expires_the_application_session_authoritatively() -> None:
    auth_api = (FRONTEND_SOURCE / "api/auth.ts").read_text(encoding="utf-8")
    client = (FRONTEND_SOURCE / "api/client.ts").read_text(encoding="utf-8")
    provider = (FRONTEND_SOURCE / "providers/auth-provider.tsx").read_text(
        encoding="utf-8"
    )

    assert "session_expires_at?: string" in auth_api
    assert "session_idle_timeout_seconds" not in auth_api
    assert "session_idle_expires_at" not in auth_api
    assert 'SESSION_EXPIRED_EVENT = "turnstile:session-expired"' in client
    assert "SESSION_ACTIVITY_EVENT" not in client
    assert 'fetch(apiUrl("/api/v1/auth/me")' in client
    assert "return profile.status === 401" in client
    assert "if (response.status === 401 && await applicationSessionExpired())" in client
    assert "Date.parse(user.session_expires_at) - Date.now()" in provider
    assert "Math.min(remaining, 2_147_483_647)" in provider
    assert "window.addEventListener(SESSION_EXPIRED_EVENT, expireLocalSession)" in provider
    assert "SESSION_ACTIVITY_EVENT" not in provider
    assert "x-session-idle-expires-at" not in client
    assert "sensitive_reauthentication" not in client
    assert "sensitive_reauthentication" not in provider
    assert "queryClient.clear()" in provider
    assert 'setStatus("anonymous")' in provider
