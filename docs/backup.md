# Backup and restore

## What is backed up (`scripts/backup.sh`)
| Component | How | File |
|---|---|---|
| PostgreSQL: `coder`, `keycloak`, `guacamole_db` | `pg_dump -Fc` per DB and `pg_dumpall --globals-only` (roles) | `postgres/<db>.dump`, `postgres/globals.sql` |
| Portal DB (users, projects, quotas, licence seats, scans, approvals, audit) | SQLite online backup API in the portal container | `portal/devhub.db` |
| LDAP | `slapcat -n0` (config) and `-n1` (data) | `ldap/config.ldif`, `ldap/data.ldif` |
| Docker named volumes | `tar` through a throw-away alpine container: Coder home, Grafana, Guacamole recordings, Prometheus, workspace homes, secret volumes. VM disks (`libvirt-data`) only with `--with-vms` | `volumes/<volume>.tar.gz` |
| Configuration | compose files, `.env`, `public.env`, `stack/`, `identity/`, `templates/`, `scripts/`, `packer/` (no build outputs or ISOs), `ops/` | `config/config.tar.gz` |
| Secrets | `secrets/`, AES-256-CBC + PBKDF2 encrypted with `BACKUP_PASSPHRASE` from `secrets/devhub.env` (skipped with a warning if it is unset) | `config/secrets.tar.gz.enc` |

Each run writes `manifest.json` (sha256 and size per file) and updates `$BACKUP_DIR/status.json`. The status is
copied into the portal, which shows it in Admin → KVM / Backups and at `/api/backup/status`.

Options: `--with-vms` (also VM disks), `--no-volumes` (databases and config only), `--no-retention`.

## Retention
`BACKUP_KEEP_DAILY` (default 7) and `BACKUP_KEEP_WEEKLY` (default 4) in `.env`. The newest backup of each of the last
7 days is kept, plus the newest backup of each of the last 4 ISO weeks. Everything else under `BACKUP_DIR` is pruned
after a successful run. Copy `BACKUP_DIR` off-host (for example `rclone sync` or `restic` to the CSIR object store);
the PoC keeps it on the same disk.

## Verify / test restore (`scripts/restore.sh`)
```
scripts/restore.sh --list
scripts/restore.sh latest --verify        # non-destructive
```
`--verify` checks every manifest sha256, then:
- restores every DB dump into a scratch database `restore_verify_<db>` and compares the table count and the row counts of key
  tables with the live DB
- restores the portal SQLite file and runs `PRAGMA integrity_check`
- compares the LDAP LDIF entry count with the live directory
- test-reads every volume tarball and test-decrypts the secrets archive
- drops the scratch DBs

Real restore (destructive, one component at a time; stop the consumer first):
```
./scripts/dc.sh stop coder
scripts/restore.sh latest --restore postgres:coder --yes
./scripts/dc.sh start coder
```
Components: `postgres:<db>`, `portal`, `ldap`, `volume:<name>`, `config`.

## Scheduling
The PoC box has no systemd or cron (the container's PID 1 is `tini`), so run backups by hand there. On a real host,
use either of the following.

**systemd** (`ops/systemd/`): edit the paths, then
```
sudo cp ops/systemd/csir-devhub-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now csir-devhub-backup.timer
systemctl list-timers csir-devhub-backup.timer
```
**cron**:
```
30 1 * * * cd /opt/csir-devhub && scripts/backup.sh >> /var/log/csir-devhub-backup.log 2>&1 && scripts/restore.sh latest --verify >> /var/log/csir-devhub-backup.log 2>&1
```
