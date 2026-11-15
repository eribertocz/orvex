"""#436, #486: every refusal of the access contracts (`orvex.contracts.access.errors`) that can
reach the HTTP API is registered in `http-api` v1, with the exact spelling raised; the store's
own refusals and the contract's own two stop a start or a command, or are turned into another
refusal, and never reach a response."""

from __future__ import annotations

from orvex.contracts.access import errors
from tools.checks import error_codes
from tools.repository import REPOSITORY_ROOT

SENT_OVER_HTTP = {
    "ACCESS_FORBIDDEN",
    "LAST_BUILDER",
    "LAST_ADMINISTRATOR",
    "USER_NAME_TAKEN",
    "VALIDATION_FAILED",
    "AUDIT_UNAVAILABLE",
    "LOGIN_FAILED",
    "LOGIN_DELAYED",
    "PASSWORD_REJECTED",
    "SESSION_EXPIRED",
    "PASSWORD_CHANGE_REQUIRED",
    "OIDC_FAILED",
    "OIDC_NOT_CONFIGURED",
    "OIDC_REFRESH_REQUIRED",
    "USER_NOT_FOUND",
    "GROUP_NOT_FOUND",
    "IDENTITY_NOT_FOUND",
    "IDENTITY_TAKEN",
    "PROJECT_NOT_SERVED",
    "LOOKUP_LIMITED",
}
# They stop a start or a command, or report on the audit log; none reaches a response.
STORE_ONLY = {
    "ACCESS_STORE_NEWER",
    "ACCESS_STORE_UNSAFE",
    "AUDIT_EVENT_INVALID",
    "AUDIT_RETENTION_INVALID",
    "AUDIT_CHAIN_BROKEN",
    "AUDIT_CHECKPOINT_INVALID",
    "AUDIT_KEY_UNSAFE",
    "ACCESS_INSTANCE_UNKNOWN",
    "CONTEXT_INVALID",
}


def test_the_contract_declares_exactly_these_codes() -> None:
    defined = {
        value for name, value in vars(errors).items() if name.isupper() and isinstance(value, str)
    }
    assert defined == SENT_OVER_HTTP | STORE_ONLY
    assert all(getattr(errors, code) == code for code in defined)


def test_the_codes_sent_over_http_are_registered() -> None:
    registered = error_codes.contract_codes(REPOSITORY_ROOT)
    assert {code: registered.get(code) for code in SENT_OVER_HTTP} == dict.fromkeys(
        SENT_OVER_HTTP, "http-api"
    )
    assert not STORE_ONLY & set(registered)
