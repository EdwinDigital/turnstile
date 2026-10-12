import subprocess

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_client_download_navigation_and_private_transfer() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text()
    assert 'if (page === "client-downloads") return page;' in app
    assert 'navigatePage("client-downloads")' in app
    assert "<ClientDownloadsPage" in app
    api = (FRONTEND_SOURCE / "api/client-downloads.ts").read_text()
    assert 'method: "POST", credentials: "include", cache: "no-store"' in api
    assert "SESSION_EXPIRED_EVENT" in api
    page = (FRONTEND_SOURCE / "pages/client-downloads-page.tsx").read_text()
    assert "URL.revokeObjectURL" in page
    assert "localStorage" not in page
    assert 'aria-describedby="client-secret-warning"' in page
    assert 'className="finops-scroll-region"' in page


def test_plugin_hooks_and_dispatch() -> None:
    subprocess.run(
        ["node", "--test", str(REPOSITORY_ROOT / "tests/platform/frontend/client-plugin.test.mjs")],
        check=True,
    )


def test_browser_authorization_request_transport() -> None:
    subprocess.run(
        [
            "node", "--test",
            str(REPOSITORY_ROOT / "tests/platform/frontend/client-authorization.test.mjs"),
        ],
        check=True,
    )


def test_browser_download_names() -> None:
    subprocess.run(
        [
            "node", "--test",
            str(REPOSITORY_ROOT / "tests/platform/frontend/client-downloads.test.mjs"),
        ],
        check=True,
    )
