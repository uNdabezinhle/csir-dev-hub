"""External Keycloak identities with project-only sessions."""
import base64
import hashlib
import os
import re
import secrets
import time
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import audit, db, sdlc_store as store
from .config import PORTAL_PUBLIC_URL

INVITE_SECONDS = 7 * 86400
MEMBERSHIP_SECONDS = 30 * 86400


def settings():
    issuer = os.getenv("SDLC_EXTERNAL_ISSUER", "").rstrip("/")
    client = os.getenv("SDLC_EXTERNAL_CLIENT_ID", "")
    secret = os.getenv("SDLC_EXTERNAL_CLIENT_SECRET", "")
    acr = os.getenv("SDLC_EXTERNAL_MFA_ACR", "2")
    callback = f"{PORTAL_PUBLIC_URL.rstrip('/')}/delivery/external/callback"
    internal_issuer = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
    if (urlsplit(issuer).scheme != "https" or issuer == internal_issuer
            or urlsplit(callback).scheme != "https" or not client or not secret or not acr):
        return None
    return issuer, client, secret, acr, callback


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=4)
def jwks(url):
    return jwt.PyJWKClient(url, timeout=5)


def accept_identity(claims, issuer, required_acr, invite_hash):
    subject, email = claims.get("sub"), claims.get("email")
    if (not isinstance(subject, str) or not subject or not isinstance(email, str) or not email
            or len(email) > 254 or claims.get("iss") != issuer
            or claims.get("email_verified") is not True or claims.get("acr") != required_acr):
        raise HTTPException(403, "A verified email and the configured MFA assurance are required")
    email = email.strip().casefold()
    principal = digest(issuer + "\x00" + subject)
    if not invite_hash:
        return principal
    now = time.time()
    with db.tx(immediate=True) as conn:
        invite = db.one(
            """SELECT * FROM sdlc_invitations WHERE token_hash=? AND redeemed_at IS NULL
               AND revoked_at IS NULL AND expires_at>?""", (invite_hash, now),
        )
        if not invite or not store.project_enabled(invite["project_id"]) or invite["email"] != email:
            raise HTTPException(403, "Invitation is unavailable for this identity")
        conn.execute(
            """INSERT INTO sdlc_external VALUES (?,?,?,?,?,NULL)
               ON CONFLICT(project_id,subject) DO UPDATE SET email=excluded.email, role=excluded.role,
               expires_at=excluded.expires_at, revoked_at=NULL""",
            (invite["project_id"], principal, email, invite["role"], now + MEMBERSHIP_SECONDS),
        )
        conn.execute("UPDATE sdlc_invitations SET redeemed_at=? WHERE token_hash=?", (now, invite_hash))
    return principal


def register(app, render, flash, ip):
    @app.get("/delivery/external")
    def external_login(request: Request):
        store.require_enabled()
        response = render(request, "sdlc_external.html", None, configured=bool(settings()))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/delivery/external")
    def external_start(request: Request, code: str = Form("")):
        store.require_enabled()
        config = settings()
        if not config:
            raise HTTPException(503, "External invitations are not configured")
        issuer, client, _, acr, callback = config
        code = code.strip()
        if code and not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
            raise HTTPException(422, "Enter a valid invitation code")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        request.session["external_sdlc_oidc"] = {
            "state": state, "nonce": nonce, "verifier": verifier, "expiry": time.time() + 600,
            "invite": digest(code) if code else None,
        }
        url = issuer + "/protocol/openid-connect/auth?" + urlencode({
            "client_id": client, "response_type": "code", "scope": "openid email", "redirect_uri": callback,
            "state": state, "nonce": nonce, "acr_values": acr, "prompt": "login",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return RedirectResponse(url, 303)

    @app.get("/delivery/external/callback")
    def external_finish(request: Request, code: str = "", state: str = ""):
        store.require_enabled()
        config = settings()
        saved = request.session.pop("external_sdlc_oidc", None)
        if not config or not saved or not code or len(code) > 2048 or saved["expiry"] <= time.time() or state != saved["state"]:
            raise HTTPException(403, "External sign-in state is invalid or expired")
        issuer, client, secret, acr, callback = config
        try:
            response = httpx.post(issuer + "/protocol/openid-connect/token", timeout=10, data={
                "grant_type": "authorization_code", "code": code, "client_id": client,
                "client_secret": secret, "redirect_uri": callback, "code_verifier": saved["verifier"],
            })
            response.raise_for_status()
            token = response.json()["id_token"]
            key = jwks(issuer + "/protocol/openid-connect/certs").get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, audience=client,
                                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(403, "External identity could not be verified") from error
        if claims["nonce"] != saved["nonce"] or claims.get("azp", client) != client:
            raise HTTPException(403, "External identity could not be verified")
        subject = accept_identity(claims, issuer, acr, saved["invite"])
        if not db.one("SELECT 1 FROM sdlc_external WHERE subject=? AND revoked_at IS NULL AND expires_at>?", (subject, time.time())):
            raise HTTPException(403, "No active project invitation")
        request.session.clear()
        request.session["external_sdlc"] = subject
        audit.log("external:" + subject, "sdlc.external.sign-in", "", ip=ip(request))
        return RedirectResponse("/delivery/external/projects", 303)

    @app.get("/delivery/external/projects")
    def external_projects(request: Request):
        store.require_enabled()
        subject = request.session.get("external_sdlc")
        if not subject:
            return RedirectResponse("/delivery/external", 303)
        projects = db.q(
            """SELECT p.id,p.name,e.role,e.expires_at FROM sdlc_external e JOIN projects p ON p.id=e.project_id
               WHERE e.subject=? AND e.revoked_at IS NULL AND e.expires_at>? ORDER BY p.name""", (subject, time.time()),
        )
        projects = [project for project in projects if store.project_enabled(project["id"])]
        response = render(request, "sdlc_external.html", None, projects=projects, external_session=True)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/delivery/external/sign-out")
    def external_sign_out(request: Request):
        store.require_enabled()
        request.session.clear()
        return RedirectResponse("/delivery/external", 303)

    @app.post("/delivery/{pid}/invitations")
    def invite(request: Request, pid: int, email: str = Form(...), role: str = Form(...)):
        _, user, current = store.project_access(request, pid, need_internal=True)
        store.require_role(current, "lead")
        if not settings():
            raise HTTPException(503, "External invitations are not configured")
        email = email.strip().casefold()
        if role not in store.EXTERNAL_ROLES or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HTTPException(422, "Choose an external role and enter a valid email")
        code = secrets.token_urlsafe(32)
        now = time.time()
        with db.tx(immediate=True) as conn:
            conn.execute("UPDATE sdlc_invitations SET revoked_at=? WHERE project_id=? AND email=? AND redeemed_at IS NULL AND revoked_at IS NULL",
                         (now, pid, email))
            conn.execute("INSERT INTO sdlc_invitations VALUES (?,?,?,?,?,NULL,NULL)",
                         (digest(code), pid, email, role, now + INVITE_SECONDS))
        audit.log(user["username"], "sdlc.invite.create", str(pid), {"email": email, "role": role}, ip(request))
        response = render(request, "sdlc_external.html", user, created_code=code, project_id=pid)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/delivery/{pid}/external/{subject}")
    def maintain_external(request: Request, pid: int, subject: str, action: str = Form(...)):
        _, user, role = store.project_access(request, pid, need_internal=True)
        store.require_role(role, "lead")
        if action not in {"revoke", "renew"}:
            raise HTTPException(422, "Choose an external access action")
        now = time.time()
        with db.tx(immediate=True) as conn:
            membership = db.one("SELECT * FROM sdlc_external WHERE project_id=? AND subject=?", (pid, subject))
            if not membership or (action == "renew" and membership["revoked_at"] is not None):
                raise HTTPException(404)
            if action == "revoke":
                conn.execute("UPDATE sdlc_external SET revoked_at=? WHERE project_id=? AND subject=?", (now, pid, subject))
                conn.execute("UPDATE sdlc_invitations SET revoked_at=? WHERE project_id=? AND email=? AND redeemed_at IS NULL",
                             (now, pid, membership["email"]))
            else:
                conn.execute("UPDATE sdlc_external SET expires_at=? WHERE project_id=? AND subject=?", (now + MEMBERSHIP_SECONDS, pid, subject))
        audit.log(user["username"], "sdlc.external." + action, str(pid), {"subject": subject}, ip=ip(request))
        flash(request, "External access updated")
        return RedirectResponse(f"/delivery/{pid}?tab=people", 303)

    @app.post("/delivery/{pid}/invitations/{token_hash}/revoke")
    def revoke_invite(request: Request, pid: int, token_hash: str):
        _, user, role = store.project_access(request, pid, need_internal=True)
        store.require_role(role, "lead")
        with db.tx(immediate=True) as conn:
            result = conn.execute(
                """UPDATE sdlc_invitations SET revoked_at=? WHERE project_id=? AND token_hash=?
                   AND redeemed_at IS NULL AND revoked_at IS NULL""", (time.time(), pid, token_hash),
            )
            if not result.rowcount:
                raise HTTPException(404)
        audit.log(user["username"], "sdlc.invite.revoke", str(pid), ip=ip(request))
        flash(request, "Invitation revoked")
        return RedirectResponse(f"/delivery/{pid}?tab=people", 303)
