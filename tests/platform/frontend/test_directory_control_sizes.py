from tests.support.paths import FRONTEND_SOURCE


def test_directory_controls_use_explicit_size_metadata_and_scoped_menu_styles() -> None:
    button = (FRONTEND_SOURCE / "components/ui/button.tsx").read_text()
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    styles = (FRONTEND_SOURCE / "styles/organization-management.css").read_text()
    assert "data-size={size}" in button and "data-variant={variant}" in button
    assert 'className="directory-person-menu"' in directory
    assert '[data-size="sm"]' in styles
    assert '[data-size^="icon"]' in styles
    assert '.directory-person-menu [data-slot="dropdown-menu-item"] > svg' in styles
    assert "width: 14px; height: 14px; flex: 0 0 14px" in styles


def test_directory_header_and_permission_tags_keep_compact_readable_dimensions() -> None:
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    styles = (FRONTEND_SOURCE / "styles/organization-management.css").read_text()
    assert '<FolderTree size={17} aria-hidden="true" />' in directory
    assert 'className="directory-table directory-people-table"' in directory
    assert "minWidths={peopleColumnMinWidths}" in directory
    assert "const peopleColumnMinWidths = [160, 160, 200, 160, 80, 100, 60] as const" in directory
    tags = styles.split('.directory-permission-tags [data-slot="badge"]', 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in tags and "flex: 0 0 auto" in tags
    assert "overflow-wrap: normal" in tags and "max-width: none" in tags
    assert (
        ".directory-people-table th:nth-child(3) { width: 200px; white-space: nowrap; }"
        in styles
    )
