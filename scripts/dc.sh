#!/usr/bin/env bash
# docker compose wrapper: loads .env + secrets/devhub.env, adds the OIDC overlay when configured.
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
FILES=(-f docker-compose.yml)
if grep -qE '^OIDC_ISSUER_URL=.+' secrets/devhub.env 2>/dev/null; then FILES+=(-f docker-compose.oidc.yml); fi
if grep -qE '^GUACAMOLE_ENABLED=true' .env 2>/dev/null; then FILES+=(-f docker-compose.guacamole.yml); fi
exec docker compose --env-file .env --env-file secrets/devhub.env "${FILES[@]}" "$@"
