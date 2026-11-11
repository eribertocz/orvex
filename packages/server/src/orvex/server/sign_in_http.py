"""Server mode's sign-in routes (#438): sign in, the session, sign out, change the password,
and the session cookie they set.

The cookie is `__Host-<slug>_session`: `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no
`Domain`, for at most the session's absolute lifetime. Its value is the session id, which is
written nowhere else: not in a body, a log or an error.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.sessions import Session
from orvex.access.authentication.settings import Settings
from orvex.access.directory.records import User
from orvex.access.errors import AccessError
from orvex.contracts.http_api import v1
from orvex.server.client_address import client_address
from orvex.server.sign_in_errors import access_error_response, validation_response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

LOGIN = "/api/v1/auth/login"
SESSION = "/api/v1/auth/session"
LOGOUT = "/api/v1/auth/logout"
PASSWORD = "/api/v1/auth/password"


def cookie_name(slug: str) -> str:
    return f"__Host-{slug}_session"


def session_view(user: User, password_change_only: bool) -> v1.SessionView:
    return v1.SessionView(
        user=v1.User(
            id=user.id,
            user_name=user.user_name,
            display_name=user.display_name,
            administrator=user.administrator,
        ),
        password_change_required=password_change_only,
    )


def mount_sign_in(app: FastAPI, sign_in: SignIn, settings: Settings, cookie: str) -> None:
    """The routes above. The middleware has already checked a state change's origin, and that a
    session route has a live session (`request.state.session`)."""

    def set_session(response: Response, session_id: str, lifetime: timedelta) -> None:
        response.set_cookie(
            cookie,
            session_id,
            max_age=int(lifetime.total_seconds()),
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )

    @app.post(LOGIN)
    async def login(request: Request) -> Response:
        request_id: str = request.state.request_id
        try:
            asked = v1.SignIn.model_validate_json(await request.body())
        except ValidationError as error:
            return validation_response(error, request_id)
        current = await run_in_threadpool(settings.current)
        peer = request.client.host if request.client is not None else None
        address = client_address(
            peer, request.headers.getlist("x-forwarded-for"), current.trusted_proxies
        )
        try:
            signed = await run_in_threadpool(
                sign_in.login,
                asked.user_name,
                asked.password,
                address,
                request.cookies.get(cookie),
            )
        except AccessError as error:
            return access_error_response(error, request_id)
        response = JSONResponse(
            session_view(signed.user, signed.password_change_only).model_dump(mode="json")
        )
        set_session(response, signed.session_id, current.absolute)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(SESSION)
    async def session(request: Request) -> Response:
        live: Session = request.state.session
        view = session_view(live.user, live.password_change_only)
        response = JSONResponse(view.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(LOGOUT)
    async def logout(request: Request) -> Response:
        request_id: str = request.state.request_id
        body = await request.body()
        if body.strip() != b"{}":
            return validation_response(None, request_id)
        try:
            await run_in_threadpool(sign_in.logout, request.state.session_id)
        except AccessError as error:
            return access_error_response(error, request_id)
        response = Response(status_code=204)
        response.delete_cookie(cookie, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.post(PASSWORD)
    async def password(request: Request) -> Response:
        request_id: str = request.state.request_id
        try:
            asked = v1.PasswordChange.model_validate_json(await request.body())
        except ValidationError as error:
            return validation_response(error, request_id)
        current = await run_in_threadpool(settings.current)
        peer = request.client.host if request.client is not None else None
        address = client_address(
            peer, request.headers.getlist("x-forwarded-for"), current.trusted_proxies
        )
        try:
            renewed = await run_in_threadpool(
                sign_in.change_password,
                request.state.session_id,
                asked.current_password,
                asked.new_password,
                address,
            )
        except AccessError as error:
            return access_error_response(error, request_id)
        response = Response(status_code=204)
        set_session(response, renewed, current.absolute)
        return response
