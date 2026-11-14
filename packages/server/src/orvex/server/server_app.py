"""Server mode's HTTP application, as far as signing in goes (#438): the status routes, the
sign-in routes, and the rules every request passes first.

Before any route:
1. a state change (`state_change_rules.py`) without the public origin, from another site, or
   without a JSON body is `ORIGIN_REFUSED`;
2. every API route but `health`, `version` and sign-in needs a live session, re-read now:
   `SESSION_EXPIRED` otherwise. A session opened with a temporary password reaches only the
   session, sign-out and password routes: `PASSWORD_CHANGE_REQUIRED` elsewhere. A provider
   session whose groups are due to be read again reaches only sign-out:
   `OIDC_REFRESH_REQUIRED`, naming the provider, for a silent re-authentication (#439).
   The OIDC routes check their own session or binding.

The semantic routes (`query`, `explain`, `catalog`, the asynchronous jobs) of the served
project, when given, run for the session's user with a context built at each request (#440),
and write every error for that user's audience. Every request passes authentication, then
origin/CSRF, then authorization by the route's declared permission (`route_permissions`), then
the validation of its content (#445); an API route without a declared permission, or a served
project without its audit log, refuses to build the application (the server does not start).
No route may accept both a method that changes state and one that does not:
the application is refused at construction if one does. The composition of server mode
(`--server`) comes later.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from orvex.access.audit.log import AuditLog
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    OIDC_REFRESH_REQUIRED,
    PASSWORD_CHANGE_REQUIRED,
    SESSION_EXPIRED,
    AccessError,
)
from orvex.access.oidc.service import OidcSignIn
from orvex.access.server_context import Audience, ServerRequest, audience_of, role_of
from orvex.access.storage.store import Store
from orvex.contracts.http_api import v1
from orvex.server import oidc_http, semantic_http, sign_in_http
from orvex.server.app import (
    REQUEST_ID_HEADER,
    error_response,
    install_error_handlers,
    mount_semantic,
    mount_status,
)
from orvex.server.route_permissions import allows, checked_permissions, permission_of
from orvex.server.sign_in_errors import access_error_response
from orvex.server.state_change_rules import (
    SAFE_METHODS,
    changes_state,
    content_refusal,
    origin_refusal,
)
from orvex.server.versions import Versions
from starlette.concurrency import run_in_threadpool

PUBLIC = frozenset({"/api/v1/health", "/api/v1/version", sign_in_http.LOGIN})
CHANGE_ONLY = frozenset({sign_in_http.SESSION, sign_in_http.LOGOUT, sign_in_http.PASSWORD})
API_PREFIX = "/api/"
log = logging.getLogger("orvex.server.http")


def create_server_app(
    versions: Versions,
    sign_in: SignIn,
    settings: Settings,
    slug: str,
    oidc: OidcSignIn | None = None,
    served: ServedProject | None = None,
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

    async def origin(request: Request) -> Response | None:
        if not changes_state(request.method):
            return None
        current = await run_in_threadpool(settings.current)
        return _origin_refused(
            origin_refusal(request.method, request.headers, current.public_origin),
            request.state.request_id,
        )

    async def rules(request: Request) -> Response | None:
        """Authentication, origin/CSRF, authorization, validation of the content (#445): a
        request refused at one step is told nothing of the later ones."""
        request_id: str = request.state.request_id
        path = request.url.path
        public = (
            not path.startswith(API_PREFIX) or path in PUBLIC or path.startswith(oidc_http.PREFIX)
        )
        if not public:
            refused = await authenticated(request)
            if refused is not None:
                return refused
        refused = await origin(request)
        if refused is not None:
            return refused
        if not public:
            refused = await authorized(request)
            if refused is not None:
                return refused
        return _origin_refused(content_refusal(request.method, request.headers), request_id)

    async def authorized(request: Request) -> Response | None:
        permission = permission_of(request.url.path)
        if permission is None or permission in {"public", "session"}:
            return None  # an unknown path: the router's own 404
        if served is None:
            return None
        user = request.state.session.user.id
        caller = ServerRequest(user, served.project)
        role = await run_in_threadpool(role_of, served.store, caller)
        if allows(permission, role):
            return None
        audit = served.semantic.audit
        if audit is not None:
            await run_in_threadpool(
                audit.refusal, "route", user, ACCESS_FORBIDDEN, project=served.project
            )
        return error_response(
            403, ACCESS_FORBIDDEN, "The operation is not allowed", request.state.request_id
        )

    async def authenticated(request: Request) -> Response | None:
        request_id: str = request.state.request_id
        path = request.url.path
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
        due = None if oidc is None else await run_in_threadpool(oidc.refresh_due, session_id)
        if due is not None and path != sign_in_http.LOGOUT:
            return access_error_response(
                AccessError(
                    OIDC_REFRESH_REQUIRED,
                    "Sign in again with the provider.",
                    {"provider": due},
                ),
                request_id,
            )
        request.state.session = live
        request.state.session_id = session_id
        return None

    install_error_handlers(app, lambda path: None)
    mount_status(app, versions)
    sign_in_http.mount_sign_in(app, sign_in, settings, cookie)
    oidc_http.mount_oidc(app, oidc, settings, cookie, slug)
    if served is not None:
        if not isinstance(served.semantic.audit, AuditLog):  # #445: no audit log, no server
            raise ValueError("server mode needs its audit log: the server does not start")
        served.semantic.audit.verify_at_start()  # a key or an anchor that fails: no server
        project = served.project

        def caller_of(request: Request) -> object:
            return ServerRequest(request.state.session.user.id, project)

        def audience(caller: object) -> Audience:
            if not isinstance(caller, ServerRequest):
                return "other"
            return audience_of(served.store, caller)

        def recheck(request: Request) -> None:
            sign_in.session(request.state.session_id)  # ended since the request: refused

        mount_semantic(
            app,
            served.semantic,
            caller_of,
            audience,
            lambda request: (request.state.session.user.id, project),
            recheck,
        )
    checked_route_table(app)
    checked_permissions(app)
    return app


def _origin_refused(found: tuple[str, str] | None, request_id: str) -> Response | None:
    if found is None:
        return None
    name, value = found
    return error_response(
        403,
        "ORIGIN_REFUSED",
        "This instance accepts changes only from its own pages",
        request_id,
        [v1.ErrorDetail(name=name, value=value)],
    )


@dataclass(frozen=True)
class ServedProject:
    """The project whose semantic routes this application serves (#440): its id, the semantic
    composition (whose context source is `semantic_http.server_context`) and the access store
    the caller's audience is read from."""

    project: str
    semantic: semantic_http.Semantic
    store: Store


def checked_route_table(app: FastAPI) -> None:
    """Refuse a route that takes both a safe method and one that changes state."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods = set(route.methods or ())
            if methods & SAFE_METHODS and methods - SAFE_METHODS:
                raise ValueError(f"route {route.path} mixes safe and state-changing methods")
