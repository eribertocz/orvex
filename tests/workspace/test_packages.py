"""Workspace invariants: every module is an independent distribution in the `sextant` namespace."""

import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPOSITORY_ROOT / "packages"
MODULES = ("store", "transform", "semantic", "access", "dashboards", "mcp", "server")


def distribution_name(module: str) -> str:
    return "sextant" if module == "server" else f"sextant-{module}"


def test_every_module_has_a_package() -> None:
    assert sorted(path.name for path in PACKAGES.iterdir() if path.is_dir()) == sorted(MODULES)


@pytest.mark.parametrize("module", MODULES)
def test_package_metadata(module: str) -> None:
    project = tomllib.loads((PACKAGES / module / "pyproject.toml").read_text())["project"]
    assert project["name"] == distribution_name(module)
    assert project["license"] == "Apache-2.0"
    assert project["requires-python"] == ">=3.14"


@pytest.mark.parametrize("module", MODULES)
def test_namespace_has_no_init(module: str) -> None:
    source = PACKAGES / module / "src"
    assert not (source / "sextant" / "__init__.py").exists()
    assert (source / "sextant" / module / "__init__.py").is_file()
    assert (source / "sextant" / module / "py.typed").is_file()


@pytest.mark.parametrize("module", MODULES)
def test_package_installs_and_imports_alone(module: str, tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheels"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--quiet",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_directory),
            str(PACKAGES / module),
        ],
        check=True,
    )
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    wheels = [str(path) for path in wheel_directory.glob("*.whl")]
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--no-index", "--no-deps", *wheels],
        check=True,
    )

    imported = subprocess.run(
        [str(python), "-c", f"import sextant.{module} as m; print(m.__version__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    for other in MODULES:
        if other == module:
            continue
        missing = subprocess.run(
            [str(python), "-c", f"import sextant.{other}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing.returncode != 0, f"sextant.{other} importable next to sextant.{module}"
