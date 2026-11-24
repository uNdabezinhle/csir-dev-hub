#!/usr/bin/env bash
# Idempotent: creates guacamole_db + role in the shared postgres, loads the schema, starts guacd/guacamole and rotates
# the default guacadmin/guacadmin password to GUACADMIN_PASSWORD (secrets/devhub.env). Never prints secrets.
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
set -a; . ./.env; . ./secrets/devhub.env; set +a
PG=$(docker ps -qf label=com.docker.compose.service=postgres -f label=com.docker.compose.project=csir-devhub)
psql() { docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U coder -d "${1:-coder}" -qtA; }
if [ "$(echo "SELECT 1 FROM pg_roles WHERE rolname='guacamole'" | psql)" != "1" ]; then
  echo "CREATE ROLE guacamole LOGIN PASSWORD '${GUAC_DB_PASSWORD}';" | psql
fi
if [ "$(echo "SELECT 1 FROM pg_database WHERE datname='guacamole_db'" | psql)" != "1" ]; then
  echo "CREATE DATABASE guacamole_db OWNER guacamole;" | psql
fi
if [ "$(echo "SELECT to_regclass('public.guacamole_user') IS NOT NULL" | docker exec -i "$PG" psql -U guacamole -d guacamole_db -qtA 2>/dev/null || echo f)" != "t" ]; then
  docker exec -i -e PGPASSWORD="$GUAC_DB_PASSWORD" "$PG" psql -v ON_ERROR_STOP=1 -q -U guacamole -d guacamole_db < stack/guacamole/initdb.sql
  echo "guacamole_db: schema loaded"
else
  echo "guacamole_db: schema present"
fi
./scripts/dc.sh up -d guacd guacamole >/dev/null 2>&1
URL="http://127.0.0.1:${GUACAMOLE_PORT:-8089}/guacamole"
for i in $(seq 1 60); do curl -sf -o /dev/null "$URL/" && break; sleep 3; done
tok() { curl -s -X POST "$URL/api/tokens" --data-urlencode "username=guacadmin" --data-urlencode "password=$1" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("authToken",""))' 2>/dev/null; }
T=$(tok guacadmin)
if [ -n "$T" ]; then
  python3 - "$URL" "$T" <<'PY'
import json, os, sys, urllib.request
url, tok = sys.argv[1], sys.argv[2]
body = json.dumps({"oldPassword": "guacadmin", "newPassword": os.environ["GUACADMIN_PASSWORD"]}).encode()
req = urllib.request.Request(f"{url}/api/session/data/postgresql/users/guacadmin/password?token={tok}", data=body, method="PUT",
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req); print("guacadmin: default password rotated")
PY
else
  echo "guacadmin: default password already rotated"
fi
[ -n "$(tok "$GUACADMIN_PASSWORD")" ] && echo "guacadmin: login with generated password OK" || { echo "guacadmin: login FAILED"; exit 1; }
[ -z "$(tok guacadmin)" ] && echo "guacadmin: default password rejected (good)"
