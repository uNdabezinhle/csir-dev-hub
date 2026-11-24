#!/usr/bin/env bash
# Bring the whole CSIR Dev Hub PoC up (idempotent; safe after a box/host restart).
#   1. generate secrets if missing  2. compose up (core [+ OIDC overlay] [+ Guacamole overlay])
#   3. wait for Coder/Keycloak     4. identity bootstrap (LDAP seed, realm, roles, MFA, Coder OIDC)
#   5. Guacamole bootstrap          6. push Coder templates  7. health summary
# Never prints secret values.
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
[ -f .env ] || cp .env.example .env
./scripts/gen-secrets.sh >/dev/null
set -a; . ./.env; set +a
H=${HOST_IP:-127.0.0.1}
wait_url() { for _ in $(seq 1 100); do curl -sf -o /dev/null "$1" && return 0; sleep 3; done; echo "timeout waiting for $1" >&2; return 1; }

echo "==> compose up"
./scripts/dc.sh up -d --build
wait_url "http://127.0.0.1:${CODER_PORT:-3000}/healthz"
wait_url "http://127.0.0.1:${KEYCLOAK_PORT:-8080}/realms/master"

echo "==> identity bootstrap (LDAP, Keycloak realm, roles, MFA, Coder OIDC)"
python3 scripts/bootstrap-identity.py
# bootstrap-identity may have set OIDC_ISSUER_URL; re-run compose so the OIDC overlay is applied
./scripts/dc.sh up -d
wait_url "http://127.0.0.1:${CODER_PORT:-3000}/healthz"

if [ "${GUACAMOLE_ENABLED:-false}" = "true" ]; then
  echo "==> Guacamole bootstrap"
  ./scripts/bootstrap-guacamole.sh
fi

if command -v coder >/dev/null && coder whoami >/dev/null 2>&1; then
  echo "==> pushing Coder templates"
  for t in ubuntu-server ubuntu-desktop kali; do
    coder templates push "$t" -d "templates/$t" --yes >/dev/null 2>&1 && echo "   $t pushed" || echo "   $t push failed (image built? scripts/build-images.sh)"
  done
else
  echo "   (coder CLI not logged in; skip template push: coder login http://$H:${CODER_PORT:-3000})"
fi

echo "==> status"
./scripts/dc.sh ps --format 'table {{.Service}}\t{{.Status}}'
cat <<URLS

Coder          http://$H:${CODER_PORT:-3000}
Portal/Admin   http://$H:${ADMIN_CONSOLE_PORT:-8088}
Keycloak       http://$H:${KEYCLOAK_PORT:-8080}/admin/master/console/  (realm: csir)
Grafana        http://$H:${GRAFANA_PORT:-3001}
Prometheus     http://$H:${PROMETHEUS_PORT:-9090}
Guacamole      http://$H:${GUACAMOLE_PORT:-8089}/guacamole/
Credentials    secrets/devhub.env (chmod 600)
URLS
