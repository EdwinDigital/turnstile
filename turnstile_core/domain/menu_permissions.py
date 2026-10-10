"""Application navigation entitlements, never gateway or data-scope authorization."""

from collections.abc import Iterable
from typing import Literal

MenuPermissionGroup = Literal["user", "organization_admin", "department_admin", "team_admin"]

PERSONAL_MENUS = ("user-settings", "finops-invoke")
ANALYSIS_MENUS = (
    "finops-overview",
    "finops-analytics",
    "finops-trends",
    "finops-requests",
    "assistant",
    "pinned-report",
    "copilot-requests",
)
MANAGEMENT_MENUS = (
    "finops-governance",
    "budgets",
    "models",
    "organization-management",
    "copilot-cost-centers",
    "copilot-unassigned-users",
)
ALL_MENUS = (
    *PERSONAL_MENUS,
    *ANALYSIS_MENUS,
    *MANAGEMENT_MENUS,
    "settings",
    "apim-native-routes",
    "gateway-releases",
    "applications",
    "copilot-enterprise-teams",
)
MENU_GROUPS: dict[MenuPermissionGroup, tuple[str, ...]] = {
    "user": PERSONAL_MENUS,
    "team_admin": (*PERSONAL_MENUS, *ANALYSIS_MENUS, "models", "organization-management"),
    "department_admin": (*PERSONAL_MENUS, *ANALYSIS_MENUS, *MANAGEMENT_MENUS),
    "organization_admin": ALL_MENUS,
}


def menu_permissions(
    groups: Iterable[MenuPermissionGroup] = ("user",), *, owner: bool = False
) -> tuple[str, ...]:
    if owner:
        return ALL_MENUS
    allowed = {menu for group in groups for menu in MENU_GROUPS[group]}
    return tuple(menu for menu in ALL_MENUS if menu in allowed)
