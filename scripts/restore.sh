#!/usr/bin/env bash
# CSIR Dev Hub restore.
#   scripts/restore.sh --list
#   scripts/restore.sh <backup-dir|latest> --verify
#       Non-destructive restore test: checks manifest sha256s, restores every PostgreSQL dump into a scratch database
#       (restore_verify_<db>) and compares row counts of key tables with the live DB, restores the portal SQLite DB to
#       a temp file (integrity_check + counts), checks the LDAP LDIF entry count against the live directory, test-reads
#       every volume tarball, decrypts the secrets archive; then drops the scratch DBs. Result -> status.json.
#   scripts/restore.sh <backup-dir|latest> --restore <component> --yes
#       Real restore (destructive). component: postgres:<db> | portal | ldap | volume:<name> | config
#       Stop the consumers first (e.g. ./scripts/dc.sh stop coder for postgres:coder).
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
envget(){ grep -E "^$1=" "$2" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
BACKUP_DIR=${BACKUP_DIR:-$(envget BACKUP_DIR .env)}; BACKUP_DIR=${BACKUP_DIR:-/workspace/csir-backups}
PG=csir-devhub-postgres-1; LDAP=csir-devhub-openldap-1; PORTAL=csir-devhub-portal-1; PGUSER=coder
[ "${1:-}" = "--list" ] && { ls -1 "$BACKUP_DIR" | grep -E '^[0-9]{8}-[0-9]{6}$' | while read -r b; do echo "$b  $(du -sh "$BACKUP_DIR/$b" | cut -f1)"; done; exit 0; }
B=${1:?usage: restore.sh <backup|latest> --verify | --restore <component> --yes}; shift
[ "$B" = latest ] && B=$(ls -1 "$BACKUP_DIR" | grep -E '^[0-9]{8}-[0-9]{6}$' | sort | tail -1)
[ -d "$B" ] || B="$BACKUP_DIR/$B"; [ -f "$B/manifest.json" ] || { echo "no manifest in $B"; exit 2; }
MODE=${1:-}; pass=0; fail=0
ok(){ echo "PASS $*"; pass=$((pass+1)); }; ko(){ echo "FAIL $*"; fail=$((fail+1)); }
q(){ docker exec $PG psql -U $PGUSER -d "$1" -Atc "$2" 2>/dev/null; }

if [ "$MODE" = --verify ]; then
  echo "== verify $(basename "$B")"
  python3 - "$B" <<'PY' && ok "R1 manifest: all file sha256 match" || ko "R1 manifest sha256 mismatch"
import hashlib, json, os, sys
d = sys.argv[1]; m = json.load(open(os.path.join(d, "manifest.json"))); bad = []
for f, meta in m["files"].items():
    h = hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest()
    if h != meta["sha256"]: bad.append(f)
print(f"   {len(m['files'])} files, created {m['created']}, git {m['git']}"); sys.exit(1 if bad else 0)
PY
  declare -A KEY=([coder]="users workspaces templates workspace_builds" [keycloak]="realm user_entity credential" [guacamole_db]="guacamole_entity guacamole_connection_history")
  for f in "$B"/postgres/*.dump; do
    db=$(basename "$f" .dump); s="restore_verify_${db}"
    docker exec $PG psql -U $PGUSER -d postgres -qc "DROP DATABASE IF EXISTS \"$s\"" >/dev/null 2>&1
    docker exec $PG psql -U $PGUSER -d postgres -qc "CREATE DATABASE \"$s\"" >/dev/null
    if docker exec -i $PG pg_restore -U $PGUSER -d "$s" --no-owner --no-acl --exit-on-error < "$f" 2>/tmp/pg_restore.err; then
      nt=$(q "$s" "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")
      detail=""; mism=0
      for t in ${KEY[$db]:-}; do
        a=$(q "$s" "SELECT count(*) FROM $t"); l=$(q "$db" "SELECT count(*) FROM $t"); detail="$detail $t=$a(live $l)"
        [ "$a" = "$l" ] || mism=1
      done
      # live DB can legitimately move on after the dump (new builds, sessions): mismatch is reported, not fatal
      ok "R2 postgres:$db restored into scratch DB $s ($nt tables)${detail:+ -$detail}$([ $mism = 1 ] && echo ' [live changed since backup]')"
    else ko "R2 postgres:$db pg_restore failed: $(head -c 300 /tmp/pg_restore.err)"; fi
    docker exec $PG psql -U $PGUSER -d postgres -qc "DROP DATABASE IF EXISTS \"$s\"" >/dev/null
  done
  docker cp -q "$B/portal/devhub.db" $PORTAL:/tmp/verify.db
  r=$(docker exec $PORTAL python -c "
import sqlite3; c=sqlite3.connect('/tmp/verify.db'); ic=c.execute('PRAGMA integrity_check').fetchone()[0]
print(ic, *[f'{t}={c.execute(f\"select count(*) from {t}\").fetchone()[0]}' for t in ('users','workspaces','audit','catalog')])"); docker exec $PORTAL rm -f /tmp/verify.db
  [[ $r == ok* ]] && ok "R3 portal SQLite restored to temp: $r" || ko "R3 portal SQLite: $r"
  n_b=$(grep -c '^dn:' "$B/ldap/data.ldif"); n_l=$(docker exec $LDAP slapcat -n 1 | grep -c '^dn:')
  head -1 "$B/ldap/config.ldif" | grep -q '^dn: cn=config' && [ "$n_b" -gt 0 ] && ok "R4 LDAP LDIF: $n_b entries (live $n_l), cn=config present" || ko "R4 LDAP LDIF"
  nv=0; bad=""
  for v in "$B"/volumes/*.tar.gz; do [ -e "$v" ] || continue; nv=$((nv+1)); gzip -t "$v" && tar tzf "$v" >/dev/null || bad="$bad $(basename "$v")"; done
  [ -z "$bad" ] && ok "R5 $nv volume archives readable" || ko "R5 corrupt volume archives:$bad"
  if [ -f "$B/config/secrets.tar.gz.enc" ]; then
    PASS=$(envget BACKUP_PASSPHRASE secrets/devhub.env)
    BP="$PASS" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BP -in "$B/config/secrets.tar.gz.enc" | tar tzf - | grep -c devhub.env >/dev/null \
      && ok "R6 secrets archive decrypts (AES-256, passphrase from BACKUP_PASSPHRASE)" || ko "R6 secrets archive decrypt"
  else tar tzf "$B/config/secrets.tar.gz" | grep -c devhub.env >/dev/null && ok "R6 secrets archive readable (unencrypted)" || ko "R6 secrets archive"; fi
  tar tzf "$B/config/config.tar.gz" | grep -c '^docker-compose.yml$' >/dev/null && ok "R7 config archive contains docker-compose.yml" || ko "R7 config archive"
  left=$(q postgres "SELECT count(*) FROM pg_database WHERE datname LIKE 'restore_verify_%'")
  [ "$left" = 0 ] && ok "R8 scratch databases dropped" || ko "R8 $left scratch DBs left"
  st=$([ $fail = 0 ] && echo ok || echo failed)
  python3 - "$BACKUP_DIR/status.json" "$st" "$(basename "$B")" "$pass/$((pass+fail))" <<'PY'
import json, sys, time, os
f = sys.argv[1]
try: d = json.load(open(f))
except Exception: d = {}
d["verify"] = {"status": sys.argv[2], "backup": sys.argv[3], "checks": sys.argv[4], "finished_at": time.strftime("%Y-%m-%d %H:%M %Z")}
json.dump(d, open(f, "w"), indent=1); os.chmod(f, 0o644)
PY
  docker cp -q "$BACKUP_DIR/status.json" $PORTAL:/data/backup-status.json 2>/dev/null || true
  echo "$pass passed, $fail failed"; [ $fail = 0 ]; exit
fi

if [ "$MODE" = --restore ]; then
  C=${2:?component}; [ "${3:-}" = --yes ] || { echo "destructive: re-run with --yes"; exit 2; }
  case $C in
    postgres:*) db=${C#postgres:}; echo "restoring $db (consumers should be stopped)"
      docker exec $PG psql -U $PGUSER -d postgres -qc "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)"
      docker exec $PG psql -U $PGUSER -d postgres -qc "CREATE DATABASE \"$db\""
      docker exec -i $PG psql -U $PGUSER -d postgres -q < "$B/postgres/globals.sql" >/dev/null 2>&1 || true
      docker exec -i $PG pg_restore -U $PGUSER -d "$db" --exit-on-error < "$B/postgres/$db.dump" ;;
    portal) docker cp -q "$B/portal/devhub.db" $PORTAL:/data/devhub.db && docker restart $PORTAL ;;
    ldap) docker stop $LDAP; docker run --rm --volumes-from $LDAP -v "$B/ldap":/b:ro --entrypoint sh osixia/openldap:1.5.0 -c \
            'rm -rf /var/lib/ldap/* && slapadd -n 1 -l /b/data.ldif && chown -R openldap:openldap /var/lib/ldap'; docker start $LDAP ;;
    volume:*) v=${C#volume:}; docker run --rm -i -v "$v":/v alpine:3.20 sh -c 'rm -rf /v/* /v/.[!.]* 2>/dev/null; tar xzf - -C /v' < "$B/volumes/$v.tar.gz" ;;
    config) mkdir -p restore-config && tar xzf "$B/config/config.tar.gz" -C restore-config && echo "config extracted to ./restore-config (review, then copy)" ;;
    *) echo "unknown component $C"; exit 2 ;;
  esac
  echo "restored $C from $(basename "$B")"; exit 0
fi
echo "usage: restore.sh --list | <backup|latest> --verify | <backup|latest> --restore <component> --yes"; exit 2
