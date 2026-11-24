#!/usr/bin/env bash
# Bring the whole CSIR Dev Hub PoC up (idempotent; safe after a box/host restart).
#   1. generate secrets if missing  2. compose up (core [+ OIDC overlay] [+ Guacamole overlay])
#   3. wait for Coder/Keycloak     4. identity bootstrap (LDAP seed, realm, roles, MFA, Coder OIDC)
#   5. Guacamole bootstrap          6. push Coder templates  7. health summary
# Never prints secret values.
set -euo pipefail
PY=${PYTHON:-python3}   # Windows/Git Bash: start-desktop.ps1 sets PYTHON=python
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
[ -f .env ] || cp .env.example .env
./scripts/gen-secrets.sh >/dev/null
set -a; . ./.env; set +a
H=${HOST_IP:-127.0.0.1}
wait_url() { for _ in $(seq 1 200); do curl -sf -m 5 -o /dev/null "$1" && return 0; sleep 3; done; echo "timeout waiting for $1" >&2; return 1; }

echo "==> compose up"
./scripts/dc.sh up -d --build
wait_url "http://127.0.0.1:${CODER_PORT:-3000}/healthz"
wait_url "http://127.0.0.1:${KEYCLOAK_PORT:-8080}/realms/master"

if [ "${DESKTOP_MODE:-false}" = "true" ]; then
  echo "==> workspace images (Docker Desktop: ${DESKTOP_TEMPLATES:-ubuntu-server}; built once)"
  for t in ${DESKTOP_TEMPLATES:-ubuntu-server}; do
    case $t in
      ubuntu-server)  img=csir-devhub/ubuntu-server:24.04;  args=(--build-arg UBUNTU_VERSION=24.04 images/ubuntu-server) ;;
      ubuntu-desktop) img=csir-devhub/ubuntu-desktop:24.04; args=(--build-arg UBUNTU_VERSION=24.04 images/ubuntu-desktop) ;;
      kali)           img=csir-devhub/kali:rolling;         args=(images/kali) ;;
      *) continue ;;
    esac
    docker image inspect "$img" >/dev/null 2>&1 && { echo "   $img present"; continue; }
    echo "   building $img"; docker build -q -t "$img" "${args[@]}" >/dev/null
  done
fi

echo "==> identity bootstrap (LDAP, Keycloak realm, roles, MFA, Coder OIDC)"
$PY scripts/bootstrap-identity.py
echo "==> Coder bootstrap (first admin user, portal service token)"
$PY scripts/bootstrap-coder.py
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
elif [ "${DESKTOP_MODE:-false}" = "true" ]; then
  echo "==> pushing Coder templates (coder CLI inside the coder container)"
  $PY scripts/bootstrap-coder.py --push ${DESKTOP_TEMPLATES:-ubuntu-server} --create admin/demo-ubuntu:ubuntu-server
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
