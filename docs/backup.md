# Backup and restore

## What is backed up (`scripts/backup.sh`)
| Component | How | File |
|---|---|---|
| PostgreSQL: `coder`, `keycloak`, `guacamole_db` | `pg_dump -Fc` per DB and `pg_dumpall --globals-only` (roles) | `postgres/<db>.dump`, `postgres/globals.sql` |
| Portal DB (users, projects, quotas, licence seats, scans, approvals, audit) | SQLite online backup API in the portal container | `portal/devhub.db` |
| LDAP | `slapcat -n0` (config) and `-n1` (data) | `ldap/config.ldif`, `ldap/data.ldif` |
| Docker named volumes | `tar` through a throw-away alpine container: Coder home, Grafana, Guacamole recordings, Prometheus, workspace homes, secret volumes. VM disks (`libvirt-data`) only with `--with-vms` | `volumes/<volume>.tar.gz` |
| Configuration | compose files, `.env`, `public.env`, `stack/`, `identity/`, `templates/`, `scripts/`, `packer/` (no build outputs or ISOs), `ops/` | `config/config.tar.gz` |
| Secrets | `secrets/` and each `csir-secrets-*` volume, encrypted with AES-256-CBC + PBKDF2. The backup requires `BACKUP_PASSPHRASE` from the job environment or `secrets/devhub.env`. | `config/secrets.tar.gz.enc`, `volumes/csir-secrets-*.tar.gz.enc` |

Each run writes `manifest.json` (sha256 and size per file) and updates `$BACKUP_DIR/status.json`. The status is
copied into the portal, which shows it in Admin → KVM / Backups and at `/api/backup/status`.

Options: `--with-vms` (also VM disks), `--no-volumes` (databases and config only), `--no-retention`.

## Retention
`BACKUP_KEEP_DAILY` (default 7) and `BACKUP_KEEP_WEEKLY` (default 4) in `.env`. The newest backup of each of the last
7 days is kept, plus the newest backup of each of the last 4 ISO weeks. Everything else under `BACKUP_DIR` is pruned
after a successful run.

## Off-host replication and recovery

Provision a restic repository on a separate host or object store and set `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`
and any backend credentials in the backup job environment. Keep access to both the repository password and the
archive passphrase outside the backup host. Restic encrypts the entire replicated snapshot. The local backup still
contains database dumps and workspace data, so keep `BACKUP_DIR` on restricted storage.

When configured, `backup.sh` replicates the completed backup before local retention runs and reports replication in
`status.json`. Configure remote retention independently. To verify recovery, select a timestamp from
`restic snapshots --tag devhub` and run `bash scripts/verify-offhost-backup.sh YYYYMMDD-HHMMSS` against the
verification stack. The script downloads the snapshot to a temporary directory, verifies its contents, runs the same
restore checks as a local backup, and removes the downloaded files.

## Replacement-host recovery drill

Before the drill, assign a recovery owner and an evidence reviewer. Agree the recovery time objective and the maximum
acceptable data loss with the service owner. Record both in the release evidence. Do not use the live hub for a
destructive restore drill.

1. Provision a compatible verification host. Recover the repository credentials and archive passphrase through the
   approved credential store. Download the selected snapshot and verify its manifest and archives.
2. Restore release configuration and secrets, including the portal encryption key. Start only the dependencies needed
   to recover the data. Restore PostgreSQL roles and databases, LDAP data and configuration, and named volumes before
   starting their consumers. Use the dedicated portal SQLite backup for the portal database. Restore VM volumes only
   if the selected backup included them. The component restore commands below need their target containers or volumes
   to exist.
3. Start the platform and verify identity, workspace access, secret injection and reconciliation. Record the snapshot
   timestamp, elapsed recovery time, any data loss, excluded volumes, and the reviewer’s sign-off.

The `config` restore component extracts configuration only. To recover the encrypted secrets archive, load
`BACKUP_PASSPHRASE` into the drill shell through the credential store and extract to a private staging directory:

```bash
umask 077
stage=$(mktemp -d)
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE \
  -in "$SNAPSHOT/config/secrets.tar.gz.enc" | tar xzf - -C "$stage"
```

Here `SNAPSHOT` is the verified backup directory. Review staged files before transferring them to the replacement
host. Apply the deployment’s secret file permissions and remove the staging directory when finished.

A disposable restic round trip has been verified. Recovery from the real off-host repository remains open until a
destination is configured and a drill with Dev Hub data passes. Keep the recovery targets and evidence with the
[release acceptance record](upgrade-acceptance.md#verify-before-sign-off).

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
