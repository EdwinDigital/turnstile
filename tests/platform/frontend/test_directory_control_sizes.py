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
