#!/usr/bin/env bash
# CSIR Dev Hub backup: PostgreSQL DBs (coder, keycloak, guacamole_db + roles), portal DB (SQLite), LDAP (slapcat),
# Docker volumes (Coder home, Grafana, Guacamole recordings, Prometheus, workspace homes, secret volumes; VM disks with
# --with-vms), and configuration (compose, .env, stack/, identity/, templates/) + encrypted secrets.
# Requires BACKUP_PASSPHRASE. Writes manifest.json (sha256 per file) and $BACKUP_DIR/status.json
# (shown in the portal), then applies the retention policy.
#   scripts/backup.sh [--with-vms] [--no-volumes] [--no-retention]
# Env (.env): BACKUP_DIR (default /workspace/csir-backups), BACKUP_KEEP_DAILY (7), BACKUP_KEEP_WEEKLY (4)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
envget(){ grep -E "^$1=" "$2" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
BACKUP_DIR=${BACKUP_DIR:-$(envget BACKUP_DIR .env)}; BACKUP_DIR=${BACKUP_DIR:-/workspace/csir-backups}
KEEP_DAILY=${BACKUP_KEEP_DAILY:-$(envget BACKUP_KEEP_DAILY .env)}; KEEP_DAILY=${KEEP_DAILY:-7}
KEEP_WEEKLY=${BACKUP_KEEP_WEEKLY:-$(envget BACKUP_KEEP_WEEKLY .env)}; KEEP_WEEKLY=${KEEP_WEEKLY:-4}
WITH_VMS=0; VOLUMES=1; RETENTION=1
for a in "$@"; do case $a in --with-vms) WITH_VMS=1;; --no-volumes) VOLUMES=0;; --no-retention) RETENTION=0;; *) echo "unknown option $a"; exit 2;; esac; done
PASS=${BACKUP_PASSPHRASE:-$(envget BACKUP_PASSPHRASE secrets/devhub.env)}
if [ -z "$PASS" ] || ! command -v openssl >/dev/null; then
  echo "Backup requires OpenSSL and BACKUP_PASSPHRASE" >&2
  exit 2
fi
if [ -n "${RESTIC_REPOSITORY:-}" ] && ! command -v restic >/dev/null; then
  echo "RESTIC_REPOSITORY is configured but restic is unavailable" >&2
  exit 2
fi
PG=csir-devhub-postgres-1; LDAP=csir-devhub-openldap-1; PORTAL=csir-devhub-portal-1; P=csir-devhub
STAMP=$(date +%Y%m%d-%H%M%S); DEST="$BACKUP_DIR/$STAMP"; T0=$(date +%s)
umask 077; mkdir -p "$DEST"/{postgres,ldap,portal,volumes,config}; chmod 700 "$BACKUP_DIR" "$DEST"
log(){ echo "[backup $(date +%H:%M:%S)] $*"; }
status(){ python3 - "$BACKUP_DIR/status.json" "$@" <<'PY'
import json, sys, os
f = sys.argv[1]; d = {}
try: d = json.load(open(f))
except Exception: pass
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1); d[k] = v
json.dump(d, open(f + ".tmp", "w"), indent=1); os.replace(f + ".tmp", f); os.chmod(f, 0o644)
PY
  docker cp -q "$BACKUP_DIR/status.json" csir-devhub-portal-1:/data/backup-status.json 2>/dev/null || true   # portal: Admin -> KVM/Backups
}
trap 'status status=failed finished_at="$(date "+%Y-%m-%d %H:%M %Z")" path="$DEST" detail="failed at line $LINENO"; log "FAILED (line $LINENO)"' ERR
status status=running started_at="$(date '+%Y-%m-%d %H:%M %Z')" path="$DEST" replication=unconfigured

# ---- PostgreSQL (custom format, restorable per DB) + global roles
PGUSER=coder
for db in $(docker exec $PG psql -U $PGUSER -d postgres -Atc "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT LIKE 'restore_verify_%' AND datname <> 'postgres'"); do
  log "pg_dump $db"; docker exec $PG pg_dump -U $PGUSER -Fc -d "$db" > "$DEST/postgres/$db.dump"
done
docker exec $PG pg_dumpall -U $PGUSER --globals-only > "$DEST/postgres/globals.sql"

# ---- Portal DB (SQLite online backup API -> consistent copy)
log "portal sqlite"
docker exec $PORTAL python -c "import sqlite3; s=sqlite3.connect('/data/devhub.db'); d=sqlite3.connect('/tmp/devhub-backup.db'); s.backup(d); d.close()"
docker cp -q $PORTAL:/tmp/devhub-backup.db "$DEST/portal/devhub.db"; docker exec $PORTAL rm -f /tmp/devhub-backup.db

# ---- LDAP (cn=config + data, LDIF)
log "slapcat"
docker exec $LDAP slapcat -n 0 > "$DEST/ldap/config.ldif"
docker exec $LDAP slapcat -n 1 > "$DEST/ldap/data.ldif"

# ---- Docker volumes (tar.gz via a throw-away alpine container, volumes mounted read-only)
if [ $VOLUMES = 1 ]; then
  vols=$(docker volume ls --format '{{.Name}}' | grep -E "^(${P}_(coder-home|grafana-data|guac-recordings|prom-data|ldap-data|ldap-config|portal-data)|coder-.*-home|csir-secrets-.*)$" || true)
  [ $WITH_VMS = 1 ] && vols="$vols ${P}_libvirt-data"
  for v in $vols; do
    log "volume $v"   # provisioner work dirs/terraform plugin caches are re-creatable -> excluded
    if [[ "$v" == csir-secrets-* ]]; then
      docker run --rm -v "$v":/v:ro alpine:3.20 sh -c 'tar czf - -C /v .' |
        BP="$PASS" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BP -out "$DEST/volumes/$v.tar.gz.enc"
    else
      docker run --rm -v "$v":/v:ro alpine:3.20 sh -c "tar czf - -C /v --exclude='./.cache/coder' . 2> >(grep -v 'socket ignored' >&2)" > "$DEST/volumes/$v.tar.gz"
    fi
  done
fi

# ---- Configuration + secrets
log "config"
tar czf "$DEST/config/config.tar.gz" --exclude='.terraform' --exclude='__pycache__' --exclude='output-*' --exclude='packer_cache' --exclude='*.iso' --exclude='build-*.log' docker-compose*.yml .env .env.example stack identity templates scripts packer ops public.env 2>/dev/null || \
  tar czf "$DEST/config/config.tar.gz" --exclude='.terraform' --exclude='__pycache__' --exclude='output-*' --exclude='packer_cache' --exclude='*.iso' --exclude='build-*.log' docker-compose*.yml .env .env.example stack identity templates scripts packer ops
tar czf - secrets | BP="$PASS" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BP -out "$DEST/config/secrets.tar.gz.enc"

# ---- Manifest
python3 - "$DEST" <<'PY'
import hashlib, json, os, sys, subprocess, time
d = sys.argv[1]; files = {}
for root, _, fs in os.walk(d):
    for f in sorted(fs):
        p = os.path.join(root, f); h = hashlib.sha256()
        with open(p, "rb") as fh:
            for ch in iter(lambda: fh.read(1 << 20), b""): h.update(ch)
        files[os.path.relpath(p, d)] = {"bytes": os.path.getsize(p), "sha256": h.hexdigest()}
git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
json.dump({"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "git": git, "files": files}, open(os.path.join(d, "manifest.json"), "w"), indent=1)
PY
SIZE=$(du -sh "$DEST" | cut -f1); EL=$(( $(date +%s) - T0 ))
log "done: $DEST ($SIZE, ${EL}s)"

if [ -n "${RESTIC_REPOSITORY:-}" ]; then
  status replication=running
  log "replicating encrypted backup with restic"
  if ! restic backup --tag devhub --tag "devhub-$STAMP" "$DEST"; then
    status status=failed replication=failed
    exit 1
  fi
  status replication=ok
fi

# ---- Retention: newest backup per day for KEEP_DAILY days + newest per ISO week for KEEP_WEEKLY weeks
if [ $RETENTION = 1 ]; then
  python3 - "$BACKUP_DIR" "$KEEP_DAILY" "$KEEP_WEEKLY" <<'PY'
import os, re, sys, shutil, datetime as dt
root, kd, kw = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
sets = sorted([d for d in os.listdir(root) if re.match(r"^\d{8}-\d{6}$", d)], reverse=True)
keep, days, weeks = set(), [], []
for s in sets:
    t = dt.datetime.strptime(s, "%Y%m%d-%H%M%S"); day = t.date(); wk = t.isocalendar()[:2]
    if day not in days and len(days) < kd: days.append(day); keep.add(s)
    if wk not in weeks and len(weeks) < kw: weeks.append(wk); keep.add(s)
for s in sets:
    if s not in keep:
        shutil.rmtree(os.path.join(root, s)); print(f"[backup] retention: removed {s}")
print(f"[backup] retention: keeping {len(keep)} set(s) (daily={kd}, weekly={kw})")
PY
fi
status status=ok finished_at="$(date '+%Y-%m-%d %H:%M %Z')" path="$DEST" size="$SIZE" seconds="$EL" with_vms="$WITH_VMS"
