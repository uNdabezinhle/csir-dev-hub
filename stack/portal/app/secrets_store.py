"""Per-user secrets: stored encrypted (Fernet) in the portal DB and injected into workspaces as files on the
user's read-only volume `csir-secrets-<user>` (mounted at /run/csir-secrets by every template)."""
import io, tarfile, time, re, secrets as pysecrets, logging
from cryptography.fernet import Fernet
from . import db
from .config import FERNET_KEY
log = logging.getLogger("secrets")
_f = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

def put(username, name, value):
    if not NAME_RE.match(name): raise ValueError("invalid secret name")
    with db.tx() as c:
        c.execute("INSERT INTO secrets(username,name,value_enc,updated_at) VALUES (?,?,?,?) ON CONFLICT(username,name) DO UPDATE SET value_enc=excluded.value_enc, updated_at=excluded.updated_at",
                  (username, name, _f.encrypt(value.encode()).decode(), time.time()))
    sync(username)

def delete(username, name):
    with db.tx() as c: c.execute("DELETE FROM secrets WHERE username=? AND name=?", (username, name))
    sync(username)

def get(username, name):
    r = db.one("SELECT value_enc FROM secrets WHERE username=? AND name=?", (username, name))
    return _f.decrypt(r["value_enc"].encode()).decode() if r else None

def names(username):
    return db.q("SELECT name, updated_at FROM secrets WHERE username=? ORDER BY name", (username,))

def ensure_rdp_password(username):
    """System secret used for brokered RDP into the user's desktop workspaces."""
    pw = get(username, "rdp_password")
    if not pw:
        pw = pysecrets.token_urlsafe(18); put(username, "rdp_password", pw)
    return pw

def sync(username):
    """Rewrite the user's secrets volume with all current secrets (files mode 0440, owned by uid 1000)."""
    import docker
    cli = docker.from_env()
    vol = f"csir-secrets-{username}"
    try: cli.volumes.get(vol)
    except docker.errors.NotFound: cli.volumes.create(vol, labels={"org.csir.devhub": "secrets", "coder.owner": username})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for r in db.q("SELECT name, value_enc FROM secrets WHERE username=?", (username,)):
            data = _f.decrypt(r["value_enc"].encode())
            ti = tarfile.TarInfo(r["name"]); ti.size = len(data); ti.mode = 0o440; ti.uid = ti.gid = 1000; ti.mtime = time.time()
            tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    c = cli.containers.create("alpine:latest", command=["sh", "-c", "true"], volumes={vol: {"bind": "/s", "mode": "rw"}})
    try:
        c.start(); c.wait()
        # clear then write fresh content
        cli.containers.run("alpine:latest", ["sh", "-c", "rm -rf /s/* && chown 1000:1000 /s && chmod 0750 /s"], volumes={vol: {"bind": "/s", "mode": "rw"}}, remove=True)
        c.put_archive("/s", buf.getvalue())
    finally:
        c.remove(force=True)
