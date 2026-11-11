"""Server mode's HTTP application, as far as signing in goes (#438): the status routes, the
sign-in routes, and the rules every request passes first.

Before any route:
1. a state change (`state_change_rules.py`) without the public origin, from another site, or
   without a JSON body is `ORIGIN_REFUSED`;
2. every API route but `health`, `version` and sign-in needs a live session, re-read now:
   `SESSION_EXPIRED` otherwise. A session opened with a temporary password reaches only the
   session, sign-out and password routes: `PASSWORD_CHANGE_REQUIRED` elsewhere.

No route may accept both a method that changes state and one that does not: the application is
refused at construction if one does. The composition of server mode (`--server`) comes later.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.errors import PASSWORD_CHANGE_REQUIRED, SESSION_EXPIRED, AccessError
from orvex.contracts.http_api import v1
from orvex.server import sign_in_http
from orvex.server.app import (
    REQUEST_ID_HEADER,
    error_response,
    install_error_handlers,
    mount_status,
)
from orvex.server.sign_in_errors import access_error_response
from orvex.server.state_change_rules import SAFE_METHODS, changes_state, refusal
from orvex.server.versions import Versions
from starlette.concurrency import run_in_threadpool

PUBLIC = frozenset({"/api/v1/health", "/api/v1/version", sign_in_http.LOGIN})
CHANGE_ONLY = frozenset({sign_in_http.SESSION, sign_in_http.LOGOUT, sign_in_http.PASSWORD})
API_PREFIX = "/api/"
log = logging.getLogger("orvex.server.http")


def create_server_app(
    versions: Versions, sign_in: SignIn, settings: Settings, slug: str
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    cookie = sign_in_http.cookie_name(slug)

    @app.middleware("http")
    async def request_rules(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            refused = await rules(request)
            response = refused if refused is not None else await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"request_id": request.state.request_id})
            response = error_response(
                500, "INTERNAL_ERROR", "Unexpected server error", request.state.request_id
            )
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        log.info(
            "request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    async def rules(request: Request) -> Response | None:
        request_id: str = request.state.request_id
        if changes_state(request.method):
            current = await run_in_threadpool(settings.current)
            found = refusal(request.method, request.headers, current.public_origin)
            if found is not None:
                name, value = found
                return error_response(
                    403,
                    "ORIGIN_REFUSED",
                    "This instance accepts changes only from its own pages",
                    request_id,
                    [v1.ErrorDetail(name=name, value=value)],
                )
        path = request.url.path
        if not path.startswith(API_PREFIX) or path in PUBLIC:
            return None
        session_id = request.cookies.get(cookie)
        if session_id is None:
            return access_error_response(
                AccessError(SESSION_EXPIRED, "The session has ended: sign in again."), request_id
            )
        try:
            live = await run_in_threadpool(sign_in.session, session_id)
        except AccessError as error:
            return access_error_response(error, request_id)
        if live.password_change_only and path not in CHANGE_ONLY:
            return access_error_response(
                AccessError(PASSWORD_CHANGE_REQUIRED, "Change the temporary password first."),
                request_id,
            )
        request.state.session = live
        request.state.session_id = session_id
        return None

    install_error_handlers(app, lambda path: None)
    mount_status(app, versions)
    sign_in_http.mount_sign_in(app, sign_in, settings, cookie)
    checked_route_table(app)
    return app


def checked_route_table(app: FastAPI) -> None:
    """Refuse a route that takes both a safe method and one that changes state."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods = set(route.methods or ())
            if methods & SAFE_METHODS and methods - SAFE_METHODS:
                raise ValueError(f"route {route.path} mixes safe and state-changing methods")
