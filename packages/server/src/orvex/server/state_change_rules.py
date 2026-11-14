"""Server mode's rules for a request that changes state (#438): what refuses cross-site request
forgery before any route runs.

- `GET`, `HEAD` and `OPTIONS` never change state; every other method is a state change.
- A state change must carry `Origin` equal to the instance's public origin (scheme, host and
  port); a missing one is refused. Until the administrator sets the public origin, every state
  change is refused (fail closed).
- `Sec-Fetch-Site`, when the browser sends it, must be `same-origin`.
- The body must be `application/json`: no form or other simple request a page on another site
  could send.

Each refusal is `ORIGIN_REFUSED`, naming the header that failed. The origin rules run before
authorization and the body type after it (#445: authentication, origin/CSRF, authorization,
validation of the content, execution).
"""

from __future__ import annotations

from collections.abc import Mapping

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
JSON = "application/json"
_SHOWN = 200


def changes_state(method: str) -> bool:
    return method.upper() not in SAFE_METHODS


def refusal(
    method: str, headers: Mapping[str, str], public_origin: str | None
) -> tuple[str, str] | None:
    """The header that refuses a state change, and its value as shown; None when it may go on:
    its origin, then its body type."""
    return origin_refusal(method, headers, public_origin) or content_refusal(method, headers)


def origin_refusal(
    method: str, headers: Mapping[str, str], public_origin: str | None
) -> tuple[str, str] | None:
    """The origin and CSRF step (#445 precision 1): `Origin` and `Sec-Fetch-Site`."""
    if not changes_state(method):
        return None
    origin = headers.get("origin")
    if origin is None or public_origin is None or origin.lower() != public_origin:
        return "origin", (origin or "")[:_SHOWN]
    site = headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        return "sec-fetch-site", site[:_SHOWN]
    return None


def content_refusal(method: str, headers: Mapping[str, str]) -> tuple[str, str] | None:
    """The validation of the content (#445 precision 1), after authorization: the body type.
    A `DELETE` carries no body: no form or simple cross-site request can send one."""
    if not changes_state(method) or method.upper() == "DELETE":
        return None
    media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != JSON:
        return "content-type", media[:_SHOWN]
    return None
