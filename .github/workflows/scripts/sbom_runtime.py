"""What a release ships, read from the lock files (#242): the runtime Python packages of
`pylock.toml` and the npm packages bundled in the web build. The release SBOM is built from
this, and `sbom_check.py` compares the SBOM with it.

`pylock.toml` records no dependency group, so the runtime set is the dependency closure of the
`runtime` group of `pyproject.toml` and of each product package, walked through the installed
distributions' `Requires-Dist` (the job installs the lock first). A requirement whose marker is
false here (behind an extra, or for another platform or Python) is not followed, as pip would
not install it; any other requirement the lock lacks fails (fail closed), and so does a
requirement with extras, whose added dependencies this walk does not follow. Versions and hashes
come from the lock, never from the installed copy.
"""

from __future__ import annotations

import base64
import json
import re
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement


class LockError(Exception):
    """The lock files cannot say what the release ships."""


@dataclass(frozen=True, slots=True)
class Package:
    ecosystem: str  # "pypi" or "npm"
    name: str
    version: str
    hashes: tuple[tuple[str, str], ...]  # (CycloneDX algorithm, hex digest)
    license: str = ""
    urls: tuple[tuple[str, str], ...] = ()  # (file URL, sha256) of each locked file

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_lock(root: Path) -> dict[str, Package]:
    """Every package of `pylock.toml`, by normalized name."""
    try:
        listed = tomllib.loads((root / "pylock.toml").read_text("utf-8"))["packages"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise LockError(f"cannot read pylock.toml: {error}") from error
    found = {}
    for entry in listed:
        files = list(entry.get("wheels", [])) + ([entry["sdist"]] if "sdist" in entry else [])
        urls = tuple((str(f["url"]), str(f["hashes"]["sha256"])) for f in files)
        hashes = (("SHA-256", urls[0][1]),) if len(urls) == 1 else ()
        name = normalize(str(entry["name"]))
        found[name] = Package("pypi", name, str(entry["version"]), hashes, urls=urls)
    return found


def roots(root: Path) -> list[Requirement]:
    """The runtime group and each product package's dependencies."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    listed = list(pyproject.get("dependency-groups", {}).get("runtime", []))
    for package in sorted((root / "packages").glob("*/pyproject.toml")):
        listed += (
            tomllib.loads(package.read_text("utf-8")).get("project", {}).get("dependencies", [])
        )
    return [Requirement(text) for text in listed]


def product_names(root: Path) -> set[str]:
    names = set()
    for package in (root / "packages").glob("*/pyproject.toml"):
        names.add(normalize(tomllib.loads(package.read_text("utf-8"))["project"]["name"]))
    return names


def python_runtime(root: Path) -> list[Package]:
    """The runtime closure, with the lock's versions and hashes, sorted by name."""
    lock = python_lock(root)
    products = product_names(root)
    seen: set[str] = set()
    pending = roots(root)
    while pending:
        requirement = pending.pop()
        name = normalize(requirement.name)
        marker = requirement.marker
        if marker is not None and not marker.evaluate({"extra": ""}):
            continue
        # Before the skip below, so it fails in every order, even for a package already walked.
        if requirement.extras:
            raise LockError(f"{requirement}: the SBOM does not follow extras yet")
        if name in products or name in seen:
            continue
        if name not in lock:
            raise LockError(f"{name} is a runtime dependency that pylock.toml lacks")
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError as error:
            raise LockError(
                f"{name} is locked but not installed: install the lock first"
            ) from error
        pending += [Requirement(text) for text in requires]
    return [_licensed(lock[name]) for name in sorted(seen)]


def _licensed(package: Package) -> Package:
    try:
        found = metadata.metadata(package.name)
    except metadata.PackageNotFoundError:
        return package
    text = found.get("License-Expression") or ""
    return Package(
        package.ecosystem, package.name, package.version, package.hashes, text, package.urls
    )


def npm_bundled(root: Path) -> list[Package]:
    """Every non-dev package of `web/package-lock.json`, once per name and version."""
    try:
        listed = json.loads((root / "web/package-lock.json").read_text("utf-8"))["packages"]
    except (OSError, KeyError, ValueError) as error:
        raise LockError(f"cannot read web/package-lock.json: {error}") from error
    found: dict[str, Package] = {}
    for path, entry in listed.items():
        if not path or entry.get("dev") or entry.get("link"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        version = str(entry["version"])
        algorithm, _, digest = str(entry.get("integrity", "")).partition("-")
        hashes = ((_ALGORITHMS[algorithm], _hex(digest)),) if algorithm in _ALGORITHMS else ()
        package = Package("npm", name, version, hashes, str(entry.get("license") or ""))
        found[package.key] = package
    return [found[key] for key in sorted(found)]


_ALGORITHMS = {"sha512": "SHA-512", "sha384": "SHA-384", "sha256": "SHA-256", "sha1": "SHA-1"}


def _hex(base64_digest: str) -> str:
    return base64.b64decode(base64_digest).hex()
