"""The interface-contract format: declaration, validation and description (#486 §5 step 1)."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Protocol

import pyarrow as pa
import pytest
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.serializable import crossing_problem
from orvex.contracts.store import v1 as store_v1
from orvex.contracts.version import Version
from pydantic import AwareDatetime, BaseModel

from .sample_contract import LEDGER, LedgerError, Reading

READING_TYPE = f"{Reading.__module__}.{Reading.__qualname__}"


class Color(Enum):
    RED = "red"


class Shape(BaseModel):
    side: Decimal


@dataclass(frozen=True, slots=True)
class Inner:
    when: datetime


@dataclass(frozen=True, slots=True)
class Outer:
    inner: Inner
    items: tuple[Inner, ...]


@dataclass(slots=True)
class Mutable:
    value: int


def contract_for(method: Callable[..., object], **changes: object) -> InterfaceContract:
    protocol = type("Probe", (Protocol,), {"use": method})
    fields: dict[str, Any] = {
        "name": "probe",
        "version": Version(1, 0),
        "protocol": protocol,
        "error_type": LedgerError,
        "capabilities": ("use",),
        "errors": (),
        "operations": (Operation("use", "use"),),
    }
    fields.update(changes)
    return InterfaceContract(**fields)


def code_of(build: Callable[[], object]) -> str:
    with pytest.raises(InterfaceContractError) as raised:
        build()
    return raised.value.code


class TestVersion:
    @pytest.mark.parametrize(
        ("text", "expected"), [("1.0", Version(1, 0)), ("2.13", Version(2, 13))]
    )
    def test_parses_major_dot_minor(self, text: str, expected: Version) -> None:
        assert Version.parse(text) == expected
        assert str(expected) == text

    @pytest.mark.parametrize("text", ["1", "01.0", "1.0.0", "0.1", "v1.0", "1.01", ""])
    def test_anything_else_is_refused(self, text: str) -> None:
        assert code_of(lambda: Version.parse(text)) == "INTERFACE_VERSION_INVALID"

    def test_a_version_provides_its_major_at_a_lower_or_equal_minor(self) -> None:
        assert Version(1, 3).provides(Version(1, 0))
        assert Version(1, 3).provides(Version(1, 3))
        assert not Version(1, 3).provides(Version(1, 4))
        assert not Version(2, 0).provides(Version(1, 0))
        assert not Version(1, 0).provides(Version(2, 0))

    def test_the_previous_version_is_the_previous_minor_of_the_major(self) -> None:
        assert Version(1, 3).previous() == Version(1, 2)
        assert Version(2, 0).previous() is None


class TestDescription:
    def test_the_description_names_every_part_of_the_contract(self) -> None:
        described = LEDGER.describe()
        assert described["name"] == "ledger"
        assert described["version"] == "1.1"
        assert described["capabilities"] == ["read", "write"]
        assert described["errors"] == ["LEDGER_NAME_EMPTY"]
        operations = described["operations"]
        assert isinstance(operations, dict)
        assert sorted(operations) == ["count", "names", "readings", "record"]
        envelope = {
            "name": "envelope",
            "kind": "positional-or-keyword",
            "type": f"{CallEnvelope.__module__}.CallEnvelope",
            "default": None,
        }
        assert operations["record"] == {
            "capability": "write",
            "changes_state": True,
            "since": "1.0",
            "errors": ["LEDGER_NAME_EMPTY"],
            "parameters": [
                envelope,
                {
                    "name": "reading",
                    "kind": "positional-or-keyword",
                    "type": READING_TYPE,
                    "default": None,
                },
            ],
            "returns": "int",
        }
        assert operations["readings"]["returns"] == f"tuple[{READING_TYPE}, ...]"
        assert operations["names"]["changes_state"] is False
        assert operations["count"]["since"] == "1.1"

    def test_operations_are_found_by_name_and_by_capability(self) -> None:
        assert LEDGER.operation("readings").capability == "read"
        assert [o.name for o in LEDGER.operations_of("read")] == ["readings", "names", "count"]
        assert LEDGER.since(LEDGER.operation("count")) == Version(1, 1)
        assert LEDGER.since(LEDGER.operation("names")) == Version(1, 0)
        assert code_of(lambda: LEDGER.operation("drop")) == "INTERFACE_OPERATION_UNKNOWN"


def _plain(self: object, envelope: CallEnvelope, value: int) -> None: ...


class TestRefusals:
    def test_a_valid_probe_is_accepted(self) -> None:
        assert contract_for(_plain).operation("use").name == "use"

    @pytest.mark.parametrize(
        "changes",
        [
            {"name": "Probe"},
            {"name": ""},
            {"operations": (Operation("use", "other"),)},
            {"capabilities": ("use", "idle")},
            {"capabilities": ("use", "use")},
            {"operations": (Operation("use", "use", ("PROBE_FAILED",)),)},
            {"errors": ("probe failed",)},
            {"errors": ("PROBE_FAILED", "PROBE_FAILED")},
            {"operations": (Operation("use", "use"), Operation("use", "use"))},
            {"operations": (Operation("use", "use"), Operation("other", "use"))},
            {"operations": ()},
            {"error_type": int},
            {"operations": (Operation("use", "use", since=Version(1, 1)),)},
            {"operations": (Operation("use", "use", since=Version(2, 0)),)},
        ],
        ids=lambda changes: ",".join(changes),
    )
    def test_an_inconsistent_declaration_is_refused(self, changes: dict[str, object]) -> None:
        assert code_of(lambda: contract_for(_plain, **changes)) == "INTERFACE_CONTRACT_INVALID"

    def test_a_protocol_method_that_is_not_declared_is_refused(self) -> None:
        protocol = type("Probe", (Protocol,), {"use": _plain, "hidden": _plain})
        assert code_of(lambda: contract_for(_plain, protocol=protocol)) == (
            "INTERFACE_CONTRACT_INVALID"
        )

    @pytest.mark.parametrize("where", ["contract", "operation"])
    def test_unsupported_operation_can_never_be_declared(self, where: str) -> None:
        unsupported = ("UNSUPPORTED_OPERATION",)
        changes: dict[str, object] = {"errors": unsupported}
        if where == "operation":
            changes["operations"] = (Operation("use", "use", unsupported),)
        assert code_of(lambda: contract_for(_plain, **changes)) == (
            "INTERFACE_UNSUPPORTED_OPERATION_DECLARED"
        )

    def test_unannotated_and_variadic_parameters_are_refused(self) -> None:
        def unannotated(self: object, envelope: CallEnvelope, value):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
            return value

        def variadic(self: object, envelope: CallEnvelope, *values: int) -> None: ...

        def keywords(self: object, envelope: CallEnvelope, **values: int) -> None: ...

        for method in (unannotated, variadic, keywords):
            with pytest.raises(InterfaceContractError) as raised:
                contract_for(method)
            assert raised.value.code == "INTERFACE_CONTRACT_INVALID"


def _takes(annotation: object) -> Callable[..., object]:
    def method(self: object, envelope: CallEnvelope, value: Any) -> None: ...  # noqa: ANN401

    method.__annotations__ = {"envelope": CallEnvelope, "value": annotation, "return": type(None)}
    return method


def _returns(annotation: object) -> Callable[..., object]:
    def method(self: object, envelope: CallEnvelope) -> None: ...

    method.__annotations__ = {"envelope": CallEnvelope, "return": annotation}
    return method


def _no_envelope(self: object, value: int) -> None: ...


def _envelope_second(self: object, value: int, envelope: CallEnvelope) -> None: ...


def _envelope_keyword(self: object, *, envelope: CallEnvelope) -> None: ...


def _envelope_untyped(self: object, envelope: str) -> None: ...


class TestEnvelopeParameter:
    @pytest.mark.parametrize(
        "method", [_no_envelope, _envelope_second, _envelope_keyword, _envelope_untyped]
    )
    def test_an_operation_without_the_envelope_first_is_refused(
        self, method: Callable[..., object]
    ) -> None:
        with pytest.raises(InterfaceContractError) as raised:
            contract_for(method)
        assert raised.value.code == "INTERFACE_CONTRACT_INVALID"
        assert "the first parameter is `envelope: CallEnvelope`" in raised.value.message


SERIALIZABLE: list[object] = [
    AwareDatetime,
    int,
    bool,
    float,
    str,
    bytes,
    Decimal,
    datetime,
    date,
    time,
    timedelta,
    type(None),
    int | None,
    Literal["a", 1, True, None],
    tuple[int, ...],
    tuple[str, Decimal],
    list[Decimal],
    dict[str, list[int]],
    Color,
    Shape,
    Outer,
]

LIVE: list[object] = [
    Any,
    object,
    Callable[[], int],
    Iterator[int],
    tuple,
    list,
    dict,
    dict[int, str],
    tuple[()],
    threading.Lock,
    Mutable,
    Literal[Decimal(1)],
    list[Callable[[], int]],
    int | Iterator[int],
]


class TestCrossingTypes:
    @pytest.mark.parametrize("annotation", SERIALIZABLE, ids=repr)
    def test_plain_values_may_cross(self, annotation: object) -> None:
        contract_for(_takes(annotation))
        contract_for(_returns(annotation))

    @pytest.mark.parametrize("annotation", LIVE, ids=repr)
    def test_a_live_object_may_never_cross(self, annotation: object) -> None:
        assert (
            code_of(lambda: contract_for(_takes(annotation))) == "INTERFACE_TYPE_NOT_SERIALIZABLE"
        )
        assert code_of(lambda: contract_for(_returns(annotation))) == (
            "INTERFACE_TYPE_NOT_SERIALIZABLE"
        )

    def test_the_classes_that_cross_are_collected(self) -> None:
        found = contract_for(_takes(Outer)).value_classes()
        assert found == frozenset({CallEnvelope, Outer, Inner})
        assert contract_for(_returns(list[Color | Shape])).value_classes() == {
            CallEnvelope,
            Color,
            Shape,
        }
        assert LEDGER.value_classes() == {CallEnvelope, Reading}

    def test_replace_validates_again(self) -> None:
        assert code_of(lambda: dataclasses.replace(LEDGER, name="Bad")) == (
            "INTERFACE_CONTRACT_INVALID"
        )


@dataclass(frozen=True, slots=True)
class Use:
    key: str | None
    ttl_seconds: int | None


@dataclass(frozen=True, slots=True)
class Stored:
    table: pa.Table
    withheld: tuple[str, ...]
    estimate: int | None


class ResultCache(Protocol):
    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None: ...

    def put(self, envelope: CallEnvelope, use: Use, stored: Stored) -> None: ...

    def size_bytes(self, envelope: CallEnvelope) -> int: ...

    def len(self, envelope: CallEnvelope) -> int: ...


def test_the_format_expresses_the_result_cache_that_g1_substitutes() -> None:
    cache = InterfaceContract(
        name="result-cache",
        version=Version(1, 0),
        protocol=ResultCache,
        error_type=LedgerError,
        capabilities=("cache",),
        errors=(),
        operations=(
            Operation("get", "cache"),
            Operation("put", "cache", changes_state=True),
            Operation("size_bytes", "cache"),
            Operation("len", "cache"),
        ),
    )
    assert cache.value_classes() == {CallEnvelope, Use, Stored}
    assert cache.describe()["operations"]["get"]["returns"] == (  # type: ignore[index]
        f"{Stored.__module__}.Stored | None"
    )


def test_every_generated_store_type_may_cross() -> None:
    models = [
        kind
        for kind in vars(store_v1).values()
        if isinstance(kind, type)
        and issubclass(kind, BaseModel)
        and kind.__module__ == store_v1.__name__
    ]
    assert models
    assert {kind.__name__: crossing_problem(kind, set()) for kind in models} == {
        kind.__name__: None for kind in models
    }
