"""#486 G1: the store's kit proves every code each operation declares, and nothing else; a code
declared without its case, or a case of an undeclared code, refuses the kit."""

from __future__ import annotations

import dataclasses

import pytest
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.store.interface import STORE
from orvex.contracts.store.kit import ISOLATED, STORE_KIT
from orvex.contracts.store.kit_codes import code_cases


def _with_errors(operation: str, errors: tuple[str, ...]) -> object:
    operations = tuple(
        dataclasses.replace(o, errors=errors) if o.name == operation else o
        for o in STORE.operations
    )
    return dataclasses.replace(STORE, operations=operations)


def test_every_declared_code_has_its_case_in_the_kit() -> None:
    declared = {(o.name, code) for o in STORE.operations for code in o.errors}
    proven = {
        (case.operation, case.id.split(".raises.")[1].upper().replace("-", "_"))
        for case in STORE_KIT.cases
        if ".raises." in case.id
    }
    assert proven == declared


def test_a_code_declared_without_its_case_refuses_the_kit() -> None:
    widened = _with_errors("held", ("STORE_BUSY",))
    with pytest.raises(InterfaceContractError) as caught:
        code_cases(widened, ISOLATED)  # type: ignore[arg-type]
    assert caught.value.code == "KIT_CODE_UNPROVEN"
    assert "('held', 'STORE_BUSY')" in caught.value.message


def test_a_case_of_a_code_no_longer_declared_refuses_the_kit() -> None:
    narrowed = _with_errors("json_frame", ())
    with pytest.raises(InterfaceContractError) as caught:
        code_cases(narrowed, ISOLATED)  # type: ignore[arg-type]
    assert caught.value.code == "KIT_CODE_UNPROVEN"
    assert "('json_frame', 'STORE_TYPE_UNSUPPORTED')" in caught.value.message
