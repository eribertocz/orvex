"""Errors of the store contract (`contracts/store/v1`, `ErrorCode`)."""

from __future__ import annotations

from orvex.contracts.store.v1 import ErrorCode, ErrorDetail
from orvex.contracts.store.v1 import StoreError as StoreErrorMessage


class StoreError(Exception):
    """A store failure with a stable code from the contract."""

    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        super().__init__(f"{code}: {message}")
        # Validating through the generated type rejects a code the contract does not define.
        self.code: str = ErrorCode.model_validate(code).root
        self.message = message
        self.details = dict(details)

    def to_message(self) -> StoreErrorMessage:
        return StoreErrorMessage(
            kind="store_error",
            code=ErrorCode.model_validate(self.code),
            message=self.message,
            details=[ErrorDetail(name=name, value=value) for name, value in self.details.items()],
        )
