import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_image_frontend_unit_cases() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--test",
            str(Path(__file__).with_name("image-generation.test.mjs")),
            str(Path(__file__).with_name("model-publication-dialog.test.mjs")),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_image_invocation_keeps_results_in_memory_and_excludes_text_models() -> None:
    page = (FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-invocation.tsx").read_text()
    result = (FRONTEND_SOURCE / "data-sources/apim/pages/image-generation-result.tsx").read_text()
    assert 'capabilities.includes("image_generation") === imageMode' in page
    assert "n: 1, stream: false" in page
    assert "image_configuration_schema_version === 4" in page
    assert "setImageResult(null)" in page and 'setPrompt("")' in page
    assert "URL.createObjectURL" in result and "URL.revokeObjectURL" in result
    assert "naturalWidth" in result and "naturalHeight" in result
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "setQueryData",
        "useMutation",
        "console.log",
    ):
        assert forbidden not in page + result


def test_image_editor_requires_prices_and_preserves_operation() -> None:
    editor = (FRONTEND_SOURCE / "components/model-management/model-edit-dialog.tsx").read_text()
    publication = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text()
    assert "[draft.inputPrice, draft.cacheReadPrice, draft.outputPrice]" in editor
    assert "if (imagePriceMissing) return" in editor
    assert "disabled={busy || imageGeneration}" in editor
    assert 'operation: imageGeneration ? "image_generation" : undefined' in publication
    assert "[inputPrice, cacheReadPrice, outputPrice]" in publication
    assert "context_window: imageGeneration ? null" in publication
    assert (
        "cache_write_cost_per_million: imageGeneration || pricing.contextPricingPreview ? null"
        in publication
    )
