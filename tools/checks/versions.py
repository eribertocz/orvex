"""Version manifest check (docs/process/versioning.md §3).

`versions.toml` lists every distribution, each package's `[project].version` equals its entry, the
server's packaged copy equals the root file, and every contract a module declares in its
`contracts.toml` exists under `contracts/<name>/<vN>`.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tools.repository import MODULES, REPOSITORY_ROOT, identity


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def declared_contracts(path: Path) -> list[tuple[str, str]]:
    data = tomllib.loads(path.read_text("utf-8"))
    found = list(data.get("produces", {}).items())
    for contract, versions in data.get("consumes", {}).items():
        found += [(contract, version) for version in versions]
    return found


def scan(root: Path) -> list[Finding]:
    namespace = identity(root)["python_namespace"]
    manifest_path = root / "versions.toml"
    manifest = tomllib.loads(manifest_path.read_text("utf-8"))
    findings = []
    copy = root / "packages" / "server" / "src" / namespace / "server" / "versions.toml"
    if not copy.is_file() or copy.read_bytes() != manifest_path.read_bytes():
        findings.append(
            Finding("VERSION_MISMATCH", f"{copy.relative_to(root)} differs from versions.toml")
        )
    if "product" not in manifest:
        findings.append(Finding("VERSION_MISSING", "versions.toml has no `product`"))
    for module in sorted(MODULES):
        package = root / "packages" / module
        version = tomllib.loads((package / "pyproject.toml").read_text("utf-8"))["project"][
            "version"
        ]
        if module not in manifest:
            findings.append(Finding("VERSION_MISSING", f"versions.toml has no entry for {module}"))
        elif manifest[module] != version:
            findings.append(
                Finding(
                    "VERSION_MISMATCH",
                    f"{module}: pyproject.toml says {version}, "
                    f"versions.toml says {manifest[module]}",
                )
            )
        declaration = package / "src" / namespace / module / "contracts.toml"
        if not declaration.is_file():
            findings.append(
                Finding("CONTRACT_DECLARATION_MISSING", f"{declaration.relative_to(root)}")
            )
            continue
        for contract, contract_version in declared_contracts(declaration):
            if not (root / "contracts" / contract / contract_version).is_dir():
                findings.append(
                    Finding(
                        "CONTRACT_NOT_FOUND", f"{module} declares {contract} {contract_version}"
                    )
                )
    for extra in sorted(set(manifest) - set(MODULES) - {"product"}):
        findings.append(Finding("VERSION_MISMATCH", f"versions.toml lists unknown module {extra}"))
    return findings


def main() -> int:
    findings = scan(REPOSITORY_ROOT)
    for finding in findings:
        print(finding)
    print(f"versions: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
