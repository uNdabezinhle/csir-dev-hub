"""Shared helpers for the scripts/test-*.py end-to-end checks."""
import http.cookiejar, urllib.parse

class _LocalhostSecurePolicy(http.cookiejar.DefaultCookiePolicy):
    """Keycloak marks its cookies Secure when served on http://localhost (browsers treat localhost as a secure
    context and send them). Python's cookiejar would drop them over plain http -> mimic the browser for loopback
    hosts only (Docker Desktop / laptop runs, HOST_IP=localhost)."""
    def return_ok_secure(self, cookie, request):
        if urllib.parse.urlsplit(request.get_full_url()).hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        return super().return_ok_secure(cookie, request)

def cookiejar():
    return http.cookiejar.CookieJar(policy=_LocalhostSecurePolicy())
