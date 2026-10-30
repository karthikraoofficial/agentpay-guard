"""Every 4xx and 5xx response is RFC 9457 problem+json with a stable `code`.

Outcomes (allow, step_up, block) are never errors; they are 200 responses. Validation
errors list where and what failed, never the submitted values, so a rejected raw
phone number or email is not echoed back or logged.
"""

import logging
import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

_TITLES = {
    400: "Bad request",
    401: "Unauthenticated",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    422: "Validation failed",
    500: "Internal error",
}


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}


def problem(request: Request, status: int, code: str, detail: str, **extra: object) -> JSONResponse:
    body = {
        "type": f"urn:agentpay-guard:problem:{code}",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "code": code,
        "detail": detail,
        "request_id": request_id(request),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def request_id(request: Request) -> str:
    rid: str = getattr(request.state, "request_id", "")
    return rid


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def assign_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = f"req_{secrets.token_urlsafe(12)}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        response = problem(request, exc.status, exc.code, exc.detail)
        response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        if any(e["type"] == "json_invalid" for e in exc.errors()):
            return problem(request, 400, "MALFORMED_REQUEST", "The body is not valid JSON.")
        return problem(
            request, 422, "VALIDATION_FAILED", "The request failed validation.", errors=errors
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return problem(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"request_id": request_id(request)})
        return problem(request, 500, "INTERNAL", "Something went wrong. Nothing was stored.")
