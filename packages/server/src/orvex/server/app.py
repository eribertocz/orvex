"""The HTTP application: API routes of contracts/http-api/v1, errors as ErrorEnvelope, web build."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pyarrow as pa
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from orvex.access.local import LocalRequest
from orvex.contracts.http_api import v1
from orvex.semantic.errors import SemanticError
from orvex.server import semantic_http
from orvex.server.local_access import CODE_PARAMETER, LocalAccess
from orvex.server.versions import Versions
from orvex.store.errors import StoreError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

REQUEST_ID_HEADER = "X-Request-Id"
API_PREFIX = "/api/"
# Local mode answers only requests addressed to itself by one of these names (DNS rebinding).
LOCAL_NAMES = ("127.0.0.1", "localhost")
SHOWN = 200  # characters of a refused Host or Origin kept in the error details
SEMANTIC_PREFIX = "/api/v1/semantic/"
# A browser marks where a request comes from; only the server's own pages, or the user typing
# the address (`none`), may reach the semantic routes (CSRF).
FETCH_SITES = ("same-origin", "none")
JSON = "application/json"
BUSY_RETRY_SECONDS = "5"
log = logging.getLogger("orvex.server.http")


def error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    details: list[v1.ErrorDetail] | None = None,
    semantic: dict[str, object] | None = None,
) -> JSONResponse:
    body = v1.ErrorBody(
        code=code, message=message, details=details or [], request_id=request_id, semantic=semantic
    )
    envelope = v1.ErrorEnvelope(error=body)
    return JSONResponse(envelope.model_dump(mode="json", exclude_none=True), status_code=status)


def _detail(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def semantic_error_response(error: SemanticError, request_id: str) -> JSONResponse:
    """An engine error: its builder message, its details, and its whole `semantic-error` v1
    message under `semantic`."""
    message = semantic_http.semantic_message(error)
    details = [
        v1.ErrorDetail(name=str(name), value=_detail(value))
        for name, value in sorted(message["details"].items())
    ]
    return error_response(
        semantic_http.status_of(error),
        error.code,
        str(message["message"]),
        request_id,
        details,
        message,
    )


def refused_semantic(request: Request, access: LocalAccess) -> JSONResponse | None:
    """The refusal of a semantic request that is not this session's own: one a browser sent
    from another site, a body that is not JSON (no form or simple cross-site request can send
    JSON), or a request without the session's token."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.lower() not in FETCH_SITES:
        return error_response(
            403,
            "ORIGIN_NOT_ALLOWED",
            "This local server answers only its own pages",
            request_id_of(request),
            [v1.ErrorDetail(name="sec-fetch-site", value=site[:SHOWN])],
        )
    if request.method == "POST":
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != JSON:
            return error_response(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                "The request body must be application/json",
                request_id_of(request),
                [v1.ErrorDetail(name="content-type", value=media[:SHOWN])],
            )
    if not access.accepts(request.headers, request.cookies):
        return error_response(
            401,
            "UNAUTHENTICATED",
            "This local server answers only requests with its session token",
            request_id_of(request),
        )
    return None


def wants_arrow(request: Request) -> bool:
    """Whether `Accept` names the Arrow stream (with a q-value above 0)."""
    for entry in request.headers.get("accept", "").split(","):
        media, *parameters = (part.strip().lower() for part in entry.split(";"))
        if media != semantic_http.ARROW:
            continue
        refused = any(
            p.replace(" ", "") in {"q=0", "q=0.0", "q=0.00", "q=0.000"} for p in parameters
        )
        if not refused:
            return True
    return False


def arrow_response(table: pa.Table) -> Response:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return Response(sink.getvalue().to_pybytes(), media_type=semantic_http.ARROW)


def request_id_of(request: Request) -> str:
    value: str = request.state.request_id
    return value


def refused_address(request: Request, port: int) -> JSONResponse | None:
    """The refusal for a request not addressed to this local server, or None.

    `Host` must name the server (`127.0.0.1:<port>` or `localhost:<port>`), so a page whose own
    name was made to resolve to 127.0.0.1 (DNS rebinding) is refused. An `Origin`, when a browser
    sends one, must be the server's own, so another site cannot send requests either."""
    allowed = {f"{name}:{port}" for name in LOCAL_NAMES}
    host = request.headers.get("host")
    if host is None or host.lower() not in allowed:
        return error_response(
            400,
            "HOST_NOT_ALLOWED",
            "This local server answers only requests addressed to it as 127.0.0.1 or localhost",
            request_id_of(request),
            [v1.ErrorDetail(name="host", value=(host or "")[:SHOWN])],
        )
    origin = request.headers.get("origin")
    if origin is not None and origin.lower() not in {f"http://{item}" for item in allowed}:
        return error_response(
            403,
            "ORIGIN_NOT_ALLOWED",
            "This local server answers only its own pages",
            request_id_of(request),
            [v1.ErrorDetail(name="origin", value=origin[:SHOWN])],
        )
    return None


@dataclass(frozen=True)
class LocalSemantic:
    """The semantic routes of local mode: the project's id, the session's access and the
    composition. Without it, no semantic route exists (server mode until P3)."""

    project: str
    access: LocalAccess
    semantic: semantic_http.Semantic


def create_app(
    versions: Versions, web_build: Path | None, port: int, local: LocalSemantic | None = None
) -> FastAPI:
    """The application served on 127.0.0.1:`port` (local mode)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def web_page(path: str) -> Path | None:
        """The web build file for a non-API path, or the fallback page for client-side routes."""
        if path.startswith(API_PREFIX) or web_build is None:
            return None
        root = web_build.resolve()
        candidate = (root / path.lstrip("/")).resolve()
        if candidate.is_file() and candidate.is_relative_to(root):
            return candidate
        return root / "index.html"

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            # Before any route, the web build or the 404 handling.
            refused: Response | None = refused_address(request, port)
            if refused is None and local is not None:
                refused = await session_gate(request, local)
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

    async def session_gate(request: Request, local: LocalSemantic) -> Response | None:
        """The semantic routes' own refusals, and the exchange of the bootstrap code for a
        session cookie on a page request."""
        path = request.url.path
        if path.startswith(SEMANTIC_PREFIX):
            return refused_semantic(request, local.access)
        if path.startswith(API_PREFIX) or CODE_PARAMETER not in request.query_params:
            return None
        session = None
        response: Response
        if request.method == "GET":
            session = local.access.exchange(request.query_params.get(CODE_PARAMETER))
        if session is None:
            response = error_response(
                401,
                "UNAUTHENTICATED",
                "This bootstrap code is not valid: it works once, for a few minutes",
                request_id_of(request),
            )
        else:
            kept = [(k, v) for k, v in request.query_params.multi_items() if k != CODE_PARAMETER]
            # Always a path of this server: `//host/...` would name another one.
            local_path = "/" + path.lstrip("/")
            response = RedirectResponse(local_path + ("?" + urlencode(kept) if kept else ""), 303)
            response.set_cookie(
                local.access.cookie, session, httponly=True, samesite="strict", path="/"
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if error.status_code == 404 and request.method == "GET":
            page = web_page(request.url.path)
            if page is not None:
                return FileResponse(page)
        if error.status_code == 404:
            return error_response(
                404,
                "NOT_FOUND",
                f"No route for {request.url.path}",
                request_id_of(request),
                [v1.ErrorDetail(name="path", value=request.url.path)],
            )
        if error.status_code == 405:
            response = error_response(
                405,
                "METHOD_NOT_ALLOWED",
                f"{request.method} is not allowed on {request.url.path}",
                request_id_of(request),
                [
                    v1.ErrorDetail(name="method", value=request.method),
                    v1.ErrorDetail(name="path", value=request.url.path),
                ],
            )
            allowed = (error.headers or {}).get("Allow")
            if allowed is not None:  # RFC 9110 §15.5.6: a 405 lists the accepted methods
                response.headers["Allow"] = allowed
            return response
        return error_response(
            error.status_code, "INTERNAL_ERROR", str(error.detail), request_id_of(request)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            v1.ErrorDetail(
                name=".".join(str(part) for part in problem.get("loc", ())) or "request",
                value=str(problem.get("msg", "invalid")),
            )
            for problem in error.errors()
        ]
        return error_response(
            422,
            "VALIDATION_FAILED",
            "The request does not match the contract",
            request_id_of(request),
            details,
        )

    @app.get("/api/v1/health")
    async def health() -> v1.Health:
        return v1.Health(status="ok")

    @app.get("/api/v1/version")
    async def version() -> v1.Version:
        return v1.Version(
            product=v1.SemanticVersion(versions.product),
            modules=[
                v1.ModuleVersion(name=name, version=v1.SemanticVersion(number))
                for name, number in versions.modules
            ],
            contracts=[
                v1.ContractVersion(name=name, version=number) for name, number in versions.contracts
            ],
        )

    if local is not None:
        mount_semantic(app, local)
    return app


def mount_semantic(app: FastAPI, local: LocalSemantic) -> None:
    """`query`, `explain` and `catalog` (`contracts/http-api/v1`). The session gate has already
    checked the request's site, body type and token: what remains is the caller's own
    permissions, which `access` gives and the engine applies."""
    caller = LocalRequest(local.project, authenticated=True)

    def refusal(error: Exception, request_id: str) -> Response:
        if isinstance(error, SemanticError):
            return semantic_error_response(error, request_id)
        if isinstance(error, semantic_http.Refused):
            details = [v1.ErrorDetail(name=n, value=v) for n, v in error.details]
            return error_response(error.status, error.code, error.message, request_id, details)
        if isinstance(error, semantic_http.ProjectBusy):
            response = error_response(
                503, "PROJECT_BUSY", "A run holds the project's lake; try again", request_id
            )
            response.headers["Retry-After"] = BUSY_RETRY_SECONDS
            return response
        raise error

    @app.post("/api/v1/semantic/query")
    async def query(request: Request) -> Response:
        body = await request.body()
        request_id = request_id_of(request)
        try:
            asked = await run_in_threadpool(semantic_http.ask, local.semantic, body, caller)
            if wants_arrow(request):
                return arrow_response(asked.arrow_table())
            return JSONResponse(asked.json_document())
        except (SemanticError, semantic_http.Refused, semantic_http.ProjectBusy) as error:
            return refusal(error, request_id)
        except StoreError as error:
            log.error("store error", extra={"request_id": request_id, "code": error.code})
            return error_response(500, "INTERNAL_ERROR", "Unexpected server error", request_id)

    @app.post("/api/v1/semantic/explain")
    async def explain(request: Request) -> Response:
        body = await request.body()
        request_id = request_id_of(request)
        try:
            document = await run_in_threadpool(
                semantic_http.explanation, local.semantic, body, caller
            )
            return JSONResponse(document)
        except (SemanticError, semantic_http.Refused, semantic_http.ProjectBusy) as error:
            return refusal(error, request_id)
        except StoreError as error:
            log.error("store error", extra={"request_id": request_id, "code": error.code})
            return error_response(500, "INTERNAL_ERROR", "Unexpected server error", request_id)

    @app.get("/api/v1/semantic/catalog")
    async def catalog(request: Request) -> Response:
        request_id = request_id_of(request)
        try:
            document = await run_in_threadpool(semantic_http.catalog, local.semantic, caller)
            return JSONResponse(document)
        except (SemanticError, semantic_http.Refused, semantic_http.ProjectBusy) as error:
            return refusal(error, request_id)
        except StoreError as error:
            log.error("store error", extra={"request_id": request_id, "code": error.code})
            return error_response(500, "INTERNAL_ERROR", "Unexpected server error", request_id)
