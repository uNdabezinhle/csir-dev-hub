"""Consume a Dev Hub staging permit immediately before applying the approved digest."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AUDIENCE = "csir-devhub-staging"
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Authorisation request failed (HTTP {error.code})") from None
    except (URLError, ValueError, OSError):
        raise RuntimeError("Authorisation service could not be reached or returned an invalid response") from None


def github_token():
    endpoint = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    delimiter = "&" if "?" in endpoint else "?"
    return request_json(endpoint + delimiter + urlencode({"audience": AUDIENCE}),
                        os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"])["value"]


def main():
    gate = os.environ["DEVHUB_GATE_URL"].rstrip("/")
    url = urlsplit(gate)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Set DEVHUB_GATE_URL to the approved HTTPS portal origin")
    compose = Path(os.environ["DEVHUB_STAGING_COMPOSE_FILE"])
    if not compose.is_absolute() or not compose.is_file():
        raise ValueError("Set DEVHUB_STAGING_COMPOSE_FILE to an absolute staging-only Compose file")
    release = os.environ["DEVHUB_STAGING_RELEASE"]
    digest = os.environ["DEVHUB_STAGING_DIGEST"]
    if not re.fullmatch(r"[1-9][0-9]{0,17}", release) or not DIGEST.fullmatch(digest):
        raise ValueError("Supply a release number and an immutable image digest")
    candidate = {"release_id": int(release), "digest": digest,
                 "rollback": os.environ.get("DEVHUB_STAGING_ROLLBACK") == "true"}
    base = gate + "/api/sdlc/automation"
    expected_image = f"ghcr.io/{os.environ['GITHUB_REPOSITORY'].lower()}/portal@{digest}"
    command = ["docker", "compose", "--project-name", "csir-devhub-staging", "-f", str(compose)]
    with TemporaryDirectory(prefix="devhub-staging-docker-") as docker_config:
        env = {key: value for key, value in os.environ.items()
               if key not in {"GHCR_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL"}}
        env.update(DOCKER_CONFIG=docker_config, PORTAL_STAGING_IMAGE=expected_image)
        definition = json.loads(subprocess.check_output(command + ["config", "--format", "json"], env=env))
        portal = definition.get("services", {}).get("portal", {})
        if portal.get("image") != expected_image or "build" in portal:
            raise ValueError("Staging Compose must use PORTAL_STAGING_IMAGE for the portal and omit build")
        healthcheck = portal.get("healthcheck") or {}
        if healthcheck.get("disable") or not healthcheck.get("test") or healthcheck["test"] == ["NONE"]:
            raise ValueError("Staging Compose must define a portal readiness health check")
        subprocess.run(["docker", "login", "ghcr.io", "--username", os.environ["GITHUB_ACTOR"], "--password-stdin"],
                       input=os.environ["GHCR_TOKEN"], text=True, check=True, env=env)
        subprocess.run(["docker", "pull", expected_image], check=True, env=env)
        digests = json.loads(subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", expected_image], env=env))
        if expected_image not in digests:
            raise ValueError("Pulled image does not match the requested registry digest")
        permit = request_json(base + "/permit", github_token(), candidate)
        if permit.get("image") != expected_image:
            raise ValueError("Permit image does not match the pulled artifact")
        labels = json.loads(subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", expected_image], env=env)) or {}
        if not permit.get("commit") or labels.get("org.opencontainers.image.revision") != permit["commit"]:
            raise ValueError("Artifact revision label does not match the approved source commit")
        use = {**candidate, "permit": permit["permit"]}
        consumed = request_json(base + "/consume", github_token(), use)
        result = "failed"
        try:
            if consumed.get("image") != expected_image or consumed.get("commit") != permit["commit"]:
                raise ValueError("Consumed permit does not match the pulled artifact")
            subprocess.run(command + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait",
                                      "--wait-timeout", "90", "portal"], check=True, env=env)
            container = subprocess.check_output(command + ["ps", "-q", "portal"], text=True, env=env).strip()
            running = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Config.Image}}", container], text=True, env=env).strip()
            if running != expected_image:
                raise ValueError("The staging portal is not running the approved digest")
            result = "succeeded"
        finally:
            request_json(base + "/outcome", github_token(), {**use, "outcome": result})
    print(f"Staging deployed release {release} at {digest}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
