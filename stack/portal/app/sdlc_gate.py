"""Fail-closed GitHub Actions staging permits with atomic single-use consumption."""
import hashlib
import os
import re
import secrets
import time
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

import jwt
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import audit, db, sdlc, sdlc_store as store

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "csir-devhub-staging"
WORKFLOW = ".github/workflows/deploy-staging.yml"
PERMIT_SECONDS = 120


def settings():
    repository = os.getenv("SDLC_GITHUB_REPOSITORY", "")
    repository_id = os.getenv("SDLC_GITHUB_REPOSITORY_ID", "")
    project = os.getenv("SDLC_STAGING_PROJECT_ID", "")
    image = os.getenv("SDLC_STAGING_IMAGE", "")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not repository_id.isdecimal() or not project.isdecimal()
            or image.lower() != f"ghcr.io/{repository.lower()}/portal"):
        raise HTTPException(503, "Staging gate is unconfigured")
    if not store.project_enabled(int(project)):
        raise HTTPException(503, "Staging project is outside the configured pilot")
    return repository, repository_id, int(project), image


@lru_cache(maxsize=1)
def github_keys():
    return jwt.PyJWKClient(ISSUER + "/.well-known/jwks", timeout=5)


def validate_claims(claims, repository, repository_id):
    expected = {
        "repository": repository, "repository_id": repository_id, "ref": "refs/heads/main",
        "environment": "staging", "runner_environment": "self-hosted", "event_name": "workflow_dispatch",
        "sub": f"repo:{repository}:environment:staging",
        "workflow_ref": f"{repository}/{WORKFLOW}@refs/heads/main",
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise HTTPException(403, "Workflow is not authorised for staging")
    if any(not isinstance(claims.get(key), str) or not claims[key].isdecimal() for key in ("run_id", "run_attempt")):
        raise HTTPException(403, "GitHub run identity is missing")
    if not isinstance(claims.get("sha"), str) or not re.fullmatch(r"[a-f0-9]{40}", claims["sha"]):
        raise HTTPException(403, "Workflow commit is missing")
    return claims


def authorise(request, repository, repository_id):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) > 20000:
        raise HTTPException(401, "GitHub OIDC token required")
    token = header[7:]
    try:
        key = github_keys().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER, audience=AUDIENCE,
                            options={"require": ["iss", "aud", "sub", "exp", "iat", "nbf"]})
    except (jwt.PyJWTError, ValueError, OSError) as error:
        raise HTTPException(401, "GitHub OIDC token could not be verified") from error
    return validate_claims(claims, repository, repository_id)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def require_unused_attempt(claims):
    if db.one("SELECT 1 FROM sdlc_permits WHERE run_id=? AND attempt=? AND consumed_at IS NOT NULL",
              (claims["run_id"], claims["run_attempt"])):
        raise HTTPException(409, "This workflow attempt already consumed its permit")


def eligible(release, claims, repository, now, rollback=False):
    if rollback and not db.one("SELECT 1 FROM sdlc_permits WHERE release_id=? AND outcome='succeeded'", (release["id"],)):
        raise HTTPException(409, "Rollback requires a previously successful staging release")
    if not rollback and release["commit_sha"] != claims["sha"]:
        raise HTTPException(409, "Release commit does not match the workflow commit")
    run_path = urlsplit(release["ci_url"]).path.rstrip("/")
    if not re.fullmatch(rf"/{re.escape(repository)}/actions/runs/[0-9]+", run_path, re.I):
        raise HTTPException(409, "CI evidence is outside the configured repository")
    blockers = sdlc.release_blockers(release, now)
    if blockers:
        raise HTTPException(409, "; ".join(blockers))


class Candidate(BaseModel):
    release_id: int = Field(gt=0)
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    rollback: bool = False


class Permit(Candidate):
    permit: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class Outcome(Permit):
    outcome: Literal["succeeded", "failed"]


def app():
    api = FastAPI(openapi_url=None)

    @api.middleware("http")
    async def audit_denials(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if request.method == "POST" and response.status_code >= 400:
            audit.log("github-actions", "sdlc.staging.denied", request.url.path,
                      {"status": response.status_code}, outcome="denied")
        return response

    def context(request, payload):
        repository, repository_id, project, image = settings()
        claims = authorise(request, repository, repository_id)
        release = sdlc.row("sdlc_releases", project, payload.release_id)
        if payload.digest != release["digest"]:
            raise HTTPException(409, "Artifact digest does not match the approved candidate")
        return repository, image, release, claims

    @api.post("/permit")
    def issue(request: Request, payload: Candidate):
        repo, image, release, claims = context(request, payload)
        now = time.time()
        permit = secrets.token_urlsafe(32)
        with db.tx(immediate=True) as conn:
            eligible(release, claims, repo, now, payload.rollback)
            require_unused_attempt(claims)
            conn.execute("INSERT INTO sdlc_permits VALUES (?,?,?,?,?,NULL,NULL,?)",
                         (token_hash(permit), payload.release_id, claims["run_id"], claims["run_attempt"],
                          now + PERMIT_SECONDS, int(payload.rollback)))
        audit.log("github-actions", "sdlc.staging.permit", str(payload.release_id),
                  {"run": claims["run_id"], "rollback": payload.rollback})
        return {"permit": permit, "image": f"{image}@{release['digest']}", "commit": release["commit_sha"],
                "expires_in": PERMIT_SECONDS}

    @api.post("/consume")
    def consume(request: Request, payload: Permit):
        repo, image, release, claims = context(request, payload)
        now = time.time()
        with db.tx(immediate=True) as conn:
            eligible(release, claims, repo, now, payload.rollback)
            require_unused_attempt(claims)
            updated = conn.execute(
                """UPDATE sdlc_permits SET consumed_at=? WHERE token_hash=? AND release_id=? AND run_id=?
                   AND attempt=? AND consumed_at IS NULL AND expires_at>? AND rollback=?""",
                (now, token_hash(payload.permit), payload.release_id, claims["run_id"], claims["run_attempt"], now, int(payload.rollback)),
            )
            if not updated.rowcount:
                raise HTTPException(409, "Permit is invalid, expired or already used")
        audit.log("github-actions", "sdlc.staging.consume", str(payload.release_id), {"run": claims["run_id"]})
        return {"image": f"{image}@{release['digest']}", "commit": release["commit_sha"]}

    @api.post("/outcome")
    def outcome(request: Request, payload: Outcome):
        _, _, release, claims = context(request, payload)
        if not payload.rollback and release["commit_sha"] != claims["sha"]:
            raise HTTPException(409, "Workflow commit changed")
        with db.tx(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE sdlc_permits SET outcome=? WHERE token_hash=? AND release_id=? AND run_id=?
                   AND attempt=? AND consumed_at IS NOT NULL AND outcome IS NULL AND rollback=?""",
                (payload.outcome, token_hash(payload.permit), payload.release_id, claims["run_id"], claims["run_attempt"], int(payload.rollback)),
            )
            if not updated.rowcount:
                raise HTTPException(409, "No consumed permit can record this outcome")
        audit.log("github-actions", "sdlc.staging.outcome", str(payload.release_id),
                  {"run": claims["run_id"], "result": payload.outcome}, outcome=payload.outcome)
        return {"outcome": payload.outcome}

    return api
