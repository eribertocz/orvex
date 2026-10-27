"""The lock files agree with the exact pins declared in the root pyproject.toml."""

import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOCK_FILES = ("pylock.toml", "pylock.py314.toml")


def declared_pins() -> dict[str, str]:
    groups = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())["dependency-groups"]
    pins: dict[str, str] = {}
    for requirement in groups["dev"] + groups["runtime"]:
        name, separator, version = requirement.partition("==")
        assert separator, f"dependency '{requirement}' is not pinned exactly with =="
        pins[name.lower()] = version
    return pins


@pytest.mark.parametrize("lock_file", LOCK_FILES)
def test_lock_file_matches_declared_pins(lock_file: str) -> None:
    lock = tomllib.loads((REPOSITORY_ROOT / lock_file).read_text())
    locked = {package["name"].lower(): package["version"] for package in lock["packages"]}
    for name, version in declared_pins().items():
        assert locked.get(name) == version, (
            f"{lock_file} has {name}=={locked.get(name)} but pyproject.toml pins {version}; "
            "regenerate the lock files (CONTRIBUTING.md)"
        )


@pytest.mark.parametrize("lock_file", LOCK_FILES)
def test_every_locked_wheel_has_a_hash(lock_file: str) -> None:
    lock = tomllib.loads((REPOSITORY_ROOT / lock_file).read_text())
    for package in lock["packages"]:
        for artifact in package.get("wheels", []) + (
            [package["sdist"]] if "sdist" in package else []
        ):
            assert artifact["hashes"].get("sha256"), (
                f"{package['name']} has no sha256 in {lock_file}"
            )


def test_package_dependencies_are_pinned_in_the_runtime_group() -> None:
    groups = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())["dependency-groups"]
    runtime = set(groups["runtime"])
    for project in sorted((REPOSITORY_ROOT / "packages").glob("*/pyproject.toml")):
        for requirement in tomllib.loads(project.read_text())["project"]["dependencies"]:
            assert requirement in runtime, (
                f"{project.parent.name}: {requirement} not in runtime group"
            )
