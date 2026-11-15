"""The assembly check (#486 G1): the server refuses to start when a chosen implementation lacks a
required capability or version."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Protocol

import pytest
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.manifest import Manifest, Provided, Required
from orvex.contracts.version import Version
from orvex.server import cli
from orvex.server.assembly import (
    AssemblyRefused,
    assembly_problems,
    check_assembly,
    check_installed_assembly,
    installed_manifests,
    load_implementations,
)
from orvex.server.identity import load_identity


class CacheError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        super().__init__(f"{code}: {message}")


class Cache(Protocol):
    def get(self, envelope: CallEnvelope, key: str) -> bytes | None: ...

    def put(self, envelope: CallEnvelope, key: str, value: bytes) -> None: ...

    def size(self, envelope: CallEnvelope) -> int: ...


CACHE = InterfaceContract(
    name="cache",
    version=Version(1, 1),
    protocol=Cache,
    error_type=CacheError,
    capabilities=("read", "write"),
    errors=(),
    operations=(
        Operation("get", "read"),
        Operation("size", "read"),
        Operation("put", "write", changes_state=True),
    ),
)


class Whole:
    def get(self, key: str) -> bytes | None:
        return None

    def put(self, key: str, value: bytes) -> None:
        return None

    def size(self) -> int:
        return 0


class WithoutPut:
    """A planted alternative that omits one operation."""

    def get(self, key: str) -> bytes | None:
        return None

    def size(self) -> int:
        return 0


def provider(
    version: str = "1.1", capabilities: tuple[str, ...] = ("read", "write"), module: str = "alpha"
) -> Manifest:
    provided = Provided("cache", Version.parse(version), capabilities, "Cache")
    return Manifest(module, (provided,), (), (), ())


def consumer(version: str = "1.0", capabilities: tuple[str, ...] = ("read", "write")) -> Manifest:
    required = Required("cache", Version.parse(version), capabilities)
    return Manifest("server", (), (required,), (), ())


def problems(*manifests: Manifest, implementation: object = None) -> list[str]:
    chosen = {
        (m.module, p.contract): Whole() if implementation is None else implementation
        for m in manifests
        for p in m.provides
    }
    return assembly_problems(manifests, {"cache": CACHE}, chosen)


def codes(found: list[str]) -> list[str]:
    return [problem.split(":", 1)[0] for problem in found]


def test_a_complete_implementation_at_a_compatible_version_assembles() -> None:
    assert problems(provider(), consumer()) == []
    assert problems(provider("1.1"), consumer("1.1", ("read",))) == []
    check_assembly((provider(), consumer()), {"cache": CACHE}, {("alpha", "cache"): Whole()})


def test_a_planted_implementation_missing_a_required_capability_is_refused() -> None:
    found = problems(provider(capabilities=("read",)), consumer())
    assert codes(found) == ["ASSEMBLY_CAPABILITY_MISSING"]
    assert "write" in found[0]


def test_a_planted_implementation_omitting_an_operation_is_refused() -> None:
    found = problems(provider(), consumer(), implementation=WithoutPut())
    assert codes(found) == ["ASSEMBLY_OPERATION_MISSING"]
    assert "put" in found[0]


def test_an_operation_that_is_not_callable_is_refused() -> None:
    hollow = types.SimpleNamespace(get=None, put=Whole().put, size=Whole().size)
    assert codes(problems(provider(), consumer(), implementation=hollow)) == [
        "ASSEMBLY_OPERATION_MISSING"
    ]


@pytest.mark.parametrize(
    ("provided", "required"),
    [("1.0", "1.1"), ("1.1", "2.0"), ("1.2", "1.0")],
    ids=["older minor", "other major", "newer than the installed contract"],
)
def test_an_incompatible_version_is_refused(provided: str, required: str) -> None:
    assert "ASSEMBLY_VERSION_INCOMPATIBLE" in codes(
        problems(provider(provided), consumer(required))
    )


def test_no_implementation_and_two_implementations_are_refused() -> None:
    assert codes(problems(consumer())) == ["ASSEMBLY_NO_IMPLEMENTATION"]
    assert codes(problems(provider(), provider(module="beta"), consumer())) == [
        "ASSEMBLY_AMBIGUOUS_IMPLEMENTATION"
    ]


def test_an_unknown_contract_or_capability_is_refused() -> None:
    unknown = Manifest("server", (), (Required("clock", Version(1, 0), ("read",)),), (), ())
    assert codes(problems(provider(), unknown)) == ["ASSEMBLY_CONTRACT_UNKNOWN"]
    assert codes(problems(provider(capabilities=("read", "write", "purge")))) == [
        "ASSEMBLY_CAPABILITY_UNKNOWN"
    ]
    assert codes(problems(provider(), consumer(capabilities=("purge",)))) == [
        "ASSEMBLY_CAPABILITY_UNKNOWN"
    ]


def test_every_problem_is_reported_together() -> None:
    with pytest.raises(AssemblyRefused) as raised:
        check_assembly(
            (provider(capabilities=("read",)), consumer("1.2")),
            {"cache": CACHE},
            {("alpha", "cache"): WithoutPut()},
        )
    assert raised.value.code == "ASSEMBLY_REFUSED"
    assert codes(raised.value.problems) == [
        "ASSEMBLY_VERSION_INCOMPATIBLE",
        "ASSEMBLY_CAPABILITY_MISSING",
    ]
    assert str(raised.value).startswith("ASSEMBLY_REFUSED: ")


def api_module(namespace: str, module: str, **attributes: object) -> Callable[[], None]:
    name = f"{namespace}.{module}.api"
    sys.modules[name] = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(sys.modules[name], key, value)

    def cleanup() -> None:
        sys.modules.pop(name, None)

    return cleanup


def test_implementations_are_loaded_from_the_module_s_api_package_only() -> None:
    namespace = load_identity().python_namespace
    cleanup = api_module(namespace, "alpha", Cache=Whole)
    try:
        loaded = load_implementations(namespace, (provider(),))
        assert loaded == {("alpha", "cache"): Whole}
    finally:
        cleanup()


def test_an_implementation_that_is_not_there_is_refused() -> None:
    namespace = load_identity().python_namespace
    with pytest.raises(AssemblyRefused) as raised:
        load_implementations(namespace, (provider(module="absent"),))
    assert codes(raised.value.problems) == ["ASSEMBLY_IMPLEMENTATION_MISSING"]
    cleanup = api_module(namespace, "alpha")
    try:
        with pytest.raises(AssemblyRefused) as raised:
            load_implementations(namespace, (provider(),))
        assert codes(raised.value.problems) == ["ASSEMBLY_IMPLEMENTATION_MISSING"]
    finally:
        cleanup()


def test_the_installed_modules_assemble() -> None:
    identity = load_identity()
    modules = [manifest.module for manifest in installed_manifests(identity)]
    assert "contracts" in modules
    check_installed_assembly(identity)


def test_the_server_does_not_start_when_assembly_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(identity: object) -> None:
        raise AssemblyRefused(["ASSEMBLY_CAPABILITY_MISSING: planted"])

    monkeypatch.setattr(cli, "check_installed_assembly", refuse)
    assert cli.main([]) == 3
    assert "ASSEMBLY_CAPABILITY_MISSING: planted" in capsys.readouterr().err
