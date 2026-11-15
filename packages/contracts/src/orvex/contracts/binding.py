"""The two bindings an implementation is reached through (#486 G5): in process, and serialized
as an independent process would reach it.

Through either binding only the contract's operations are callable, each with its call
envelope first (`orvex.contracts.envelope`), validated before the implementation runs: a
refused call never reaches it (ContractCallRefused). The binding serves the implementation at
the version it provides (`provided`, the contract's version by default). An error crosses only
when it is the contract's error type with a code the operation declares, or the machinery's own
`InterfaceContractError`; anything else raised is INTERFACE_ERROR_UNDECLARED. Serialized, the
envelope, the arguments, the result and the error's code, message and details each pass
through the codec, so a live object is refused (INTERFACE_VALUE_NOT_SERIALIZABLE) and nothing
is shared with the caller.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .codec import Codec
from .envelope import envelope_refusal
from .interface import InterfaceContract
from .interface_errors import ContractCallRefused, InterfaceContractError
from .version import Version


class Bound:
    """An implementation of `contract` at version `provided`, reached through one binding."""

    __slots__ = ("_codec", "_contract", "_implementation", "_provided")

    def __init__(
        self,
        contract: InterfaceContract,
        implementation: object,
        *,
        serialized: bool,
        provided: Version | None = None,
    ) -> None:
        version = contract.version if provided is None else provided
        if not contract.version.provides(version):
            raise ContractCallRefused(
                "CONTRACT_VERSION_UNSUPPORTED",
                f"contract {contract.name} {contract.version} cannot serve an implementation "
                f"at {version}",
                {"contract": contract.name, "provided": str(version)},
            )
        self._contract = contract
        self._implementation = implementation
        self._provided = version
        self._codec = Codec(contract.value_classes()) if serialized else None

    @property
    def contract(self) -> InterfaceContract:
        return self._contract

    @property
    def provided(self) -> Version:
        return self._provided

    @property
    def serialized(self) -> bool:
        return self._codec is not None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if not self._contract.offers(name):
            raise AttributeError(f"contract {self._contract.name} has no operation {name!r}")
        return functools.partial(self.call, name)

    def call(
        self, operation: str, envelope: object, /, *arguments: object, **keywords: object
    ) -> Any:  # noqa: ANN401 - the operation's result
        declared = self._contract.operation(operation)
        refusal = envelope_refusal(envelope, self._contract, declared, self._provided)
        if refusal is not None:
            raise refusal
        method = getattr(self._implementation, operation, None)
        if not callable(method):
            raise InterfaceContractError(
                "INTERFACE_OPERATION_MISSING",
                f"the implementation of {self._contract.name} has no operation {operation}",
            )
        codec = self._codec
        if codec is not None:
            envelope, arguments, keywords = _crossed(codec, (envelope, arguments, keywords))
        try:
            result = method(envelope, *arguments, **keywords)
        except self._contract.error_type as error:
            code = getattr(error, "code", None)
            if code not in declared.errors:
                raise self._undeclared(operation, repr(code)) from (None if codec else error)
            if codec is None:
                raise
            raise self._rebuilt(codec, error) from None
        except InterfaceContractError as error:
            if codec is None:
                raise
            raise _copied(codec, error) from None
        except Exception as error:  # noqa: BLE001 - fail closed: nothing undeclared crosses
            raise self._undeclared(operation, type(error).__name__) from (None if codec else error)
        return result if codec is None else _crossed(codec, result)

    def _undeclared(self, operation: str, what: str) -> InterfaceContractError:
        return InterfaceContractError(
            "INTERFACE_ERROR_UNDECLARED",
            f"{self._contract.name}.{operation} raised {what}, which it does not declare",
            {"contract": self._contract.name, "operation": operation},
        )

    def _rebuilt(self, codec: Codec, error: Exception) -> Exception:
        parts = tuple(getattr(error, name, None) for name in ("code", "message", "details"))
        code, message, details = _crossed(codec, parts)
        if not isinstance(message, str) or not isinstance(details, dict):
            raise InterfaceContractError(
                "INTERFACE_VALUE_NOT_SERIALIZABLE",
                f"a {type(error).__name__} needs a message and details to cross the contract",
            ) from None
        return self._contract.error_type(code, message, details)


def _copied(codec: Codec, error: InterfaceContractError) -> InterfaceContractError:
    """The machinery's own error, crossed as values: its class, code, message and details."""
    code, message, details = _crossed(codec, (error.code, error.message, error.details))
    kind = ContractCallRefused if type(error) is ContractCallRefused else InterfaceContractError
    return kind(code, message, details)


def _crossed(codec: Codec, value: object) -> Any:  # noqa: ANN401 - the value decoded back
    return codec.decode(codec.encode(value))
