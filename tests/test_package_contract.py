"""Distribution metadata contracts for the ShelfWise Python package."""

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
