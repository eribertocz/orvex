"""#436: every refusal the access module can send through the HTTP API is registered in
`http-api` v1, with the exact spelling the module raises; its store's own refusals stop a start
or a command and never reach a response."""

from __future__ import annotations

from orvex.access import errors

from tools.checks import error_codes
from tools.repository import REPOSITORY_ROOT

SENT_OVER_HTTP = {
    "ACCESS_FORBIDDEN",
    "LAST_BUILDER",
    "LAST_ADMINISTRATOR",
    "USER_NAME_TAKEN",
    "VALIDATION_FAILED",
    "AUDIT_UNAVAILABLE",
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
}


def test_the_module_raises_exactly_these_codes() -> None:
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
