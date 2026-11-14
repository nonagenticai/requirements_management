"""Security headers middleware for FastAPI.

Adds security headers (X-Content-Type-Options, X-Frame-Options, X-XSS-Protection) to API responses.
Skips Swagger UI endpoints to prevent interference with documentation rendering.
"""

from fastapi import FastAPI, Request


def add_security_headers_middleware(app: FastAPI) -> None:
    """Register security headers middleware on FastAPI app."""

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Add security headers to HTTP responses.

        Applies standard security headers to all API endpoints while excluding
        Swagger UI paths (/docs, /openapi.json). Also ensures proper Content-Type
        headers with UTF-8 charset for JSON responses from specific entity endpoints.

        Args:
            request: The incoming HTTP request object.
            call_next: The next middleware or route handler in the chain.

        Returns:
            HTTP response with added security headers:
            - X-Content-Type-Options: nosniff (prevents MIME sniffing)
            - X-Frame-Options: SAMEORIGIN (prevents clickjacking)
            - X-XSS-Protection: 1; mode=block (enables XSS filtering)
            - Content-Type: application/json; charset=utf-8 (for entity endpoints)
        """
        response = await call_next(request)

        if not (
            request.url.path in ["/docs", "/openapi.json"] or request.url.path.startswith("/docs/")
        ):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["X-XSS-Protection"] = "1; mode=block"

        if (
            request.url.path.startswith("/principles")
            or request.url.path.startswith("/requirements")
            or request.url.path.startswith("/decisions")
        ):
            if "application/json" in response.headers.get("content-type", ""):
                response.headers["Content-Type"] = "application/json; charset=utf-8"

        return response
