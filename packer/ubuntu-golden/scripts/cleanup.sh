#!/bin/bash
# Make the image generic: no machine-id, SSH host keys, logs, apt caches or build user left behind.
set -eux
apt-get -y autoremove --purge; apt-get clean; rm -rf /var/lib/apt/lists/*
truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*
find /var/log -type f -exec truncate -s 0 {} \;
# the temporary build user is removed on first boot of the published image (cannot delete the logged-in user here)
cat > /etc/cloud/cloud.cfg.d/99-csir-remove-build-user.cfg <<'CFG'
bootcmd:
  - [ sh, -c, 'id packer >/dev/null 2>&1 && userdel -r -f packer || true' ]
CFG
sync
