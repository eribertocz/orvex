"""Running one kit case in a process of its own, built from the case's declared environment
(#486 G1): its variables and its extension directory, and nothing inherited from the kit's
process, so no case patches an implementation's internals to reach a condition.

    python -m orvex.contracts.kit_process   # reads one request on stdin

The child finds the kit by its `module:NAME` reference and the implementation by its factory's,
so both are defined at module level; it then checks that the kit it found describes itself as
the parent's does and that the case's check is the same function, so it can never run another
case in its place (KIT_CASE_NOT_IMPORTABLE otherwise). The child runs the case through the
binding it is asked for and sends its `CaseResult` back through the serializing codec, in a file
of the case's own directory; a child that fails, answers anything else or takes longer than
`TIMEOUT` fails the case (KIT_CASE_PROCESS_FAILED).
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .codec import Codec
from .interface_errors import InterfaceContractError
from .kit import CaseEnvironment, Idempotent, Kit, KitCase
from .kit_case import CaseResult, run_case
from .version import Version

TIMEOUT = 300
CODEC = Codec(frozenset({CaseResult}))


def _reference(value: object) -> str:
    """`module:qualified.name` of a module-level class or function."""
    name = getattr(value, "__qualname__", "")
    if "<" in name or not name:
        raise ValueError(f"{value!r} is not defined at module level")
    return f"{value.__module__}:{name}"


def _resolve(reference: str) -> object:
    module, _, name = reference.partition(":")
    found: object = importlib.import_module(module)
    for part in name.split("."):
        found = getattr(found, part)
    return found


def _root(module: str) -> str:
    """The directory a module is imported from by its name, whatever made it importable here
    (pytest's importlib mode imports test modules without adding their root to `sys.path`)."""
    found = sys.modules[module]
    path = Path(str(found.__file__))
    return str(path.parents[module.count(".") + (path.name == "__init__.py")])


def _check_reference(case: KitCase) -> str:
    check = case.check
    return _reference(check.call if isinstance(check, Idempotent) else check)


def _kit_reference(kit: Kit, module: str) -> str:
    """`module:NAME` of the kit, which the module must hold at its top level."""
    for name, value in vars(sys.modules[module]).items():
        if value is kit:
            return f"{module}:{name}"
    raise ValueError(f"kit {kit.contract.name} is not a top-level name of {module}")


def _variables(environment: CaseEnvironment, directory: Path) -> dict[str, str]:
    """The child's whole environment: the declared variables and the extension directory."""
    variables = dict(environment.variables)
    extensions = environment.extensions
    if extensions is not None:
        target = directory / "extensions"
        source = None if extensions.source is None else _source(extensions.source)
        if source is None:
            target.mkdir()
        else:
            shutil.copytree(source, target, ignore=_without(extensions.without))
        variables[extensions.variable] = str(target)
    return variables


def _source(variable: str) -> Path:
    value = os.environ.get(variable)
    if not value or not Path(value).is_dir():
        raise ValueError(f"the extension source {variable} names no directory")
    return Path(value)


def _without(names: tuple[str, ...]) -> Callable[[str, list[str]], set[str]]:
    """Ignores every file of the named extensions (`<name>.<suffix>`), at any depth."""

    def ignored(directory: str, entries: list[str]) -> set[str]:
        return {entry for entry in entries if entry.partition(".")[0] in names}

    return ignored


def run_case_in_its_own_process(
    kit: Kit,
    case: KitCase,
    make: Callable[[], object],
    serialized: bool,
    consumer: Version,
    provided: Version,
) -> CaseResult:
    """One case, in a process built from its declared environment."""
    environment = case.environment or CaseEnvironment()
    try:
        check, factory = _check_reference(case), _reference(make)
        module = check.partition(":")[0]
        roots = [_root(module), _root(factory.partition(":")[0])]
        request = {
            "path": [*roots, *(entry for entry in sys.path if entry)],
            "kit": _kit_reference(kit, module),
            "description": kit.describe(),
            "case": case.id,
            "check": check,
            "factory": factory,
            "serialized": serialized,
            "consumer": str(consumer),
            "provided": str(provided),
            "result": "",
        }
    except (ValueError, KeyError, TypeError) as error:
        return CaseResult(case.id, "failed", f"KIT_CASE_NOT_IMPORTABLE: {error}")
    with tempfile.TemporaryDirectory(prefix="kit-case-") as directory:
        try:
            variables = _variables(environment, Path(directory))
        except (OSError, ValueError) as error:
            return CaseResult(case.id, "failed", f"KIT_CASE_PROCESS_FAILED: {error}")
        answer = Path(directory) / "result"
        request["result"] = str(answer)
        try:
            done = subprocess.run(
                [sys.executable, "-m", __name__],
                input=json.dumps(request),
                env=variables,
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CaseResult(case.id, "failed", f"KIT_CASE_PROCESS_FAILED: over {TIMEOUT} s")
        try:
            result = CODEC.decode(answer.read_bytes())
        except OSError, InterfaceContractError:
            result = None
    if done.returncode != 0 or not isinstance(result, CaseResult) or result.id != case.id:
        tail = (done.stderr or done.stdout)[-2000:]
        return CaseResult(case.id, "failed", f"KIT_CASE_PROCESS_FAILED: {tail}")
    return result


def _the_parent_s_case(request: dict[str, Any]) -> tuple[Kit, KitCase] | None:
    """The kit and case the parent named, or None when the reference holds anything else."""
    kit = _resolve(request["kit"])
    if not isinstance(kit, Kit) or json.loads(json.dumps(kit.describe())) != request["description"]:
        return None
    for case in kit.cases:
        if case.id == request["case"] and _check_reference(case) == request["check"]:
            return kit, case
    return None


def main() -> int:
    request = json.loads(sys.stdin.read())
    sys.path[:0] = [entry for entry in request["path"] if entry not in sys.path]
    found = _the_parent_s_case(request)
    make = _resolve(request["factory"])
    if found is None or not callable(make):
        detail = f"KIT_CASE_NOT_IMPORTABLE: {request['kit']} does not hold the parent's case"
        result = CaseResult(request["case"], "failed", detail)
    else:
        kit, case = found
        result = run_case(
            kit,
            case,
            make,
            bool(request["serialized"]),
            Version.parse(request["consumer"]),
            Version.parse(request["provided"]),
        )
    Path(request["result"]).write_bytes(CODEC.encode(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
