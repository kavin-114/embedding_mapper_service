"""Request context middleware for structured logging."""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds request_id, tenant_id, erp_system to structlog context.

    Also logs request start/end with latency and adds X-Request-ID header.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        tenant_id = request.headers.get("x-tenant-id", "")
        erp_system = request.headers.get("x-erp-system", "")

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            tenant_id=tenant_id,
            erp_system=erp_system,
        )

        logger = structlog.get_logger("middleware")
        await logger.ainfo(
            "request.start",
            method=request.method,
            path=request.url.path,
        )

        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start) * 1000

        await logger.ainfo(
            "request.end",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=round(latency_ms, 2),
        )

        response.headers["X-Request-ID"] = request_id
        return response
