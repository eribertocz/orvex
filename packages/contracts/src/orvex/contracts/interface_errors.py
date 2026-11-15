"""The error of the interface-contract machinery: a stable code, a message and details.

Codes: INTERFACE_CONTRACT_INVALID, INTERFACE_VERSION_INVALID, INTERFACE_TYPE_NOT_SERIALIZABLE,
INTERFACE_UNSUPPORTED_OPERATION_DECLARED, INTERFACE_OPERATION_UNKNOWN,
INTERFACE_OPERATION_MISSING, INTERFACE_VALUE_NOT_SERIALIZABLE, INTERFACE_ERROR_UNDECLARED,
KIT_INVALID, KIT_CASE_COUNT_CHANGED, KIT_OPERATION_UNCOVERED, KIT_FAILED and
MODULE_MANIFEST_INVALID.
"""

from __future__ import annotations

from collections.abc import Mapping


class InterfaceContractError(Exception):
    """A declaration, a value or a run that breaks the interface-contract rules (ADR-0033)."""

    def __init__(self, code: str, message: str, details: Mapping[str, str] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})
