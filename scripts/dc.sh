#!/usr/bin/env bash
# docker compose wrapper: loads .env + secrets/devhub.env, adds the OIDC overlay when configured.
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
FILES=(-f docker-compose.yml)
if grep -qE '^OIDC_ISSUER_URL=.+' secrets/devhub.env 2>/dev/null; then FILES+=(-f docker-compose.oidc.yml); fi
if grep -qE '^GUACAMOLE_ENABLED=true' .env 2>/dev/null; then FILES+=(-f docker-compose.guacamole.yml); fi
ENVS=(--env-file .env --env-file secrets/devhub.env)
# Public mode (scripts/tunnel.sh up): public.env overrides URLs + OIDC issuer, docker-compose.public.yml applies them.
# "Lite" public mode (scripts/tunnel-pinggy.sh): only portal links change; Coder + Keycloak stay on local URLs.
if [ -f public.env ]; then
  ENVS+=(--env-file public.env)
  if grep -qE '^PUBLIC_MODE=lite' public.env; then FILES+=(-f docker-compose.public-lite.yml); else FILES+=(-f docker-compose.public.yml); fi
fi
exec docker compose "${ENVS[@]}" "${FILES[@]}" "$@"
