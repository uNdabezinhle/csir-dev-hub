#!/usr/bin/env bash
# Download one encrypted restic snapshot and run the Dev Hub restore checks.
set -euo pipefail
cd "$(dirname "$0")/.."
stamp=${1:?usage: verify-offhost-backup.sh YYYYMMDD-HHMMSS}
[[ "$stamp" =~ ^[0-9]{8}-[0-9]{6}$ ]] || { echo "Invalid backup timestamp" >&2; exit 2; }
: "${RESTIC_REPOSITORY:?Set RESTIC_REPOSITORY to the configured off-host repository}"
command -v restic >/dev/null || { echo "restic is required" >&2; exit 2; }
umask 077
download=$(mktemp -d -t devhub-restore.XXXXXXXX)
trap 'rm -rf -- "$download"' EXIT
restic restore latest --tag "devhub-$stamp" --target "$download" --verify
mapfile -t manifests < <(find "$download" -type f -path "*/$stamp/manifest.json")
if [ "${#manifests[@]}" -ne 1 ]; then
  echo "Expected one backup manifest in the restored snapshot" >&2
  exit 1
fi
bash scripts/restore.sh "${manifests[0]%/manifest.json}" --verify
