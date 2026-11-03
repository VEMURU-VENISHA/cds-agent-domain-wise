"""Global Error Handling Middleware and Exception Handlers."""

import traceback
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from src.core.exceptions import PlatformBaseException
from src.logging_config import logger


def register_exception_handlers(app: FastAPI) -> None:
    """Registers unified JSON exception handlers on the FastAPI application."""

    @app.exception_handler(PlatformBaseException)
    async def platform_exception_handler(request: Request, exc: PlatformBaseException):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.error(f"[{correlation_id}] Domain Exception: {exc.code} - {exc.message}")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "status": exc.status_code,
                    "correlation_id": correlation_id,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.warning(f"[{correlation_id}] Request Validation Error: {exc.errors()}")
        return JSONResponse(
            status_code=getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422),
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request payload or query parameters.",
                    "status": 422,
                    "correlation_id": correlation_id,
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.error(f"[{correlation_id}] Unhandled Exception: {str(exc)}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected error occurred. Please contact the platform administrator.",
                    "status": 500,
                    "correlation_id": correlation_id,
                    "details": {"error_type": type(exc).__name__},
                }
            },
        )
