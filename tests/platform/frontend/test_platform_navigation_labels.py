from tests.support.paths import FRONTEND_SOURCE


def test_platform_menu_uses_new_labels_and_directory_precedes_configuration() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text()
    group = app.split('label: "平台管理"', 1)[1].split("const visibleNavGroups", 1)[0]
    assert group.index('label: "组织管理"') < group.index('label: "系统配置"')
    assert 'selectedDataSource === "apim" && directoryCapabilities.data?.available' in group
    assert 'page: "settings"' in group and 'page: "organization-management"' in group
    assert 'label: "系统管理"' not in app
    assert 'label: "设置"' not in group
    settings = (FRONTEND_SOURCE / "pages/settings-page.tsx").read_text()
    assert "<h1>系统配置</h1>" in settings
    assert 'aria-label="系统配置"' in settings
    for locale in ("en", "ja", "ko"):
        catalog = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text()
        assert '"平台管理":' in catalog
        assert '"系统配置":' in catalog
