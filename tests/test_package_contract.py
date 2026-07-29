"""Distribution metadata contracts for the ShelfWise Python package."""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_wheel_includes_every_importable_source_package() -> None:
    """Keep editable/source-path development from masking incomplete release wheels."""
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = {
        Path(package).name
        for package in config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    }
    importable = {
        package.name
        for package in (_ROOT / "src").iterdir()
        if package.is_dir() and (package / "__init__.py").is_file()
    }

    assert included == importable


def test_requirements_include_every_runtime_project_dependency() -> None:
    """Keep container and CI installs aligned with the distributable package."""
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_dependencies = {
        _dependency_name(specification)
        for specification in config["project"]["dependencies"]
    }
    requirement_dependencies = {
        _dependency_name(line)
        for line in (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert project_dependencies <= requirement_dependencies


def _dependency_name(specification: str) -> str:
    """Return a normalized distribution name without extras or version constraints."""
    name = re.split(r"[\[<>=!~;\s]", specification.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()
