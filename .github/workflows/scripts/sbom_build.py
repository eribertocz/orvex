"""The release SBOM (#242): one CycloneDX 1.6 JSON document, `<slug>-<version>.cdx.json`,
listing every product wheel of `dist/`, every runtime Python package and every npm package
bundled in the web build (`sbom_runtime.py`), each with name, version, `purl`, the hashes its
lock records and its license when known.

    python .github/workflows/scripts/sbom_build.py dist <commit> <commit-time>

with `GITHUB_REPOSITORY` (`owner/name`, set by Actions) naming the repository that built it.

The document is deterministic: its serial number derives from the version and commit, and its
timestamp is the commit's, so the same commit always yields the same file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tomllib
import urllib.parse
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sbom_runtime import Package, normalize, npm_bundled, python_runtime

ROOT = Path(__file__).resolve().parents[3]


def identity(root: Path) -> tuple[str, str]:
    """(slug, product version), from `identity.toml` and `versions.toml`."""
    slug = tomllib.loads((root / "identity.toml").read_text("utf-8"))["product"]["slug"]
    version = tomllib.loads((root / "versions.toml").read_text("utf-8"))["product"]
    return str(slug), str(version)


def wheel_license(wheel: Path) -> str:
    """The wheel's own `License-Expression`, from its METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        for line in archive.read(name).decode("utf-8").splitlines():
            if line.startswith("License-Expression:"):
                return line.partition(":")[2].strip()
    raise SystemExit(f"{wheel.name} states no License-Expression")


def wheel_component(wheel: Path) -> dict[str, Any]:
    name, version = wheel.name.split("-")[:2]
    name = normalize(name)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return {
        "type": "library",
        "bom-ref": f"pkg:pypi/{name}@{version}",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{name}@{version}?file_name={urllib.parse.quote(wheel.name)}",
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "licenses": [{"expression": wheel_license(wheel)}],
    }


def package_component(package: Package) -> dict[str, Any]:
    if package.ecosystem == "npm":
        scope, _, base = package.name.rpartition("/")
        path = f"{urllib.parse.quote(scope)}/{base}" if scope else base
        purl = f"pkg:npm/{path}@{package.version}"
    else:
        purl = f"pkg:pypi/{package.name}@{package.version}"
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": purl,
        "name": package.name,
        "version": package.version,
        "purl": purl,
    }
    if package.hashes:
        component["hashes"] = [{"alg": a, "content": c} for a, c in package.hashes]
    if package.license:
        component["licenses"] = [{"expression": package.license}]
    if package.urls:
        component["externalReferences"] = [
            {"type": "distribution", "url": url, "hashes": [{"alg": "SHA-256", "content": digest}]}
            for url, digest in package.urls
        ]
    return component


def document(
    root: Path, wheels: Sequence[Path], repository: str, commit: str, committed: str
) -> dict[str, Any]:
    slug, version = identity(root)
    serial = f"{repository}@{version}@{commit}"
    components = [wheel_component(w) for w in sorted(wheels)]
    components += [package_component(p) for p in python_runtime(root)]
    components += [package_component(p) for p in npm_bundled(root)]
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial)}",
        "version": 1,
        "metadata": {
            "timestamp": committed,
            "component": {
                "type": "application",
                "bom-ref": f"pkg:github/{repository}@v{version}",
                "name": slug,
                "version": version,
                "purl": f"pkg:github/{repository}@v{version}",
            },
        },
        "components": components,
    }


def main(argv: Sequence[str] | None = None) -> int:
    folder, commit, committed = list(sys.argv[1:] if argv is None else argv)
    dist = Path(folder)
    slug, version = identity(ROOT)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        raise SystemExit("GITHUB_REPOSITORY (owner/name) names the repository that built this")
    bom = document(ROOT, sorted(dist.glob("*.whl")), repository, commit, committed)
    target = dist / f"{slug}-{version}.cdx.json"
    target.write_text(json.dumps(bom, indent=2, sort_keys=True) + "\n", "utf-8")
    print(f"sbom: {target.name}, {len(bom['components'])} components")
    return 0


if __name__ == "__main__":
    sys.exit(main())
