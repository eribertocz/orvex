"""The HTTP answer of an `access` refusal in server mode (#438): its status, and what of it a
response may show.

A refusal's message and its safe details become the ErrorEnvelope; a delay's length goes only to
`Retry-After`, so the body of `LOGIN_DELAYED` is the same for every account.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    AUDIT_UNAVAILABLE,
    LOGIN_DELAYED,
    LOGIN_FAILED,
    OIDC_FAILED,
    OIDC_NOT_CONFIGURED,
    OIDC_REFRESH_REQUIRED,
    PASSWORD_CHANGE_REQUIRED,
    PASSWORD_REJECTED,
    SESSION_EXPIRED,
    VALIDATION_FAILED,
    AccessError,
)
from orvex.contracts.http_api import v1
from orvex.server.app import error_response
from pydantic import ValidationError

STATUS = {
    LOGIN_FAILED: 401,
    SESSION_EXPIRED: 401,
    PASSWORD_REJECTED: 400,
    PASSWORD_CHANGE_REQUIRED: 403,
    ACCESS_FORBIDDEN: 403,
    LOGIN_DELAYED: 429,
    VALIDATION_FAILED: 422,
    AUDIT_UNAVAILABLE: 503,
    OIDC_FAILED: 400,
    OIDC_NOT_CONFIGURED: 404,
    OIDC_REFRESH_REQUIRED: 401,
}
# A delay's length goes to `Retry-After` only; which OIDC step or setting failed goes to the audit
# log and the operator, never to the browser.
_HIDDEN = frozenset({"retry_after", "reason", "setting"})


def access_error_response(error: AccessError, request_id: str) -> JSONResponse:
    details = [
        v1.ErrorDetail(name=name, value=value)
        for name, value in sorted(error.details.items())
        if name not in _HIDDEN
    ]
    response = error_response(
        STATUS.get(error.code, 403), error.code, error.message, request_id, details
    )
    if error.code == LOGIN_DELAYED:
        response.headers["Retry-After"] = error.details.get("retry_after", "1")
    response.headers["Cache-Control"] = "no-store"
    return response


def validation_response(error: ValidationError | None, request_id: str) -> JSONResponse:
    """A body that does not match the contract; the details name fields, never their values."""
    names = (
        sorted(
            {".".join(str(part) for part in problem["loc"]) or "body" for problem in error.errors()}
        )
        if error is not None
        else ["body"]
    )
    details = [v1.ErrorDetail(name=name, value="invalid") for name in names]
    return error_response(
        422, VALIDATION_FAILED, "The request does not match the contract", request_id, details
    )
