"""Inside a module's clean environment (#486 G4): the distributions its code imports equal those
it declares, and its own tests import no repository helper.

    <environment>/bin/python -I tools/checks/module_dependencies.py NAMESPACE DISTRIBUTION TESTS

Run by `tools/ci/standalone.py` with the environment's interpreter, so it uses the standard
library and the installed contracts package only. MODULE_DEPENDENCY_UNDECLARED: the code imports
a distribution it does not declare (or one nothing installed provides);
MODULE_DEPENDENCY_UNUSED: it declares one it never imports; MODULE_MANIFEST_DEPENDENCIES_DIFFER:
`module.toml` and the distribution disagree; MODULE_MANIFEST_MISSING: no `module.toml`;
MODULE_TEST_IMPORTS_REPOSITORY: a test imports `tests`, `conformance` or `tools`;
MODULE_TEST_DEPENDENCY_UNDECLARED / _UNUSED: the tests' own dependencies against
`dependencies.tests`; MODULE_ADAPTER_MISSING: a contract the module requires has no public test
adapter in the installed contracts package, so the module could not run without its providers.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import importlib.util
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from orvex.contracts.adapter import required_adapters
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.manifest import Manifest, canonical, parse_manifest
from orvex.contracts.registry import adapters

REPOSITORY_HELPERS = frozenset({"tests", "conformance", "tools"})
Owner = Callable[[str], "str | None"]


def imported_modules(source: str) -> set[str]:
    """The absolute imports of one file, by their full dotted names."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _external(name: str) -> bool:
    top = name.partition(".")[0]
    return top not in sys.stdlib_module_names and top != "__future__"


def _used(own: str, imports: Iterable[str], owner_of: Owner) -> tuple[set[str], list[str]]:
    used: set[str] = set()
    unknown: list[str] = []
    for name in sorted(filter(_external, imports)):
        owner = owner_of(name)
        if owner is None:
            unknown.append(name)
        elif canonical(owner) != canonical(own):
            used.add(canonical(owner))
    return used, unknown


def dependency_problems(
    own: str,
    imports: Iterable[str],
    owner_of: Owner,
    declared: Iterable[str],
    manifest_runtime: Iterable[str],
) -> list[str]:
    used, unknown = _used(own, imports, owner_of)
    declared_names = {canonical(name) for name in declared}
    problems = [
        f"MODULE_DEPENDENCY_UNDECLARED: {own} imports {name}, which nothing installed provides"
        for name in unknown
    ]
    problems += [
        f"MODULE_DEPENDENCY_UNDECLARED: {own} imports {name} without declaring it"
        for name in sorted(used - declared_names)
    ]
    problems += [
        f"MODULE_DEPENDENCY_UNUSED: {own} declares {name} and never imports it"
        for name in sorted(declared_names - used)
    ]
    listed = {canonical(name) for name in manifest_runtime}
    if listed != declared_names:
        problems.append(
            f"MODULE_MANIFEST_DEPENDENCIES_DIFFER: module.toml runtime {sorted(listed)}, "
            f"distribution {sorted(declared_names)}"
        )
    return problems


def suite_problems(
    own: str,
    imports_by_file: Mapping[str, set[str]],
    owner_of: Owner,
    runtime: Iterable[str],
    tests: Iterable[str],
) -> list[str]:
    problems: list[str] = []
    every: set[str] = set()
    for path, imports in sorted(imports_by_file.items()):
        helpers = sorted(n for n in imports if n.partition(".")[0] in REPOSITORY_HELPERS)
        problems += [
            f"MODULE_TEST_IMPORTS_REPOSITORY: {path} imports {name}, a repository helper"
            for name in helpers
        ]
        every |= imports - set(helpers)
    used, unknown = _used(own, every, owner_of)
    problems += [
        f"MODULE_TEST_DEPENDENCY_UNDECLARED: the tests import {name}; nothing installed has it"
        for name in unknown
    ]
    runtime_names = {canonical(name) for name in runtime}
    test_names = {canonical(name) for name in tests}
    problems += [
        f"MODULE_TEST_DEPENDENCY_UNDECLARED: the tests import {name}; list it in dependencies.tests"
        for name in sorted(used - runtime_names - test_names)
    ]
    problems += [
        f"MODULE_TEST_DEPENDENCY_UNUSED: dependencies.tests lists {name}, which no test imports"
        for name in sorted(test_names - used)
    ]
    return problems


def adapter_problems(manifest: Manifest, namespace: str) -> list[str]:
    """MODULE_ADAPTER_MISSING when a required contract has no public test adapter installed."""
    try:
        required_adapters(manifest, adapters(importlib.import_module(f"{namespace}.contracts")))
    except InterfaceContractError as error:
        return [str(error)]
    return []


def file_owners() -> dict[Path, str]:
    """Every installed file, with the distribution that installed it."""
    owners: dict[Path, str] = {}
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or ():
            owners[Path(str(distribution.locate_file(file))).resolve()] = distribution.metadata[
                "Name"
            ]
    return owners


def owner_finder(owners: Mapping[Path, str]) -> Owner:
    def owner_of(name: str) -> str | None:
        try:
            spec = importlib.util.find_spec(name)
        except ImportError, ValueError:
            return None
        if spec is None:
            return None
        if spec.origin is not None and spec.has_location:
            return owners.get(Path(spec.origin).resolve())
        for location in spec.submodule_search_locations or ():
            inside = {
                owner
                for path, owner in owners.items()
                if path.is_relative_to(Path(location).resolve())
            }
            if len(inside) == 1:
                return inside.pop()
        return None

    return owner_of


def main(argv: list[str]) -> int:
    namespace, name, tests = argv
    distribution = importlib.metadata.distribution(name)
    paths = [Path(str(distribution.locate_file(f))) for f in distribution.files or ()]
    manifests = [p for p in paths if p.name == "module.toml" and p.parent.parent.name == namespace]
    if len(manifests) != 1:
        print(f"MODULE_MANIFEST_MISSING: {name} ships {len(manifests)} module.toml, expected one")
        return 1
    manifest = parse_manifest(manifests[0].read_text("utf-8"), manifests[0].parent.name)
    declared = [
        re.match(r"[A-Za-z0-9._-]+", requirement)[0]  # type: ignore[index]
        for requirement in distribution.requires or ()
        if "extra ==" not in requirement
    ]
    owner_of = owner_finder(file_owners())
    imports: set[str] = set()
    for path in paths:
        if path.suffix == ".py":
            imports |= imported_modules(path.read_text("utf-8"))
    problems = dependency_problems(name, imports, owner_of, declared, manifest.runtime)
    by_file = {
        path.relative_to(tests).as_posix(): imported_modules(path.read_text("utf-8"))
        for path in sorted(Path(tests).rglob("*.py"))
    }
    problems += suite_problems(name, by_file, owner_of, manifest.runtime, manifest.tests)
    problems += adapter_problems(manifest, namespace)
    for problem in problems:
        print(problem)
    print(f"module dependencies of {name}: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
