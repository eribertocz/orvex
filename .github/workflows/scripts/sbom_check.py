"""The release SBOM is valid and complete (#242), checked on every release build:

    python .github/workflows/scripts/sbom_check.py dist

- `SBOM_INVALID`: it does not validate against the vendored CycloneDX 1.6 JSON schema
  (`vendor/cyclonedx`, offline); the schema path of the first error is named.
- `SBOM_VERSION_MISMATCH`: its product version is not `versions.toml`'s.
- `SBOM_INCOMPLETE`: a product wheel of `dist/`, a runtime package of `pylock.toml` or a bundled
  package of `web/package-lock.json` is missing; each missing `name@version` is named.
- `SBOM_HASH_MISMATCH`: a component's hashes differ from its wheel's or its lock's.

What the release ships is read again from the lock files (`sbom_runtime.py`), not from the SBOM.
Any error reading them fails (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource
from sbom_build import identity
from sbom_runtime import LockError, Package, normalize, npm_bundled, python_runtime

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ("bom-1.6.schema.json", "spdx.schema.json", "jsf-0.82.schema.json")


def validator(root: Path) -> jsonschema.protocols.Validator:
    loaded = [json.loads((root / "vendor/cyclonedx" / n).read_text("utf-8")) for n in SCHEMAS]
    registry: Registry[Any] = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in loaded
    )
    kind = jsonschema.validators.validator_for(loaded[0])
    return kind(loaded[0], registry=registry, format_checker=kind.FORMAT_CHECKER)


def expected(root: Path, wheels: Sequence[Path]) -> dict[str, tuple[tuple[str, str], ...]]:
    """Each component the release ships, `name@version`, with the hashes it must carry."""
    found: dict[str, tuple[tuple[str, str], ...]] = {}
    for wheel in wheels:
        name, version = wheel.name.split("-")[:2]
        found[f"{normalize(name)}@{version}"] = (
            ("SHA-256", hashlib.sha256(wheel.read_bytes()).hexdigest()),
        )
    packages: list[Package] = python_runtime(root) + npm_bundled(root)
    for package in packages:
        found[package.key] = package.hashes
    return found


def problems(bom: Any, root: Path, wheels: Sequence[Path]) -> list[str]:
    error = next(iter(sorted(validator(root).iter_errors(bom), key=lambda e: list(e.path))), None)
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_schema_path)
        return [f"SBOM_INVALID {where}: {error.message[:200]}"]
    found = [
        f"SBOM_VERSION_MISMATCH {bom['metadata']['component']['version']}: versions.toml has {v}"
        for v in [identity(root)[1]]
        if bom["metadata"]["component"].get("version") != v
    ]
    listed = {
        f"{c['name']}@{c['version']}": tuple((h["alg"], h["content"]) for h in c.get("hashes", []))
        for c in bom.get("components", [])
    }
    for key, hashes in sorted(expected(root, wheels).items()):
        if key not in listed:
            found.append(f"SBOM_INCOMPLETE {key}: shipped but not listed")
        elif listed[key] != hashes:
            found.append(f"SBOM_HASH_MISMATCH {key}: listed {listed[key]}, shipped {hashes}")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    (folder,) = list(sys.argv[1:] if argv is None else argv)
    dist = Path(folder)
    slug, version = identity(ROOT)
    target = dist / f"{slug}-{version}.cdx.json"
    try:
        bom = json.loads(target.read_text("utf-8"))
        found = problems(bom, ROOT, sorted(dist.glob("*.whl")))
    except (OSError, ValueError, KeyError, TypeError, LockError) as error:
        print(f"::error::SBOM_INVALID {target.name}: {error}")
        return 1
    for problem in found:
        print(f"::error::{problem}")
    print(f"sbom-check: {target.name}, {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
