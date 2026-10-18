"""Guards the layering the Python runtime packages are organised into.

The rule is only real if something fails when it is broken. A dependency that points the
wrong way does not break a test or a type check -- it just quietly makes the next move
harder -- so it is asserted here instead of living in a comment nobody re-reads.

`turnstile_core` owns reusable runtime code and may never import the FastAPI `backend`
package. The two Function composition roots may import the core package but not the
backend package.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.support.paths import BACKEND_ROOT, CORE_ROOT, FRONTEND_SOURCE, REPOSITORY_ROOT

FUNCTIONS_ROOT = REPOSITORY_ROOT / "functions"

# A layer may import from itself and from anything listed here, nothing else.
CORE_ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "persistence": {"domain"},
    "integrations": {"domain", "persistence"},
    "ingestion": {"domain", "persistence", "integrations"},
    # Reading a vendor's published price list is the same shape as reading a vendor's API, so
    # pricing sits beside integrations rather than inside services: it talks outward and owns no
    # registry state of its own.
    "pricing": {"domain"},
    "services": {"domain", "persistence", "integrations", "pricing"},
}

BACKEND_ALLOWED: dict[str, set[str]] = {
    "services": set(),
    "http": {"services"},
    "data_sources": {"services", "http"},
}

CORE_ROOT_MODULES = {"config", "security"}

# Entry points, which compose everything and are therefore exempt. `api` and `migrate` are
# additionally pinned by the App Service start command, so they cannot move without a cloud
# change; `accounts` is run by an operator over a shell and is free to move, but it is an
# entry point by the same definition -- it wires config to persistence and is invoked with
# `python -m` rather than imported by anything.
ENTRY_POINTS = {"api", "bootstrap", "bootstrap_storage_route", "migrate", "accounts"}
DEPLOYMENT_ENTRY_POINTS = {"api", "bootstrap", "migrate"}

def _imports_package(source: str, package: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and (node.module == package or node.module.startswith(f"{package}."))
        ):
            return True
        if isinstance(node, ast.Import) and any(
                alias.name == package or alias.name.startswith(f"{package}.")
                for alias in node.names
        ):
            return True
    return False


def _imported_layers(source: str, own_layer: str, layers: set[str]) -> set[str]:
    """The layers a module reaches into, resolved from its relative imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        # level 1 is a sibling inside the same layer; level 2 climbs to the package root.
        if node.level == 1:
            continue
        if node.level >= 2 and node.module:
            head = node.module.split(".")[0]
            if head in layers and head != own_layer:
                found.add(head)
    return found


def _layer_violations(package: Path, allowed_layers: dict[str, set[str]]) -> list[str]:
    violations: list[str] = []
    layers = set(allowed_layers)
    for layer, allowed in allowed_layers.items():
        for path in sorted((package / layer).rglob("*.py")):
            reached = _imported_layers(path.read_text(encoding="utf-8"), layer, layers)
            for target in sorted(reached - allowed):
                violations.append(f"{layer}/{path.name} imports {target}")
    return violations


def test_core_layers_allow_only_current_source_credential_bridge() -> None:
    violations = _layer_violations(CORE_ROOT, CORE_ALLOWED)
    assert violations == ["persistence/repository_support.py imports services"], (
        "Unexpected Source layer dependencies: " + "; ".join(violations)
    )
    support = ast.parse(
        (CORE_ROOT / "persistence/repository_support.py").read_text(encoding="utf-8")
    )
    service_imports = [
        (node.module, tuple(alias.name for alias in node.names))
        for node in ast.walk(support)
        if isinstance(node, ast.ImportFrom)
        and node.level >= 2
        and node.module is not None
        and node.module.split(".")[0] == "services"
    ]
    assert service_imports == [
        ("services.route_publication", ("route_credential_source",)),
    ]


def test_no_backend_layer_imports_a_layer_above_it() -> None:
    violations = _layer_violations(BACKEND_ROOT, BACKEND_ALLOWED)
    assert not violations, "Backend layering violated: " + "; ".join(violations)


def test_core_never_imports_backend() -> None:
    assert CORE_ROOT.is_dir(), "Shared runtime code must live in turnstile_core"
    violations = [
        str(path.relative_to(CORE_ROOT))
        for path in sorted(CORE_ROOT.rglob("*.py"))
        if _imports_package(path.read_text(encoding="utf-8"), "backend")
    ]
    assert not violations, f"Shared core imports the HTTP backend: {violations}"


def test_functions_never_import_backend() -> None:
    violations = [
        str(path.relative_to(FUNCTIONS_ROOT))
        for path in sorted(FUNCTIONS_ROOT.rglob("*.py"))
        if _imports_package(path.read_text(encoding="utf-8"), "backend")
    ]
    assert not violations, f"Function runtime imports the HTTP backend: {violations}"


def test_every_core_module_lives_in_a_layer_or_is_cross_cutting() -> None:
    loose = {
        path.stem
        for path in CORE_ROOT.glob("*.py")
        if path.stem != "__init__"
    }
    assert loose == CORE_ROOT_MODULES, (
        "Core modules at the package root must be a declared cross-cutting "
        f"concern; found {sorted(loose)}"
    )


def test_every_core_package_is_a_declared_layer() -> None:
    packages = {
        path.name
        for path in CORE_ROOT.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    }
    assert packages == set(CORE_ALLOWED), (
        "Core packages must be declared architecture layers; "
        f"expected {sorted(CORE_ALLOWED)}, found {sorted(packages)}"
    )


def test_every_backend_module_is_an_entry_point() -> None:
    loose = {
        path.stem
        for path in BACKEND_ROOT.glob("*.py")
        if path.stem != "__init__"
    }
    assert loose == ENTRY_POINTS


def test_every_backend_package_is_a_declared_layer() -> None:
    packages = {
        path.name
        for path in BACKEND_ROOT.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    }
    assert packages == set(BACKEND_ALLOWED), (
        "Backend packages must be declared architecture layers; "
        f"expected {sorted(BACKEND_ALLOWED)}, found {sorted(packages)}"
    )


def test_python_runtimes_never_import_repository_tests() -> None:
    violations: list[str] = []
    for package in (BACKEND_ROOT, CORE_ROOT):
        for path in sorted(package.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if re.search(
                r"(?m)^\s*(?:from\s+tests(?:\.|\s)|import\s+tests(?:\.|\s|$))",
                source,
            ):
                violations.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert not violations, f"Runtime modules import repository tests: {violations}"


def test_the_deployment_entry_points_keep_their_import_paths() -> None:
    """`appCommandLine` runs these modules by dotted path.

    Moving either one into a layer would need an App Service configuration change, so the
    coupling is asserted here rather than discovered when a deployment fails to start.
    """
    for name in DEPLOYMENT_ENTRY_POINTS:
        assert (BACKEND_ROOT / f"{name}.py").is_file(), (
            f"backend.{name} is referenced by the App Service start "
            "command and must stay at the package root"
        )


def test_the_layers_document_their_own_rule() -> None:
    """Each layer's `__init__` says what it may depend on, so the rule is readable in place."""
    for package, layers in ((BACKEND_ROOT, BACKEND_ALLOWED), (CORE_ROOT, CORE_ALLOWED)):
        for layer in layers:
            text = (package / layer / "__init__.py").read_text(encoding="utf-8")
            assert re.search(r'^\s*"""', text), f"{layer}/__init__.py needs a docstring"


def test_frontend_never_asserts_an_application_identity_header() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND_SOURCE.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    )

    assert "X-Hive-Role" not in source
    assert "X-Hive-User" not in source
    assert "system-finops-assistant" not in source


def test_frontend_backend_requests_stay_in_api_layer() -> None:
    violations: list[str] = []
    for path in sorted(FRONTEND_SOURCE.rglob("*")):
        if path.suffix not in {".ts", ".tsx"} or "api" in path.relative_to(FRONTEND_SOURCE).parts:
            continue
        source = path.read_text(encoding="utf-8")
        if 'fetch("/api' in source or "fetch('/api" in source or "apiUrl(" in source:
            violations.append(str(path.relative_to(FRONTEND_SOURCE)))
    assert not violations, f"Backend requests must live under frontend/src/api: {violations}"


def test_frontend_api_layer_does_not_import_react_providers() -> None:
    violations: list[str] = []
    for path in sorted((FRONTEND_SOURCE / "api").rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        if "/providers/" in source or "../providers/" in source:
            violations.append(str(path.relative_to(FRONTEND_SOURCE)))
    assert not violations, f"Frontend API layer imports providers: {violations}"
