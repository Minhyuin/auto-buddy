"""Small, dependency-free security headers for every portal response."""

from __future__ import annotations

import logging

CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'"
)


class SecurityHeadersMiddleware:
    """Add defensive headers to HTML/API responses without overriding Django's.

    The application has no inline scripts or styles, so a strict same-origin CSP
    is sufficient.  ``setdefault``/membership checks leave any header chosen by
    a reverse proxy or Django's SecurityMiddleware intact.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code >= 500:
            # Do not attach request, exception, path, form data or traceback.
            logging.getLogger('portal.errors').error('Portal returned HTTP %s; request details suppressed', response.status_code)
        headers = response.headers

        if "Content-Security-Policy" not in headers:
            headers["Content-Security-Policy"] = CSP
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")

        content_type = headers.get("Content-Type", "")
        if content_type.startswith("text/html"):
            # Pages contain account metadata and one-time CSRF tokens.  Do not
            # let a shared browser cache retain them after logout.
            headers["Cache-Control"] = "no-store"
            headers.setdefault("Pragma", "no-cache")
            headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")

        return response
