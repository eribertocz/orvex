"""#256: a registered secret never appears in an error, in any form it takes on its way there."""

from __future__ import annotations

import json
import urllib.parse

import pytest
from orvex.store.errors import StoreError, redact, register_secret

SECRET = "pa'ss\"wo\\rd ü&="


@pytest.mark.parametrize(
    "form",
    [
        SECRET,
        SECRET.replace("'", "''"),  # in an SQL string
        SECRET.replace('"', '""'),  # in an SQL identifier
        SECRET.replace("\\", "\\\\").replace("'", "\\'"),  # in a libpq connection string
        json.dumps(SECRET)[1:-1],  # in JSON
        json.dumps(SECRET, ensure_ascii=False)[1:-1],  # in JSON, letters kept
        urllib.parse.quote(SECRET, safe=""),  # in a URL
        urllib.parse.quote_plus(SECRET, safe=""),  # in a form-encoded URL
    ],
    ids=["raw", "sql-string", "sql-identifier", "libpq", "json", "json-unicode", "url", "url-plus"],
)
def test_every_escaped_form_of_a_secret_is_redacted(form: str) -> None:
    register_secret(SECRET)
    message = f"could not connect: password '{form}' was refused"
    assert redact(message) == "could not connect: password '***' was refused"
    error = StoreError("STORE_QUERY_FAILED", message, {"duckdb": f"... {form} ..."})
    assert form not in str(error)
    assert error.details == {"duckdb": "... *** ..."}


def test_an_empty_secret_redacts_nothing() -> None:
    register_secret("")
    assert redact("nothing to hide") == "nothing to hide"
