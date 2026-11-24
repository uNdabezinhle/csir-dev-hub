#!/usr/bin/env bash
# Public HTTPS access to the CSIR Dev Hub PoC via Cloudflare *quick* tunnels (trycloudflare.com).
#   ./scripts/tunnel.sh up       start tunnels, write public.env, repoint Coder/Keycloak/portal/Grafana, add OIDC redirect URIs
#   ./scripts/tunnel.sh down     stop tunnels, remove public.env, put services back on local (HOST_IP) URLs
#   ./scripts/tunnel.sh status   show tunnel processes + current public URLs
# Quick-tunnel hostnames are random and CHANGE every time the tunnels restart ("up" re-applies everything).
# For stable hostnames use a named tunnel (docs/public-access.md). Never prints secrets.
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
export PATH="$PWD/tools-bin:$PATH"
set -a; . ./.env; set +a
LOG=logs-tunnel; mkdir -p "$LOG"
# name:local-port
SERVICES=("portal:${ADMIN_CONSOLE_PORT:-8088}" "coder:${CODER_PORT:-3000}" "keycloak:${KEYCLOAK_PORT:-8080}"
          "grafana:${GRAFANA_PORT:-3001}" "guacamole:${GUACAMOLE_PORT:-8089}")

stop_tunnels() {
  for f in "$LOG"/*.pid; do [ -f "$f" ] || continue; kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done
  pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:" 2>/dev/null || true
}

start_tunnels() {
  command -v cloudflared >/dev/null || { echo "cloudflared not installed (see docs/public-access.md)"; exit 1; }
  stop_tunnels
  umask 077; : > public.env.tmp
  # Start one at a time: trycloudflare rate-limits bursts of quick-tunnel requests (HTTP 429) -> retry with backoff.
  for s in "${SERVICES[@]}"; do
    n=${s%%:*}; p=${s##*:}; url=""
    for attempt in 1 2 3 4 5; do
      nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$p" >"$LOG/$n.log" 2>&1 &
      echo $! > "$LOG/$n.pid"
      for _ in $(seq 1 40); do
        url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG/$n.log" | head -1 || true)
        [ -n "$url" ] && break
        grep -q 'provisioning failed' "$LOG/$n.log" && break; sleep 1
      done
      [ -n "$url" ] && break
      kill "$(cat "$LOG/$n.pid")" 2>/dev/null || true; sleep $((attempt * 10))
    done
    [ -n "$url" ] || { echo "no URL for $n (see $LOG/$n.log)"; stop_tunnels; rm -f public.env.tmp; exit 1; }
    # The hostname is only useful once cloudflared has a live connection to the Cloudflare edge (TCP/UDP 7844 egress).
    for _ in $(seq 1 30); do grep -q 'Registered tunnel connection' "$LOG/$n.log" && break; sleep 1; done
    grep -q 'Registered tunnel connection' "$LOG/$n.log" || {
      echo "cloudflared for $n got $url but cannot reach the Cloudflare edge:"; grep -m1 'ERR' "$LOG/$n.log" | sed 's/^/  /'
      echo "outbound port 7844 (TCP+UDP) to *.argotunnel.com must be allowed. Nothing was changed; stack stays in local mode."
      stop_tunnels; rm -f public.env.tmp; exit 1; }
    echo "PUBLIC_$(echo "$n" | tr a-z A-Z)_URL=$url" >> public.env.tmp
    sleep 3
  done
  . ./public.env.tmp
  # OIDC issuer must be the URL browsers see; containers reach it via outbound HTTPS through the tunnel.
  echo "OIDC_ISSUER_URL=$PUBLIC_KEYCLOAK_URL/realms/csir" >> public.env.tmp
  mv public.env.tmp public.env
  # wait for DNS of the new hostnames to resolve publicly
  for _ in $(seq 1 60); do
    ok=1; while IFS='=' read -r k v; do [[ $k == PUBLIC_*_URL ]] || continue
      curl -s -o /dev/null -m 10 "$v/" || ok=0; done < public.env
    [ $ok = 1 ] && break; sleep 3
  done
}

apply_config() {
  echo "==> recreating keycloak, coder, portal, grafana with $( [ -f public.env ] && echo public || echo local ) URLs"
  ./scripts/dc.sh up -d keycloak coder portal grafana >"$LOG/compose.log" 2>&1 || { tail -20 "$LOG/compose.log"; exit 1; }
  for _ in $(seq 1 100); do curl -sf -o /dev/null "http://127.0.0.1:${KEYCLOAK_PORT:-8080}/realms/csir" && break; sleep 3; done
  echo "==> Keycloak client redirect URIs"
  python3 scripts/public-access.py redirects
  for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:${CODER_PORT:-3000}/healthz" && break; sleep 2; done
}

case "${1:-status}" in
  up)
    start_tunnels; apply_config
    echo; echo "Public URLs (quick tunnels; change on restart):"; grep '^PUBLIC_' public.env | sed 's/^PUBLIC_/  /' ;;
  down)
    stop_tunnels; rm -f public.env; apply_config; echo "tunnels stopped; local URLs restored" ;;
  status)
    for s in "${SERVICES[@]}"; do n=${s%%:*}; f="$LOG/$n.pid"
      if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then echo "  $n: running (pid $(cat "$f"))"; else echo "  $n: stopped"; fi; done
    [ -f public.env ] && grep '^PUBLIC_' public.env | sed 's/^PUBLIC_/  /' || echo "  (no public.env: local mode)" ;;
  *) echo "usage: $0 up|down|status"; exit 2 ;;
esac
