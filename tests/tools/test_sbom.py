"""#242: the release SBOM lists what the release ships, validates against CycloneDX 1.6 and is
refused when incomplete, invalid or for another version."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tools.repository import REPOSITORY_ROOT

sys.path.insert(0, str(REPOSITORY_ROOT / ".github/workflows/scripts"))
import sbom_build
import sbom_check
import sbom_runtime

IDENTITY = tomllib.loads((REPOSITORY_ROOT / "identity.toml").read_text("utf-8"))["product"]
NAMESPACE = IDENTITY["python_namespace"]  # the product's distributions are <namespace>-<module>
STORE = f"{NAMESPACE}-store"
COMMIT = "0" * 40
WHEN = "2026-11-10T00:00:00Z"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    wheel = tmp_path / f"{NAMESPACE}_store-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{NAMESPACE}_store-0.3.0.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {STORE}\nVersion: 0.3.0\n"
            "License-Expression: Apache-2.0\n",
        )
    return tmp_path


def bom(dist: Path) -> dict[str, Any]:
    return sbom_build.document(
        REPOSITORY_ROOT, sorted(dist.glob("*.whl")), "owner/repo", COMMIT, WHEN
    )


def codes(found: list[str]) -> list[str]:
    return [problem.split(":")[0] for problem in found]


def test_a_complete_sbom_validates_and_passes(dist: Path) -> None:
    built = bom(dist)
    assert sbom_check.problems(built, REPOSITORY_ROOT, sorted(dist.glob("*.whl"))) == []
    assert built["specVersion"] == "1.6"
    assert built["metadata"]["component"]["version"] == sbom_build.identity(REPOSITORY_ROOT)[1]
    assert bom(dist) == built  # deterministic: the same commit gives the same document


def test_it_reads_both_lock_files_and_every_component_has_name_version_purl(dist: Path) -> None:
    built = bom(dist)
    purls = [c["purl"] for c in built["components"]]
    assert any(p.startswith(f"pkg:pypi/{STORE}@0.3.0") for p in purls)
    assert any(p.startswith("pkg:pypi/fastapi@") for p in purls)  # the runtime group
    assert any(p.startswith("pkg:pypi/starlette@") for p in purls)  # its closure
    assert not any(p.startswith(("pkg:pypi/pytest@", "pkg:pypi/ruff@")) for p in purls)  # dev
    assert any(p.startswith("pkg:npm/") for p in purls)
    assert all(c["name"] and c["version"] and c["purl"] for c in built["components"])
    locked = tomllib.loads((REPOSITORY_ROOT / "pylock.toml").read_text("utf-8"))["packages"]
    versions = {sbom_runtime.normalize(p["name"]): p["version"] for p in locked}
    for component in built["components"]:
        if component["purl"].startswith("pkg:pypi/") and component["name"] != STORE:
            assert versions[component["name"]] == component["version"]
    bundled = json.loads((REPOSITORY_ROOT / "web/package-lock.json").read_text("utf-8"))["packages"]
    expected = {
        f"{path.rsplit('node_modules/', 1)[-1]}@{entry['version']}"
        for path, entry in bundled.items()
        if path and not entry.get("dev")
    }
    listed = {
        f"{c['name']}@{c['version']}"
        for c in built["components"]
        if c["purl"].startswith("pkg:npm/")
    }
    assert listed == expected


def test_a_missing_component_fails_naming_it(dist: Path) -> None:
    built = bom(dist)
    gone = built["components"].pop(3)
    found = sbom_check.problems(built, REPOSITORY_ROOT, sorted(dist.glob("*.whl")))
    assert found == [f"SBOM_INCOMPLETE {gone['name']}@{gone['version']}: shipped but not listed"]
    built["components"] = [c for c in built["components"] if c["name"] != STORE]
    found = sbom_check.problems(built, REPOSITORY_ROOT, sorted(dist.glob("*.whl")))
    assert f"SBOM_INCOMPLETE {STORE}@0.3.0" in codes(found)


def test_an_invalid_sbom_fails_with_the_schema_path(dist: Path) -> None:
    built = bom(dist)
    built["components"][0]["hashes"][0]["alg"] = "MD-6"
    (found,) = sbom_check.problems(built, REPOSITORY_ROOT, sorted(dist.glob("*.whl")))
    assert found.startswith("SBOM_INVALID properties/components/")  # where the schema refused
    assert "'MD-6'" in found


def test_another_product_version_fails(dist: Path) -> None:
    built = bom(dist)
    built["metadata"]["component"]["version"] = "9.9.9"
    assert codes(sbom_check.problems(built, REPOSITORY_ROOT, sorted(dist.glob("*.whl")))) == [
        "SBOM_VERSION_MISMATCH 9.9.9"
    ]


def test_a_changed_hash_fails(dist: Path) -> None:
    built = copy.deepcopy(bom(dist))
    component = next(c for c in built["components"] if c["name"] == STORE)
    component["hashes"][0]["content"] = hashlib.sha256(b"another file").hexdigest()
    assert codes(sbom_check.problems(built, REPOSITORY_ROOT, sorted(dist.glob("*.whl")))) == [
        f"SBOM_HASH_MISMATCH {STORE}@0.3.0"
    ]


def test_a_runtime_dependency_the_lock_lacks_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = {k: v for k, v in sbom_runtime.python_lock(REPOSITORY_ROOT).items() if k != "starlette"}
    monkeypatch.setattr(sbom_runtime, "python_lock", lambda root: lock)
    with pytest.raises(sbom_runtime.LockError, match="starlette is a runtime dependency"):
        sbom_runtime.python_runtime(REPOSITORY_ROOT)


def test_the_command_fails_on_a_missing_sbom(
    dist: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    assert sbom_check.main([str(dist)]) == 1
    assert "SBOM_INVALID" in capsys.readouterr().out
    assert sbom_build.main([str(dist), COMMIT, WHEN]) == 0
    assert sbom_check.main([str(dist)]) == 0


def test_the_vendored_schemas_are_the_pinned_ones() -> None:
    folder = REPOSITORY_ROOT / "vendor/cyclonedx"
    pinned = tomllib.loads((folder / "PIN.toml").read_text("utf-8"))["sha256"]
    assert set(pinned) == {p.name for p in folder.iterdir() if p.name != "PIN.toml"}
    for name, digest in pinned.items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
