"""Session cookie policy and CSRF checks for portal forms."""
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware

from .config import PORTAL_LOCAL_URL, PORTAL_PUBLIC_URL


class PortalSessions:
    def __init__(self, app, **options):
        self.secure = SessionMiddleware(app, https_only=True, **options)
        self.local = SessionMiddleware(app, **options)
        self.public = urlsplit(PORTAL_PUBLIC_URL)
        self.lan = urlsplit(PORTAL_LOCAL_URL)

    async def __call__(self, scope, receive, send):
        host = next((value.decode("latin1") for key, value in scope.get("headers", []) if key == b"host"), "")
        local_http = self.lan.scheme == "http" and host.lower() == self.lan.netloc.lower()
        secure = scope.get("scheme") == "https" or (
            self.public.scheme == "https" and (not local_http or self.public.netloc.lower() == host.lower())
        )
        await (self.secure if secure else self.local)(scope, receive, send)


def csrf_token(request: Request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


async def verify_csrf(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None:
        supplied = (await request.form()).get("csrf_token")
    if not expected or not isinstance(supplied, str) or not secrets.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(403, "Invalid CSRF token")
