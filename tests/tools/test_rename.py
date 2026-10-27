import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from tools.checks.product_name import scan as scan_product_name
from tools.rename.rename import Names, apply, plan
from tools.repository import MODULES, REPOSITORY_ROOT, read_text, tracked_files

IGNORED = shutil.ignore_patterns(
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".hypothesis",
    ".svelte-kit",
    "build",
    "dist",
    "*.egg-info",
)


# Built at run time so that renaming the repository never rewrites this test's own text.
NEW_NAME = "Astro" + "labe"
NEW_SLUG = NEW_NAME.lower()
NEW_VENDOR = NEW_NAME.upper()


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    destination = tmp_path / "repository"
    shutil.copytree(REPOSITORY_ROOT, destination, ignore=IGNORED)
    return destination


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in tracked_files(root)}


def rename(root: Path, name: str, slug: str) -> None:
    apply(*plan(root, Names.from_slug(name, slug)))


def test_invalid_names_are_rejected() -> None:
    for name, slug in (("", NEW_SLUG), (NEW_NAME, NEW_NAME), (NEW_NAME, "9x")):
        with pytest.raises(ValueError, match="RENAME_INVALID"):
            Names.from_slug(name, slug)


def test_rename_leaves_no_trace_of_the_old_name(copy: Path) -> None:
    rename(copy, NEW_NAME, NEW_SLUG)

    assert scan_product_name(copy) == []
    old = re.compile(r"(?<![A-Za-z0-9])sextant(?![A-Za-z0-9])", re.IGNORECASE)
    leftovers = [
        f"{path.relative_to(copy)}:{number}"
        for path in tracked_files(copy)
        for number, line in enumerate((read_text(path) or "").splitlines(), start=1)
        if old.search(line) and not line.startswith("previous_vendor_names")
    ]
    assert leftovers == []
    identity = (copy / "identity.toml").read_text()
    assert 'previous_vendor_names = ["SEXTANT"]' in identity
    assert not list(copy.glob("packages/*/src/sextant"))


def test_renamed_packages_install_and_import(copy: Path, tmp_path: Path) -> None:
    rename(copy, NEW_NAME, NEW_SLUG)
    wheels = tmp_path / "wheels"
    for package in sorted((copy / "packages").iterdir()):
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
                str(wheels),
                str(package),
            ],
            check=True,
        )
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-index",
            "--no-deps",
            *map(str, wheels.glob("*.whl")),
        ],
        check=True,
    )
    present = [module for module in MODULES if (copy / "packages" / module).is_dir()]
    imports = "; ".join(f"import {NEW_SLUG}.{module}" for module in present)
    subprocess.run([str(python), "-c", imports], check=True)


def test_rename_round_trip_restores_the_tree(copy: Path) -> None:
    original = snapshot(copy)
    rename(copy, NEW_NAME, NEW_SLUG)
    rename(copy, "Sextant", "sextant")
    restored = snapshot(copy)

    identity_files = {name for name in original if name.endswith("identity.toml")}
    assert set(restored) == set(original)
    assert {name for name in original if original[name] != restored[name]} <= identity_files
    for name in identity_files:
        assert f'previous_vendor_names = ["{NEW_VENDOR}"]' in restored[name].decode()
