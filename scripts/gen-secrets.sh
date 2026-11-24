#!/usr/bin/env bash
# Generates secrets/devhub.env once (idempotent). Never commit it.
set -euo pipefail
cd "$(dirname "$0")/.."
F=secrets/devhub.env
[ -f "$F" ] && { echo "$F exists, keeping it"; exit 0; }
gen() { openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 28; }
umask 077
cat > "$F" <<EOT
# CSIR Dev Hub secrets - generated $(date -Iseconds)
POSTGRES_PASSWORD=$(gen)
KEYCLOAK_DB_PASSWORD=$(gen)
KEYCLOAK_ADMIN_PASSWORD=$(gen)
LDAP_ADMIN_PASSWORD=$(gen)
LDAP_DEMO_PASSWORD=$(gen)
OIDC_CLIENT_SECRET=$(gen)
GRAFANA_ADMIN_PASSWORD=$(gen)
CODER_ADMIN_EMAIL=admin@csir.local
CODER_ADMIN_PASSWORD=$(gen)Aa1!
PORTAL_OIDC_SECRET=$(gen)
PORTAL_SESSION_KEY=$(gen)$(gen)
PORTAL_FERNET_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
GUAC_JSON_SECRET=$(openssl rand -hex 16)
GUAC_DB_PASSWORD=$(gen)
GUACADMIN_PASSWORD=$(gen)
# filled in by scripts/bootstrap.sh
CODER_SESSION_TOKEN=
OIDC_ISSUER_URL=
EOT
chmod 600 "$F"
echo "wrote $F"
