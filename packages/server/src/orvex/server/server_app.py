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
The administration routes (#446), when given, are checked against the projects the instance
serves (`PROJECT_NOT_SERVED`, before any directory read) and by the directory's own read rules.
No route may accept both a method that changes state and one that does not:
the application is refused at construction if one does. The composition of server mode
(`--server`) comes later.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from orvex.contracts.access.client import AccessClient, signed_in, unauthenticated
from orvex.contracts.access.errors import (
    ACCESS_FORBIDDEN,
    OIDC_REFRESH_REQUIRED,
    PASSWORD_CHANGE_REQUIRED,
    PROJECT_NOT_SERVED,
    SESSION_EXPIRED,
    AccessError,
    not_found,
)
from orvex.contracts.access.values import Audience, ServerRequest
from orvex.contracts.http_api import v1
from orvex.server import oidc_http, semantic_http, sign_in_http
from orvex.server.admin_http import Administration, mount_administration
from orvex.server.app import (
    error_response,
    install_error_handlers,
    mount_semantic,
    mount_status,
)
from orvex.server.call_context import REQUEST_ID_HEADER, request_correlation_id
from orvex.server.route_permissions import (
    ADMINISTRATION,
    allows,
    checked_permissions,
    permission_of,
    project_of,
)
from orvex.server.semantic_http import local_identity, local_limits
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
    access: AccessClient,
    slug: str,
    served: ServedProject | None = None,
    administration: Administration | None = None,
    run_jobs: Callable[[Callable[[], None]], None] | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def instance(request: Request) -> AccessClient:
        """The access instance, for the request's caller before authentication: anonymous."""
        return access.acting_for(unauthenticated(request.state.request_id))

    if administration is not None:
        if served is not None and served.project not in administration.projects:
            raise ValueError("the served project is not one of the instance's projects")
        administration.access.verify_at_start()  # a key or an anchor that fails: no server
    cookie = sign_in_http.cookie_name(slug)

    @app.middleware("http")
    async def request_rules(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = request_correlation_id(request.headers)
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
        current = await run_in_threadpool(instance(request).sign_in_settings)
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
        request_id: str = request.state.request_id
        user = request.state.session.user.id
        if permission in ADMINISTRATION:
            if administration is None:
                return None  # not mounted: the router's own 404
            project = project_of(request.url.path)
            if project is not None and project not in administration.projects:
                return access_error_response(not_found(PROJECT_NOT_SERVED), request_id)
            asking = administration.access.acting_for(signed_in(user, request_id))
            if await run_in_threadpool(asking.allowed, permission, project):
                return None
            await run_in_threadpool(
                lambda: asking.refusal("route", ACCESS_FORBIDDEN, project=project)
            )
            return error_response(
                403, ACCESS_FORBIDDEN, "The operation is not allowed.", request_id
            )
        if served is None:
            return None
        asking = access.acting_for(signed_in(user, request_id))
        role = await run_in_threadpool(asking.role_of, user, served.project)
        if allows(permission, role):
            return None
        await run_in_threadpool(
            lambda: asking.refusal("route", ACCESS_FORBIDDEN, project=served.project)
        )
        return error_response(403, ACCESS_FORBIDDEN, "The operation is not allowed.", request_id)

    async def authenticated(request: Request) -> Response | None:
        request_id: str = request.state.request_id
        path = request.url.path
        session_id = request.cookies.get(cookie)
        if session_id is None:
            return access_error_response(
                AccessError(SESSION_EXPIRED, "The session has ended: sign in again."), request_id
            )
        try:
            live = await run_in_threadpool(instance(request).session, session_id)
        except AccessError as error:
            return access_error_response(error, request_id)
        if live.password_change_only and path not in CHANGE_ONLY:
            return access_error_response(
                AccessError(PASSWORD_CHANGE_REQUIRED, "Change the temporary password first."),
                request_id,
            )
        due = await run_in_threadpool(instance(request).oidc_refresh_due, session_id)
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
    sign_in_http.mount_sign_in(app, access, cookie)
    oidc_http.mount_oidc(app, access, cookie, slug)
    if administration is not None:
        mount_administration(app, administration)
    if served is not None:
        if not isinstance(served.semantic.audit, AccessClient):  # #445: no audit log, no server
            raise ValueError("server mode needs its audit log: the server does not start")
        if served.semantic.identity is local_identity or served.semantic.limits is local_limits:
            raise ValueError("server mode needs its own identity and limits: no server")
        served.semantic.audit.verify_at_start()  # a key or an anchor that fails: no server
        project = served.project

        def caller_of(request: Request) -> object:
            return ServerRequest(request.state.session.user.id, project)

        def audience(caller: object) -> Audience:
            if not isinstance(caller, ServerRequest):
                return "other"
            return access.audience_of(caller.user, caller.project)

        def recheck(request: Request) -> None:
            instance(request).session(request.state.session_id)  # ended since: refused

        mount_semantic(
            app,
            served.semantic,
            caller_of,
            audience,
            lambda request: (request.state.session.user.id, project),
            recheck,
            run_jobs,
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
    """The project whose semantic routes this application serves (#440): its id and the semantic
    composition (whose context source is `semantic_http.server_context`, on the same access
    instance as the application)."""

    project: str
    semantic: semantic_http.Semantic


def checked_route_table(app: FastAPI) -> None:
    """Refuse a route that takes both a safe method and one that changes state."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods = set(route.methods or ())
            if methods & SAFE_METHODS and methods - SAFE_METHODS:
                raise ValueError(f"route {route.path} mixes safe and state-changing methods")
