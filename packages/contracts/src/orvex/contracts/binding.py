"""The two bindings an implementation is reached through (#486 G5): in process, and serialized
as an independent process would reach it.

Through either binding only the contract's operations are callable, and an error crosses only
when it is the contract's error type with a code the operation declares
(INTERFACE_ERROR_UNDECLARED otherwise). Serialized, the arguments, the result and the error's
code, message and details each pass through the codec, so a live object is refused
(INTERFACE_VALUE_NOT_SERIALIZABLE) and nothing is shared with the caller.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .codec import Codec
from .interface import InterfaceContract
from .interface_errors import InterfaceContractError


class Bound:
    """An implementation of `contract`, reached through one binding."""

    __slots__ = ("_codec", "_contract", "_implementation")

    def __init__(
        self, contract: InterfaceContract, implementation: object, *, serialized: bool
    ) -> None:
        self._contract = contract
        self._implementation = implementation
        self._codec = Codec(contract.value_classes()) if serialized else None

    @property
    def serialized(self) -> bool:
        return self._codec is not None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_") or name not in {o.name for o in self._contract.operations}:
            raise AttributeError(f"contract {self._contract.name} has no operation {name!r}")
        return functools.partial(self.call, name)

    def call(self, operation: str, /, *arguments: object, **keywords: object) -> Any:  # noqa: ANN401
        declared = self._contract.operation(operation)
        method = getattr(self._implementation, operation, None)
        if not callable(method):
            raise InterfaceContractError(
                "INTERFACE_OPERATION_MISSING",
                f"the implementation of {self._contract.name} has no operation {operation}",
            )
        codec = self._codec
        if codec is not None:
            arguments, keywords = _crossed(codec, (arguments, keywords))
        try:
            result = method(*arguments, **keywords)
        except self._contract.error_type as error:
            code = getattr(error, "code", None)
            if code not in declared.errors:
                raise InterfaceContractError(
                    "INTERFACE_ERROR_UNDECLARED",
                    f"{self._contract.name}.{operation} raised {code!r}, which it does not declare",
                ) from error
            if codec is None:
                raise
            raise self._rebuilt(codec, error) from None
        return result if codec is None else _crossed(codec, result)

    def _rebuilt(self, codec: Codec, error: Exception) -> Exception:
        parts = tuple(getattr(error, name, None) for name in ("code", "message", "details"))
        code, message, details = _crossed(codec, parts)
        if not isinstance(message, str) or not isinstance(details, dict):
            raise InterfaceContractError(
                "INTERFACE_VALUE_NOT_SERIALIZABLE",
                f"a {type(error).__name__} needs a message and details to cross the contract",
            ) from error
        return self._contract.error_type(code, message, details)


def _crossed(codec: Codec, value: object) -> Any:  # noqa: ANN401 - the value decoded back
    return codec.decode(codec.encode(value))
