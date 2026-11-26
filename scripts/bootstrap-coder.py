#!/usr/bin/env python3
"""Idempotent Coder bootstrap without a host coder CLI (used by scripts/start.sh on Docker Desktop / fresh hosts).
  1. creates the first Coder user (admin / CODER_ADMIN_EMAIL / CODER_ADMIN_PASSWORD) if none exists
  2. mints a long-lived portal service token into secrets/devhub.env (CODER_SESSION_TOKEN) if missing/invalid
  3. --push TEMPLATE...   pushes templates/<name> with the coder CLI *inside* the coder container (skips a template
                          whose workspace image is not built locally)
  4. --create OWNER/NAME:TEMPLATE  creates a workspace with the template defaults if it does not exist
Never prints secrets."""
import json, os, subprocess, sys, time, urllib.request, urllib.error
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SECRETS = f"{ROOT}/secrets/devhub.env"
def envfile(p):
    d = {}
    for l in open(p):
        l = l.strip()
        if l and not l.startswith("#") and "=" in l: k, v = l.split("=", 1); d[k] = v
    return d
S = envfile(SECRETS); E = envfile(f"{ROOT}/.env")
CODER = f"http://127.0.0.1:{E.get('CODER_PORT', '3000')}"
CONTAINER = f"{E.get('COMPOSE_PROJECT_NAME', 'csir-devhub')}-coder-1"
IMAGES = {"ubuntu-server": "csir-devhub/ubuntu-server:24.04", "ubuntu-desktop": "csir-devhub/ubuntu-desktop:24.04",
          "kali": "csir-devhub/kali:rolling"}

def req(method, path, body=None, tok=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Coder-Session-Token"] = tok
    r = urllib.request.Request(CODER + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            t = resp.read(); return resp.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        return e.code, {}

def set_secret(key, val):
    lines = open(SECRETS).read().splitlines(); found = False
    for i, l in enumerate(lines):
        if l.startswith(key + "="): lines[i] = f"{key}={val}"; found = True
    if not found: lines.append(f"{key}={val}")
    open(SECRETS, "w", newline="\n").write("\n".join(lines) + "\n")
    try: os.chmod(SECRETS, 0o600)
    except OSError: pass
    S[key] = val

def first_user():
    if req("GET", "/api/v2/users/first")[0] == 200: print("Coder: first user exists"); return
    st, _ = req("POST", "/api/v2/users/first", {"email": S["CODER_ADMIN_EMAIL"], "username": "admin", "name": "Hub Admin",
                                                "password": S["CODER_ADMIN_PASSWORD"], "trial": False})
    print("Coder: first user 'admin' created" if st == 201 else f"Coder: first-user create FAILED (HTTP {st})")
    if st != 201: sys.exit(1)

def token():
    t = S.get("CODER_SESSION_TOKEN", "")
    if t and req("GET", "/api/v2/users/me", tok=t)[0] == 200: print("Coder: portal service token valid"); return t
    st, r = req("POST", "/api/v2/users/login", {"email": S["CODER_ADMIN_EMAIL"], "password": S["CODER_ADMIN_PASSWORD"]})
    if st != 201: print(f"Coder: admin login FAILED (HTTP {st})"); sys.exit(1)
    st, k = req("POST", "/api/v2/users/me/keys/tokens", {"lifetime": 8760 * 3600 * 10**9, "scope": "all",
                "token_name": f"devhub-portal-{int(time.time())}"}, tok=r["session_token"])
    if st != 201: print(f"Coder: token mint FAILED (HTTP {st})"); sys.exit(1)
    set_secret("CODER_SESSION_TOKEN", k["key"]); print("Coder: portal service token minted -> secrets/devhub.env")
    return k["key"]

def cli(tok, *args):
    return subprocess.run(["docker", "exec", "-e", "CODER_URL=http://127.0.0.1:3000", "-e", f"CODER_SESSION_TOKEN={tok}",
                           CONTAINER, "coder", *args], capture_output=True, text=True)

def push(tok, names):
    for n in names:
        img = IMAGES.get(n)
        if img and subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode != 0:
            print(f"   {n}: image {img} not built - skipped"); continue
        staging = subprocess.run(
            ["docker", "exec", CONTAINER, "mktemp", "-d", "/tmp/devhub-template.XXXXXXXX"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if not staging.startswith("/tmp/devhub-template.") or not staging.removeprefix("/tmp/devhub-template.").isalnum():
            raise RuntimeError("Unexpected Coder template staging path")
        source = f"{staging}/source"
        try:
            subprocess.run(
                ["docker", "cp", os.path.join(ROOT, "templates", n), f"{CONTAINER}:{source}"],
                check=True, capture_output=True,
            )
            r = cli(tok, "templates", "push", n, "-d", source, "--yes")
        finally:
            subprocess.run(["docker", "exec", "--user", "0", CONTAINER, "rm", "-rf", staging], capture_output=True)
        print(f"   {n}: pushed" if r.returncode == 0 else f"   {n}: push FAILED: {(r.stderr or r.stdout).strip()[-400:]}")
        if r.returncode != 0:
            raise SystemExit(r.returncode)

def create(tok, spec):
    who, tpl = spec.split(":"); r = cli(tok, "list", "--all", "-o", "json")
    have = {f"{w['owner_name']}/{w['name']}" for w in json.loads(r.stdout or "[]")} if r.returncode == 0 else set()
    if who in have: print(f"   workspace {who}: exists"); return
    r = cli(tok, "create", who, "--template", tpl, "--yes", "--use-parameter-defaults")
    if r.returncode != 0 and "use-parameter-defaults" in (r.stderr or ""):
        r = cli(tok, "create", who, "--template", tpl, "--yes")
    print(f"   workspace {who}: created" if r.returncode == 0 else f"   workspace {who}: FAILED: {(r.stderr or r.stdout).strip()[-400:]}")

if __name__ == "__main__":
    a = sys.argv[1:]
    first_user(); tok = token()
    if "--push" in a:
        i = a.index("--push"); names = []
        for x in a[i + 1:]:
            if x.startswith("--"): break
            names.append(x)
        push(tok, names)
    if "--create" in a: create(tok, a[a.index("--create") + 1])
