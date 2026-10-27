"""Errors of the store contract (`contracts/store/v1`, `ErrorCode`)."""

from __future__ import annotations

from orvex.contracts.store.v1 import ErrorCode, ErrorDetail
from orvex.contracts.store.v1 import StoreError as StoreErrorMessage

_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """A value that must never appear in an error: it is replaced by `***` everywhere."""
    if value:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in tuple(_SECRETS):  # a copy: another thread may register a secret meanwhile
        text = text.replace(secret, "***")
    return text


class StoreError(Exception):
    """A store failure with a stable code from the contract. Registered secrets are redacted."""

    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        message = redact(message)
        details = {name: redact(value) for name, value in details.items()}
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
