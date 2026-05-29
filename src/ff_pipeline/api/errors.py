"""HTTP error handling for the API.

Per ``docs/06_API_CONTRACT.md`` the error body is
``{"error": "...", "detail": "...", "status": 4xx}`` — flatter than
FastAPI's default ``{"detail": "..."}`` shape, so we install a small
exception class plus a handler that produces the canonical envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ff_pipeline.api.schemas import ErrorBody

if TYPE_CHECKING:
    from fastapi import FastAPI


class ApiError(HTTPException):
    """Domain-specific HTTPException carrying a stable error code.

    ``error`` is the snake_case slug ("not_found", "bad_request"); the
    ``detail`` is a human-readable explanation. Both surface in the
    response body unchanged.
    """

    def __init__(self, *, status_code: int, error: str, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error = error


def not_found(detail: str) -> ApiError:
    return ApiError(status_code=404, error="not_found", detail=detail)


def bad_request(detail: str) -> ApiError:
    return ApiError(status_code=400, error="bad_request", detail=detail)


def service_unavailable(detail: str) -> ApiError:
    return ApiError(status_code=503, error="service_unavailable", detail=detail)


def _error_payload(error: str, detail: str, status: int) -> dict[str, object]:
    return ErrorBody(error=error, detail=detail, status=status).model_dump()


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.error, exc.detail, exc.status_code),
    )


async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    # Map well-known status codes back to the contract's error slugs so
    # FastAPI-raised 404/422 (e.g. from path parsing) still match the
    # documented envelope.
    error_map = {
        400: "bad_request",
        404: "not_found",
        422: "bad_request",
        503: "service_unavailable",
    }
    error = error_map.get(exc.status_code, "error")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(error, detail, exc.status_code),
    )


async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=_error_payload("bad_request", str(exc.errors()), 400),
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]
