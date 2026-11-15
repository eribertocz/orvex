"""The provider fetcher the access implementation is composed with (#439, #486): the server's
requests to an OIDC provider, under its SSRF rules.

`access` opens no connection itself. The composer gives the implementation one fetcher when it
builds it, in process; the fetcher sees each provider as a `ProviderTarget` (plain values) and
answers bytes, or refuses with `OIDC_FAILED` (`oidc_failure.failed`, reason `network` or
`token_exchange`). It is part of the composition, not an operation: nothing crosses a contract
call through it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from orvex.contracts.access.values import ProviderTarget


class ProviderFetcher(Protocol):
    def get(self, provider: ProviderTarget, url: str) -> bytes: ...

    def post_form(
        self, provider: ProviderTarget, url: str, form: Mapping[str, str], basic: tuple[str, str]
    ) -> bytes: ...
