"""The interface-contract format (#486 §5 step 1, ADR-0033): how a contract declares its
operations, inputs, outputs, errors, capabilities and version.

An interface contract is a `typing.Protocol` whose public methods are the operations (their
annotations are the inputs and outputs; the first parameter of each is the call envelope,
`envelope: CallEnvelope`), plus an `InterfaceContract` that gives the contract a
name and a `major.minor` version, groups the operations into capabilities, and lists the error
codes each operation may raise through the contract's error type (built as
`error_type(code, message, details)`). The declaration is checked when it is built: a refused
declaration never exists.
"""

from __future__ import annotations

import inspect
import re
import typing
from dataclasses import dataclass

from .envelope import CallEnvelope
from .interface_errors import InterfaceContractError
from .serializable import crossing_problem, type_name
from .version import Version

NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
ERROR_CODE = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*")
# A capability is either implemented in full or not provided: this code can never stand in for
# an operation a contract declares (#486 G1).
UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
KINDS: dict[inspect._ParameterKind, str] = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional-or-keyword",
    inspect.Parameter.KEYWORD_ONLY: "keyword-only",
}


@dataclass(frozen=True, slots=True)
class Operation:
    """One operation: its method name, the capability it belongs to, the codes it raises,
    whether it changes state (then every call carries an idempotency key) and the version it
    exists from (`None`: the first version of the contract's major)."""

    name: str
    capability: str
    errors: tuple[str, ...] = ()
    changes_state: bool = False
    since: Version | None = None


@dataclass(frozen=True, slots=True)
class InterfaceContract:
    """An interface contract: what any implementation must do, and nothing about how."""

    name: str
    version: Version
    protocol: type
    error_type: type[Exception]
    capabilities: tuple[str, ...]
    errors: tuple[str, ...]
    operations: tuple[Operation, ...]

    def __post_init__(self) -> None:
        _check(self)

    def operation(self, name: str) -> Operation:
        for operation in self.operations:
            if operation.name == name:
                return operation
        raise InterfaceContractError(
            "INTERFACE_OPERATION_UNKNOWN", f"contract {self.name} has no operation {name!r}"
        )

    def offers(self, name: str) -> bool:
        """Whether `name` is one of the contract's operations (a public attribute of a binding)."""
        return not name.startswith("_") and any(o.name == name for o in self.operations)

    def since(self, operation: Operation) -> Version:
        """The version `operation` exists from."""
        return operation.since or Version(self.version.major, 0)

    def operations_of(self, capability: str) -> tuple[Operation, ...]:
        return tuple(o for o in self.operations if o.capability == capability)

    def signature(self, name: str) -> inspect.Signature:
        """The operation's signature without `self`, its annotations resolved."""
        return _signature(self.protocol, self.operation(name).name)

    def value_classes(self) -> frozenset[type]:
        """The enums, models and dataclasses that may cross this contract."""
        classes: set[type] = set()
        for operation in self.operations:
            for annotation in _annotations(self.signature(operation.name)):
                crossing_problem(annotation, classes)
        return frozenset(classes)

    def describe(self) -> dict[str, object]:
        """The contract as plain data: what the compatibility check compares (#486 G3)."""
        operations: dict[str, object] = {}
        for operation in self.operations:
            signature = self.signature(operation.name)
            operations[operation.name] = {
                "capability": operation.capability,
                "changes_state": operation.changes_state,
                "since": str(self.since(operation)),
                "errors": sorted(operation.errors),
                "parameters": [
                    {
                        "name": parameter.name,
                        "kind": KINDS[parameter.kind],
                        "type": type_name(parameter.annotation),
                        "default": parameter.default is not inspect.Parameter.empty,
                    }
                    for parameter in signature.parameters.values()
                ],
                "returns": type_name(signature.return_annotation),
            }
        return {
            "name": self.name,
            "version": str(self.version),
            "capabilities": sorted(self.capabilities),
            "errors": sorted(self.errors),
            "operations": operations,
        }


def _invalid(contract: InterfaceContract, text: str) -> InterfaceContractError:
    return InterfaceContractError(
        "INTERFACE_CONTRACT_INVALID",
        f"contract {contract.name!r}: {text}",
        {"contract": contract.name},
    )


def _check(contract: InterfaceContract) -> None:
    if not NAME.fullmatch(contract.name):
        raise _invalid(contract, "a name is lowercase-kebab-case")
    if not (isinstance(contract.error_type, type) and issubclass(contract.error_type, Exception)):
        raise _invalid(contract, "the error type must be an exception class")
    if UNSUPPORTED_OPERATION in contract.errors or any(
        UNSUPPORTED_OPERATION in operation.errors for operation in contract.operations
    ):
        raise InterfaceContractError(
            "INTERFACE_UNSUPPORTED_OPERATION_DECLARED",
            f"contract {contract.name!r}: {UNSUPPORTED_OPERATION} can never stand in for an "
            "operation; an implementation provides a capability in full or not at all",
            {"contract": contract.name},
        )
    _check_names(contract)
    _check_operations(contract)


def _check_names(contract: InterfaceContract) -> None:
    if len(set(contract.capabilities)) != len(contract.capabilities) or not all(
        NAME.fullmatch(capability) for capability in contract.capabilities
    ):
        raise _invalid(contract, "capabilities are unique and lowercase-kebab-case")
    if len(set(contract.errors)) != len(contract.errors) or not all(
        ERROR_CODE.fullmatch(code) for code in contract.errors
    ):
        raise _invalid(contract, "error codes are unique and UPPER_SNAKE_CASE")
    names = [operation.name for operation in contract.operations]
    if not names or len(set(names)) != len(names):
        raise _invalid(contract, "a contract has at least one operation, each named once")
    for operation in contract.operations:
        if operation.capability not in contract.capabilities:
            raise _invalid(
                contract, f"{operation.name}: capability {operation.capability!r} is undeclared"
            )
        if operation.since is not None and not (
            operation.since.major == contract.version.major and operation.since <= contract.version
        ):
            raise _invalid(
                contract,
                f"{operation.name}: since {operation.since} is not a version of {contract.version}",
            )
        unknown = sorted(set(operation.errors) - set(contract.errors))
        if unknown:
            raise _invalid(
                contract, f"{operation.name}: error codes {unknown} are not the contract's"
            )
    empty = [c for c in contract.capabilities if not contract.operations_of(c)]
    if empty:
        raise _invalid(contract, f"capabilities {empty} have no operation")
    public = sorted(
        name
        for name, value in vars(contract.protocol).items()
        if not name.startswith("_") and (callable(value) or isinstance(value, property))
    )
    if public != sorted(names):
        raise _invalid(
            contract,
            f"the protocol's public methods {public} must be the operations {sorted(names)}",
        )


def _check_operations(contract: InterfaceContract) -> None:
    for operation in contract.operations:
        member = vars(contract.protocol)[operation.name]
        if not inspect.isfunction(member):
            raise _invalid(contract, f"{operation.name}: an operation is a plain method")
        signature = _signature(contract.protocol, operation.name)
        first = next(iter(signature.parameters.values()), None)
        if (
            first is None
            or first.name != "envelope"
            or first.annotation is not CallEnvelope
            or first.kind is inspect.Parameter.KEYWORD_ONLY
        ):
            raise _invalid(
                contract, f"{operation.name}: the first parameter is `envelope: CallEnvelope`"
            )
        for parameter in signature.parameters.values():
            if parameter.kind not in KINDS:
                raise _invalid(contract, f"{operation.name}: *{parameter.name} cannot be described")
            if parameter.annotation is inspect.Parameter.empty:
                raise _invalid(contract, f"{operation.name}: {parameter.name} has no annotation")
        if signature.return_annotation is inspect.Signature.empty:
            raise _invalid(contract, f"{operation.name}: the result has no annotation")
        for annotation in _annotations(signature):
            problem = crossing_problem(annotation, set())
            if problem is not None:
                raise InterfaceContractError(
                    "INTERFACE_TYPE_NOT_SERIALIZABLE",
                    f"contract {contract.name!r}, {operation.name}: {problem}",
                    {"contract": contract.name, "operation": operation.name},
                )


def _signature(protocol: type, name: str) -> inspect.Signature:
    method = vars(protocol)[name]
    hints = typing.get_type_hints(method)
    signature = inspect.signature(method)
    parameters = [
        parameter.replace(annotation=hints.get(parameter.name, inspect.Parameter.empty))
        for parameter in list(signature.parameters.values())[1:]
    ]
    returns = hints.get("return", inspect.Signature.empty)
    return signature.replace(parameters=parameters, return_annotation=returns)


def _annotations(signature: inspect.Signature) -> list[object]:
    found: list[object] = [parameter.annotation for parameter in signature.parameters.values()]
    return [*found, signature.return_annotation]
