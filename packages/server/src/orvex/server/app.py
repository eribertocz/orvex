"""The HTTP application: API routes of contracts/http-api/v1, errors as ErrorEnvelope, web build."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from orvex.contracts.http_api import v1
from orvex.server.versions import Versions
from starlette.exceptions import HTTPException

REQUEST_ID_HEADER = "X-Request-Id"
API_PREFIX = "/api/"
log = logging.getLogger("orvex.server.http")


def error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    details: list[v1.ErrorDetail] | None = None,
) -> JSONResponse:
    envelope = v1.ErrorEnvelope(
        error=v1.ErrorBody(code=code, message=message, details=details or [], request_id=request_id)
    )
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status)


def request_id_of(request: Request) -> str:
    value: str = request.state.request_id
    return value


def create_app(versions: Versions, web_build: Path | None) -> FastAPI:
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
            response = await call_next(request)
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

    return app
