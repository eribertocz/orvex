"""Server mode's OIDC routes (#439): `/api/v1/auth/oidc/{provider}/start` and `/callback`.

- **Start** sends the browser to the provider. The binding's reference goes in the cookie
  `__Secure-<slug>_oidc`: `HttpOnly`, `Secure`, `SameSite=Lax` (the callback is a navigation from
  the provider's site), path limited to `/api/v1/auth/oidc`, for at most 10 minutes.
  `?refresh=true` makes it the silent re-authentication of the caller's live session
  (`prompt=none`).
- **Callback** ends in the session cookie (#438). The answer is a small page that then opens `/`,
  so the next navigation is a same-site one that carries the `SameSite=Strict` session cookie.
  Every failure is one generic `OIDC_FAILED` page; which step failed is in the audit log only.

Both are `GET`, as a browser navigation must be: they change no application state, only the
sign-in's own bookkeeping, and every check is the callback's.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from orvex.access.authentication.settings import Settings
from orvex.access.errors import OIDC_FAILED, OIDC_NOT_CONFIGURED, AccessError
from orvex.access.oidc.bindings import LIFETIME
from orvex.access.oidc.service import OidcSignIn
from orvex.server.sign_in_errors import access_error_response
from orvex.server.sign_in_http import set_session_cookie
from starlette.concurrency import run_in_threadpool

PREFIX = "/api/v1/auth/oidc/"
_PATH = "/api/v1/auth/oidc"
_SIGNED_IN = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<meta http-equiv="refresh" content="0;url=/"><title>Signed in</title></head>'
    '<body><a href="/">Continue</a></body></html>'
)
_FAILED = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sign-in failed</title></head>'
    "<body><h1>Sign-in failed</h1><p>OIDC_FAILED</p>"
    '<p><a href="/">Back</a></p></body></html>'
)
_PARAMETERS = ("code", "state", "error", "iss")


def binding_cookie(slug: str) -> str:
    return f"__Secure-{slug}_oidc"


def mount_oidc(
    app: FastAPI, oidc: OidcSignIn | None, settings: Settings, session_cookie: str, slug: str
) -> None:
    binding = binding_cookie(slug)

    def not_configured(request: Request) -> Response:
        return access_error_response(
            AccessError(OIDC_NOT_CONFIGURED, "No such sign-in provider."),
            request.state.request_id,
        )

    def failed_page() -> Response:
        response = HTMLResponse(_FAILED, status_code=400)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(binding, path=_PATH, secure=True, httponly=True, samesite="lax")
        return response

    @app.get(PREFIX + "{provider}/start")
    async def start(provider: str, request: Request) -> Response:
        if oidc is None or provider not in oidc.providers:
            return not_configured(request)
        refreshing: str | None = None
        if request.query_params.get("refresh") == "true":
            held = request.cookies.get(session_cookie)
            due = None if held is None else await run_in_threadpool(oidc.refresh_due, held)
            if held is None or due != provider:
                return failed_page()
            refreshing = held
        try:
            started = await run_in_threadpool(oidc.start, provider, refreshing=refreshing)
        except AccessError as error:
            if error.code == OIDC_NOT_CONFIGURED:
                return not_configured(request)
            if error.code != OIDC_FAILED:
                return access_error_response(error, request.state.request_id)
            return failed_page()
        response = RedirectResponse(started.location, status_code=303)
        response.set_cookie(
            binding,
            started.reference,
            max_age=int(LIFETIME.total_seconds()),
            path=_PATH,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(PREFIX + "{provider}/callback")
    async def callback(provider: str, request: Request) -> Response:
        if oidc is None or provider not in oidc.providers:
            return not_configured(request)
        parameters = {
            name: value
            for name in _PARAMETERS
            if (value := request.query_params.get(name)) is not None
        }
        try:
            signed = await run_in_threadpool(
                oidc.callback,
                provider,
                parameters,
                request.cookies.get(binding),
                request.cookies.get(session_cookie),
            )
            current = await run_in_threadpool(settings.current)
        except AccessError as error:
            if error.code == OIDC_NOT_CONFIGURED:
                return not_configured(request)
            if error.code != OIDC_FAILED:
                return access_error_response(error, request.state.request_id)
            return failed_page()
        response = HTMLResponse(_SIGNED_IN)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(binding, path=_PATH, secure=True, httponly=True, samesite="lax")
        set_session_cookie(response, session_cookie, signed.session_id, current.absolute)
        return response
