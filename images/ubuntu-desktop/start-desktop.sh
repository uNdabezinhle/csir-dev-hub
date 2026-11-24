#!/usr/bin/env bash
# Starts the desktop services inside a CSIR Dev Hub desktop workspace.
# RDP password for the 'coder' user comes from $CSIR_RDP_PASSWORD (injected by the template).
set -u
[ -f ~/.xsession ] || echo xfce4-session > ~/.xsession
if [ -n "${CSIR_RDP_PASSWORD:-}" ]; then echo "coder:${CSIR_RDP_PASSWORD}" | sudo chpasswd; fi
# xrdp (RDP on 3389) - used by Guacamole and native RDP clients (via coder port-forward)
sudo mkdir -p /var/run/xrdp && sudo chown xrdp /var/run/xrdp 2>/dev/null || true
sudo /usr/sbin/xrdp-sesman >/tmp/xrdp-sesman.log 2>&1 || true
sudo /usr/sbin/xrdp >/tmp/xrdp.log 2>&1 || true
# VNC + noVNC (browser desktop on 6080)
mkdir -p ~/.vnc
cat > ~/.vnc/xstartup <<'X'
#!/bin/sh
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS
exec dbus-launch --exit-with-session startxfce4
X
chmod +x ~/.vnc/xstartup
vncserver -kill :1 >/dev/null 2>&1 || true
vncserver :1 -geometry 1600x900 -depth 24 -localhost yes -SecurityTypes None --I-KNOW-THIS-IS-INSECURE >/tmp/vnc.log 2>&1
websockify --web /usr/share/novnc 6080 localhost:5901 >/tmp/novnc.log 2>&1 &
