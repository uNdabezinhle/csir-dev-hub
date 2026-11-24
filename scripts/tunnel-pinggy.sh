#!/usr/bin/env bash
# Account-free public HTTPS access via Pinggy (SSH reverse tunnels carried inside TLS on port 443).
#   ./scripts/tunnel-pinggy.sh up       start a background supervisor that keeps 4 tunnels alive (portal, Coder, Grafana, Guacamole)
#   ./scripts/tunnel-pinggy.sh down     stop supervisor + tunnels, remove public.env, recreate portal on local URLs
#   ./scripts/tunnel-pinggy.sh status   show tunnels + current public URLs
# Free Pinggy tunnels EXPIRE AFTER 60 MINUTES and get a NEW random hostname on reconnect. The supervisor reconnects,
# re-verifies every URL from outside, rewrites public.env and recreates the portal so its links follow.
# Lite mode: Coder/Keycloak configs are never changed (see docker-compose.public-lite.yml). Never prints secrets.
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
set -a; . ./.env; set +a
LOG=logs-tunnel; mkdir -p "$LOG"
SERVICES=("portal:${ADMIN_CONSOLE_PORT:-8088}:/login" "coder:${CODER_PORT:-3000}:/healthz"
          "grafana:${GRAFANA_PORT:-3001}:/api/health" "guacamole:${GUACAMOLE_PORT:-8089}:/guacamole/")
SSH_OPTS=(-T -p 443 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3
          -o ExitOnForwardFailure=yes -o "ProxyCommand=openssl s_client -quiet -connect a.pinggy.io:443 -servername a.pinggy.io 2>/dev/null")

alive() { [ -f "$LOG/pinggy-$1.pid" ] && kill -0 "$(cat "$LOG/pinggy-$1.pid")" 2>/dev/null; }
url_of() { grep -oE 'https://[a-z0-9-]+\.free\.pinggy\.net' "$LOG/pinggy-$1.log" 2>/dev/null | tail -1 || true; }

start_one() {  # name port
  local n=$1 p=$2 u=""
  for attempt in 1 2 3; do
    setsid nohup ssh "${SSH_OPTS[@]}" -R "0:localhost:$p" a.pinggy.io >"$LOG/pinggy-$n.log" 2>&1 </dev/null &
    echo $! > "$LOG/pinggy-$n.pid"
    for _ in $(seq 1 30); do u=$(url_of "$n"); [ -n "$u" ] && return 0; alive "$n" || break; sleep 1; done
    kill "$(cat "$LOG/pinggy-$n.pid")" 2>/dev/null || true; sleep $((attempt * 5))
  done
  echo "$(date '+%F %T') pinggy $n: no URL; last log lines:"; tail -3 "$LOG/pinggy-$n.log"; return 1
}

reachable() {  # url -> HTTP < 400 from outside (non-browser UA skips Pinggy's interstitial)
  local c; c=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$1" || true); [[ $c =~ ^[23] ]]
}

write_and_apply() {
  umask 077; { echo "PUBLIC_MODE=lite"; echo "PUBLIC_PROVIDER=pinggy"; echo "PUBLIC_SINCE=$(date -Iseconds)"
    for s in "${SERVICES[@]}"; do n=${s%%:*}; echo "PUBLIC_$(echo "$n" | tr a-z A-Z)_URL=$(url_of "$n")"; done; } > public.env.tmp
  mv public.env.tmp public.env
  if ./scripts/dc.sh up -d portal >"$LOG/compose.log" 2>&1; then
    for _ in $(seq 1 30); do reachable "$(url_of portal)/login" && { echo "$(date '+%F %T') applied: $(grep -c '^PUBLIC_.*_URL' public.env) public URLs"; return 0; }; sleep 2; done
  fi
  echo "$(date '+%F %T') portal did not come back in public mode -> reverting to local"
  rm -f public.env; ./scripts/dc.sh up -d portal >>"$LOG/compose.log" 2>&1 || true; return 1
}

supervise() {
  local last=""
  while true; do
    for s in "${SERVICES[@]}"; do
      IFS=: read -r n p path <<<"$s"
      alive "$n" || { echo "$(date '+%F %T') (re)starting $n tunnel"; start_one "$n" "$p" || true; }
    done
    # Guard: only (re)apply when EVERY tunnel is connected and answers from outside.
    cur=""; ok=1
    for s in "${SERVICES[@]}"; do IFS=: read -r n p path <<<"$s"; u=$(url_of "$n"); cur+="$u "
      [ -n "$u" ] || ok=0; done
    if [ "$ok" = 1 ] && [ "$cur" != "$last" ]; then
      for s in "${SERVICES[@]}"; do IFS=: read -r n p path <<<"$s"; reachable "$(url_of "$n")$path" || { ok=0; echo "$(date '+%F %T') $n not reachable yet"; }; done
      if [ "$ok" = 1 ]; then write_and_apply && last="$cur"; fi
    fi
    sleep 15
  done
}

stop_all() {
  [ -f "$LOG/pinggy-supervisor.pid" ] && kill "$(cat "$LOG/pinggy-supervisor.pid")" 2>/dev/null || true
  for s in "${SERVICES[@]}"; do n=${s%%:*}; alive "$n" && kill "$(cat "$LOG/pinggy-$n.pid")" 2>/dev/null || true; rm -f "$LOG/pinggy-$n.pid"; done
  rm -f "$LOG/pinggy-supervisor.pid"
}

case "${1:-status}" in
  up)
    stop_all
    setsid nohup "$0" _supervise >"$LOG/pinggy-supervisor.log" 2>&1 </dev/null &
    echo $! > "$LOG/pinggy-supervisor.pid"
    for _ in $(seq 1 60); do grep -q 'applied:' "$LOG/pinggy-supervisor.log" 2>/dev/null && break; sleep 3; done
    "$0" status ;;
  _supervise) supervise ;;
  down)
    stop_all
    if [ -f public.env ]; then rm -f public.env; ./scripts/dc.sh up -d portal >"$LOG/compose.log" 2>&1; fi
    echo "pinggy tunnels stopped; portal back on local URLs" ;;
  status)
    alive supervisor && echo "  supervisor: running" || echo "  supervisor: stopped"
    for s in "${SERVICES[@]}"; do n=${s%%:*}; alive "$n" && echo "  $n: $(url_of "$n")" || echo "  $n: stopped"; done
    [ -f public.env ] && grep -E '^PUBLIC_(MODE|SINCE)' public.env | sed 's/^/  /' || echo "  (no public.env: local mode)" ;;
  *) echo "usage: $0 up|down|status"; exit 2 ;;
esac
