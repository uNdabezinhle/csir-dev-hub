"""Runtime settings (env) + static policy files."""
import json, os
from pathlib import Path

CFG_DIR = Path(os.environ.get("PORTAL_CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))
DATA_DIR = Path(os.environ.get("PORTAL_DATA_DIR", "/data"))

def _j(name):
    return json.loads((CFG_DIR / name).read_text())

ROLES = _j("roles.json")
CATALOG_SEED = _j("catalog.json")["items"]
POLICY = _j("policy.json")
ROLE_IDS = [r["id"] for r in ROLES["roles"]]
GROUP_TO_ROLE = {r["ldap_group"]: r["id"] for r in ROLES["roles"]}

HOST_IP = os.environ.get("HOST_IP", "localhost")
CODER_URL = os.environ.get("CODER_URL", "http://coder:3000")              # server-to-server
CODER_PUBLIC_URL = os.environ.get("CODER_PUBLIC_URL", f"http://{HOST_IP}:3000")  # browser links
CODER_TOKEN = os.environ.get("CODER_SESSION_TOKEN", "")
PORTAL_PUBLIC_URL = os.environ.get("PORTAL_PUBLIC_URL", f"http://{HOST_IP}:8088")
OIDC_ISSUER = os.environ.get("OIDC_ISSUER_URL", "")
OIDC_CLIENT_ID = os.environ.get("PORTAL_OIDC_CLIENT_ID", "devhub-portal")
OIDC_CLIENT_SECRET = os.environ.get("PORTAL_OIDC_SECRET", "")
SESSION_KEY = os.environ.get("PORTAL_SESSION_KEY", "dev-insecure-key")
FERNET_KEY = os.environ.get("PORTAL_FERNET_KEY", "")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("PORTAL_BOOTSTRAP_ADMIN_PASSWORD", "")
GUAC_PUBLIC_URL = os.environ.get("GUAC_PUBLIC_URL", f"http://{HOST_IP}:8089/guacamole")
GUAC_JSON_SECRET = os.environ.get("GUAC_JSON_SECRET", "")
GUACD_CONTAINER = os.environ.get("GUACD_CONTAINER", "csir-devhub-guacd-1")
# Session recording (annex: audit of privileged/remote sessions). Files land in the guac-recordings volume.
GUAC_RECORDING = os.environ.get("GUAC_RECORDING", "true").lower() == "true"
GRAFANA_PUBLIC_URL = os.environ.get("GRAFANA_PUBLIC_URL", f"http://{HOST_IP}:3001")
KEYCLOAK_PUBLIC_URL = os.environ.get("KEYCLOAK_PUBLIC_URL", f"http://{HOST_IP}:8080")
HEALTH_TARGETS = {
    "Coder": f"{CODER_URL}/healthz",
    "Keycloak (realm csir)": f"{KEYCLOAK_PUBLIC_URL}/realms/csir",
    "Prometheus": "http://prometheus:9090/-/healthy",
    "Grafana": "http://grafana:3000/api/health",
    "Guacamole": "http://guacamole:8080/guacamole/",
}
