from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_SCIENCE_ROOT = REPO_ROOT / "src" / "shelfwise_decision_science"

# The Dependency Rule this project's own architecture claims (CLAUDE.md, CODING_STANDARDS.md):
# decision-science math must be tool-backed and testable, never invented inline in an LLM
# prompt. That only holds if the layer that computes forecasts/expiry-risk/cold-chain-risk
# cannot itself reach an LLM client, the orchestration layer that calls it, or any connector -
# otherwise the boundary is a naming convention a rushed PR can silently cross. This is the
# mechanical check: it inspects real `import`/`from ... import` statements via `ast`, not
# string matching, so it cannot be fooled by a comment or a docstring mentioning the name.
ALLOWED_FIRST_PARTY_PACKAGES = {"shelfwise_contracts", "shelfwise_runtime"}


def _first_party_package(module_name: str) -> str | None:
    """Return the top-level shelfwise_* package a dotted module name belongs to, if any."""
    root = module_name.split(".", 1)[0]
    return root if root.startswith("shelfwise_") else None


def _imported_modules(source_path: Path) -> set[str]:
    """Collect every absolute module named in an `import`/`from import` statement."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _decision_science_source_files() -> list[Path]:
    return sorted(DECISION_SCIENCE_ROOT.rglob("*.py"))


def test_decision_science_package_exists_and_is_non_empty() -> None:
    """Guard the guard: if this ever collects zero files, every check below is a false pass."""
    files = _decision_science_source_files()
    assert len(files) >= 5, (
        "expected the decision-science layer to contain real modules; found "
        f"{len(files)} - either the layer was gutted or DECISION_SCIENCE_ROOT is wrong"
    )


@pytest.mark.parametrize("source_path", _decision_science_source_files(), ids=lambda p: p.name)
def test_decision_science_module_only_imports_its_declared_dependencies(source_path: Path) -> None:
    """shelfwise_decision_science may depend on shelfwise_contracts/runtime and stdlib only.

    A cycle or a reach-out from here into shelfwise_backend, shelfwise_connectors, or
    shelfwise_inference (the LLM client) would let orchestration or an LLM call leak into
    the one layer this project's rules say must stay pure, tool-backed math - Clean
    Architecture's Dependency Rule (dependencies point inward, never toward orchestration)
    and SICP's abstraction-barrier argument (callers may use only the declared operations)
    both name this as the specific failure this test exists to catch.
    """
    forbidden: set[str] = set()
    for module_name in _imported_modules(source_path):
        package = _first_party_package(module_name)
        if package is None:
            continue  # stdlib or third-party; not a first-party boundary concern here.
        if package == "shelfwise_decision_science":
            continue  # intra-package import.
        if package not in ALLOWED_FIRST_PARTY_PACKAGES:
            forbidden.add(module_name)

    assert not forbidden, (
        f"{source_path.relative_to(REPO_ROOT)} imports {sorted(forbidden)}, which "
        f"is outside the decision-science layer's declared dependency set "
        f"{sorted(ALLOWED_FIRST_PARTY_PACKAGES)} - math must stay tool-backed and callable "
        "by orchestration, never the other way around"
    )


def test_no_decision_science_module_imports_an_llm_client_library() -> None:
    """Even a third-party name (not just shelfwise_inference) would violate the barrier.

    A future PR could route around the first-party check above by calling an LLM SDK
    directly (e.g. `openai`, `anthropic`, `httpx` against an inference endpoint) instead
    of importing shelfwise_inference. Flag any known LLM-client import by name so that
    kind of shortcut fails loudly instead of silently making a "pure" math function
    network-dependent and prompt-shaped.
    """
    llm_client_modules = {"openai", "anthropic", "fireworks", "vllm"}
    violations: dict[str, set[str]] = {}
    for source_path in _decision_science_source_files():
        hit = _imported_modules(source_path) & llm_client_modules
        if hit:
            violations[str(source_path.relative_to(REPO_ROOT))] = hit

    assert not violations, (
        f"decision-science modules importing LLM client libraries directly: {violations} - "
        "math must never become network- or prompt-dependent"
    )
