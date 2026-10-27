"""P0-08: versions.toml, package versions and contract declarations."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from tools.checks.versions import scan
from tools.repository import MODULES, REPOSITORY_ROOT, identity

NAMESPACE = identity(REPOSITORY_ROOT)["python_namespace"]


def copy_layout(tmp_path: Path) -> Path:
    """The files the check reads: identity, manifest, packages' metadata and contracts."""
    shutil.copy(REPOSITORY_ROOT / "identity.toml", tmp_path)
    shutil.copy(REPOSITORY_ROOT / "versions.toml", tmp_path)
    shutil.copytree(REPOSITORY_ROOT / "contracts", tmp_path / "contracts")
    for module in MODULES:
        source = REPOSITORY_ROOT / "packages" / module
        target = tmp_path / "packages" / module
        (target / "src" / NAMESPACE / module).mkdir(parents=True)
        shutil.copy(source / "pyproject.toml", target)
        declaration = Path("src") / NAMESPACE / module / "contracts.toml"
        shutil.copy(source / declaration, target / declaration)
    copy = Path("packages/server/src") / NAMESPACE / "server" / "versions.toml"
    shutil.copy(REPOSITORY_ROOT / copy, tmp_path / copy)
    return tmp_path


def codes(root: Path) -> list[str]:
    return [finding.code for finding in scan(root)]


def test_repository_is_consistent() -> None:
    assert scan(REPOSITORY_ROOT) == []


def test_every_distribution_is_listed() -> None:
    manifest = tomllib.loads((REPOSITORY_ROOT / "versions.toml").read_text("utf-8"))
    assert set(manifest) == {"product", *MODULES}


def test_package_version_differing_from_manifest_fails(tmp_path: Path) -> None:
    root = copy_layout(tmp_path)
    pyproject = root / "packages" / "store" / "pyproject.toml"
    text = pyproject.read_text("utf-8").replace('version = "0.0.0"', 'version = "0.2.0"', 1)
    pyproject.write_text(text, "utf-8")
    assert codes(root) == ["VERSION_MISMATCH"]


def test_distribution_missing_from_manifest_fails(tmp_path: Path) -> None:
    root = copy_layout(tmp_path)
    manifest = root / "versions.toml"
    lines = [
        line for line in manifest.read_text("utf-8").splitlines() if not line.startswith("mcp")
    ]
    manifest.write_text("\n".join(lines) + "\n", "utf-8")
    shutil.copy(manifest, root / "packages/server/src" / NAMESPACE / "server" / "versions.toml")
    assert codes(root) == ["VERSION_MISSING"]


def test_packaged_copy_must_equal_the_manifest(tmp_path: Path) -> None:
    root = copy_layout(tmp_path)
    copy = root / "packages/server/src" / NAMESPACE / "server" / "versions.toml"
    copy.write_text(copy.read_text("utf-8") + "\n", "utf-8")
    assert codes(root) == ["VERSION_MISMATCH"]


def test_declaring_an_unknown_contract_fails(tmp_path: Path) -> None:
    root = copy_layout(tmp_path)
    declaration = root / "packages" / "semantic" / "src" / NAMESPACE / "semantic" / "contracts.toml"
    declaration.write_text('produces = {}\nconsumes = { "http-api" = ["v1", "v2"] }\n', "utf-8")
    assert codes(root) == ["CONTRACT_NOT_FOUND"]
    declaration.write_text('produces = { "no-such-contract" = "v1" }\nconsumes = {}\n', "utf-8")
    assert codes(root) == ["CONTRACT_NOT_FOUND"]
