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

import pytest
from orvex.contracts.interface import InterfaceContract, Operation, Version
from orvex.contracts.interface_errors import InterfaceContractError
from pydantic import BaseModel

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


class TestDescription:
    def test_the_description_names_every_part_of_the_contract(self) -> None:
        described = LEDGER.describe()
        assert described["name"] == "ledger"
        assert described["version"] == "1.0"
        assert described["capabilities"] == ["read", "write"]
        assert described["errors"] == ["LEDGER_NAME_EMPTY"]
        operations = described["operations"]
        assert isinstance(operations, dict)
        assert sorted(operations) == ["get", "names", "put"]
        assert operations["put"] == {
            "capability": "write",
            "errors": ["LEDGER_NAME_EMPTY"],
            "parameters": [
                {
                    "name": "reading",
                    "kind": "positional-or-keyword",
                    "type": READING_TYPE,
                    "default": False,
                }
            ],
            "returns": "None",
        }
        assert operations["get"]["returns"] == f"{READING_TYPE} | None"
        assert operations["names"]["returns"] == "tuple[str, ...]"

    def test_operations_are_found_by_name_and_by_capability(self) -> None:
        assert LEDGER.operation("get").capability == "read"
        assert [o.name for o in LEDGER.operations_of("read")] == ["get", "names"]
        assert code_of(lambda: LEDGER.operation("drop")) == "INTERFACE_OPERATION_UNKNOWN"


def _plain(self: object, value: int) -> None: ...


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
        def unannotated(self: object, value):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
            return value

        def variadic(self: object, *values: int) -> None: ...

        def keywords(self: object, **values: int) -> None: ...

        for method in (unannotated, variadic, keywords):
            with pytest.raises(InterfaceContractError) as raised:
                contract_for(method)
            assert raised.value.code == "INTERFACE_CONTRACT_INVALID"


def _takes(annotation: object) -> Callable[..., object]:
    def method(self: object, value: Any) -> None: ...  # noqa: ANN401 - replaced below

    method.__annotations__ = {"value": annotation, "return": type(None)}
    return method


def _returns(annotation: object) -> Callable[..., object]:
    def method(self: object) -> None: ...

    method.__annotations__ = {"return": annotation}
    return method


SERIALIZABLE: list[object] = [
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
        assert contract_for(_takes(Outer)).value_classes() == frozenset({Outer, Inner})
        assert contract_for(_returns(list[Color | Shape])).value_classes() == {Color, Shape}
        assert LEDGER.value_classes() == {Reading}

    def test_replace_validates_again(self) -> None:
        assert code_of(lambda: dataclasses.replace(LEDGER, name="Bad")) == (
            "INTERFACE_CONTRACT_INVALID"
        )
