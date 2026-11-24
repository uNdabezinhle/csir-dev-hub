#!/usr/bin/env bash
# Account-free public HTTPS access via Pinggy (SSH reverse tunnels carried inside TLS on port 443).
#   ./scripts/tunnel-pinggy.sh up       start a background supervisor that keeps 4 tunnels alive (portal, Coder, Grafana, Guacamole)
#   ./scripts/tunnel-pinggy.sh down     stop supervisor + tunnels, remove public.env, recreate portal on local URLs
#   ./scripts/tunnel-pinggy.sh status   LIVE health of every public URL (dns.google DoH + HTTP to the public IP)
# Health: every 60 s (HEALTH_INTERVAL) the supervisor resolves each hostname via public DNS-over-HTTPS and requests it
# through the resolved public IP. A dead ssh process or a failed check (re-checked once after 10 s) reconnects that
# tunnel, rewrites public.env, recreates the portal (lite mode) and logs it. Current URLs + health are written to
# logs-tunnel/public-urls.json and copied into the portal (GET /public-urls, signed-in users).
# Free Pinggy tunnels EXPIRE AFTER 60 MINUTES and get a NEW random hostname on reconnect. The supervisor reconnects,
# re-verifies every URL from outside, rewrites public.env and recreates the portal so its links follow.
# Lite mode: Coder/Keycloak configs are never changed (see docker-compose.public-lite.yml). Never prints secrets.
# Pinggy account: if PINGGY_TOKEN is set in secrets/devhub.env, the services in PINGGY_ACCOUNT_SERVICES (default: portal)
# connect as that account (visible at dashboard.pinggy.io); the rest stay anonymous. A FREE account allows ONE active
# tunnel per token, so on free only the portal is account-bound, and it logs in as TOKEN+force so a stale session can
# never lock out a reconnect (note: this also closes any other tunnel the user runs with the same token).
# Pro: set PINGGY_ACCOUNT_SERVICES="portal coder grafana guacamole" (multiple tunnels/token; +force is then not used).
# The token is passed via a chmod-600 ssh config in secrets/ and an empty-password askpass; never on the command line.
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
set -a; . ./.env; set +a
LOG=logs-tunnel; mkdir -p "$LOG"
PINGGY_TOKEN=$(grep -E '^PINGGY_TOKEN=' secrets/devhub.env 2>/dev/null | cut -d= -f2- || true)
ACCOUNT_SERVICES=${PINGGY_ACCOUNT_SERVICES:-portal}
SSH_CONF=secrets/pinggy-ssh.conf
if [ -n "$PINGGY_TOKEN" ]; then
  force=""; [ "$(echo $ACCOUNT_SERVICES | wc -w)" = 1 ] && force="+force"
  ( umask 077; printf 'Host a.pinggy.io\n  User %s%s\n' "$PINGGY_TOKEN" "$force" > "$SSH_CONF" )
fi
unset PINGGY_TOKEN
is_account() { [ -f "$SSH_CONF" ] && [[ " $ACCOUNT_SERVICES " == *" $1 "* ]]; }
SERVICES=("portal:${ADMIN_CONSOLE_PORT:-8088}:/login" "coder:${CODER_PORT:-3000}:/healthz"
          "grafana:${GRAFANA_PORT:-3001}:/api/health" "guacamole:${GUACAMOLE_PORT:-8089}:/guacamole/")
SSH_OPTS=(-T -p 443 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3
          -o ExitOnForwardFailure=yes -o "ProxyCommand=openssl s_client -quiet -connect a.pinggy.io:443 -servername a.pinggy.io 2>/dev/null")

alive() { [ -f "$LOG/pinggy-$1.pid" ] && kill -0 "$(cat "$LOG/pinggy-$1.pid")" 2>/dev/null; }
url_of() { grep -oE 'https://[a-z0-9.-]+\.(free\.pinggy\.net|pinggy\.link|pinggy\.online)' "$LOG/pinggy-$1.log" 2>/dev/null | tail -1 || true; }
account_of() {  # name -> "anonymous" | "free:<email>" | "pro:<email>" (from the tunnel's own banner)
  local f="$LOG/pinggy-$1.log" who
  who=$(grep -oE 'authenticated as [^ ]+' "$f" 2>/dev/null | tail -1 | cut -d' ' -f3 || true)
  [ -n "$who" ] || { echo anonymous; return; }
  grep -q 'expire in 60 minutes' "$f" 2>/dev/null && echo "free:$who" || echo "pro:$who"
}

start_one() {  # name port
  local n=$1 p=$2 u=""
  for attempt in 1 2 3; do
    if is_account "$n"; then
      SSH_ASKPASS="$PWD/scripts/pinggy-askpass.sh" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
        setsid nohup ssh -F "$SSH_CONF" "${SSH_OPTS[@]}" -R "0:localhost:$p" a.pinggy.io >"$LOG/pinggy-$n.log" 2>&1 </dev/null &
    else
      setsid nohup ssh "${SSH_OPTS[@]}" -R "0:localhost:$p" a.pinggy.io >"$LOG/pinggy-$n.log" 2>&1 </dev/null &
    fi
    echo $! > "$LOG/pinggy-$n.pid"
    for _ in $(seq 1 30); do u=$(url_of "$n"); [ -n "$u" ] && return 0; alive "$n" || break; sleep 1; done
    # Pinggy echoes the token in some rejection banners -> redact the (now closed) log before anything reads it.
    if is_account "$n" && ! alive "$n"; then sed -i -E 's/token \([^)]*\)/token ([token])/g' "$LOG/pinggy-$n.log"; fi
    if is_account "$n" && grep -qiE 'not allowed|invalid|Permission denied|authentication' "$LOG/pinggy-$n.log"; then
      log "$n: Pinggy rejected the account login: $(grep -m1 -iE 'not allowed|invalid|denied|token' "$LOG/pinggy-$n.log" | sed 's/([^)]*)/([token])/')"
    fi
    kill "$(cat "$LOG/pinggy-$n.pid")" 2>/dev/null || true; sleep $((attempt * 5))
  done
  echo "$(date '+%F %T') pinggy $n: no URL; last log lines:"; tail -3 "$LOG/pinggy-$n.log"; return 1
}

# ---- health: resolve the hostname through PUBLIC DNS (Google DoH), then make the HTTP request to that public IP
# (curl --resolve) so neither the local resolver nor any local shortcut can make a dead tunnel look alive.
public_ip() {  # host -> last A record from dns.google (empty on NXDOMAIN / no answer)
  curl -s -m 10 "https://dns.google/resolve?name=$1&type=A" 2>/dev/null | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
a = [x["data"] for x in d.get("Answer", []) if x.get("type") == 1]
print(a[-1] if d.get("Status") == 0 and a else "")' 2>/dev/null || true
}
health() {  # url path -> prints "ok <ip> <code>" or "fail <reason>"; exit 0 only when healthy
  local u=$1 path=$2 h ip c
  [ -n "$u" ] || { echo "fail no-url"; return 1; }
  h=${u#https://}; ip=$(public_ip "$h")
  [ -n "$ip" ] || { echo "fail dns:NXDOMAIN/no-A"; return 1; }
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 20 --resolve "$h:443:$ip" "$u$path" 2>/dev/null || true)
  [[ $c =~ ^[23] ]] || { echo "fail http:${c:-000} ip:$ip"; return 1; }
  echo "ok $ip $c"
}
log() { echo "$(date '+%F %T') $*"; }


write_status() {  # logs-tunnel/public-urls.json (+ a copy in the portal for GET /public-urls)
  local rows="" s n p path u r
  for s in "${SERVICES[@]}"; do IFS=: read -r n p path <<<"$s"; u=$(url_of "$n")
    if alive "$n"; then r=$(health "$u" "$path") || true; else r="fail ssh-exited"; fi
    rows+="$n|$u|$r $(account_of "$n")"$'\n'; done
  printf '%s' "$rows" | python3 -c '
import json, sys, datetime
svc = {}
for line in sys.stdin.read().splitlines():
    n, u, r = line.split("|", 2); f = r.split()
    acct = f.pop()
    svc[n] = {"url": u, "healthy": f[0] == "ok", "public_ip": f[1] if f[0] == "ok" else None, "pinggy_account": acct,
              "http": f[2] if f[0] == "ok" else None, "error": None if f[0] == "ok" else " ".join(f[1:])}
print(json.dumps({"provider": "pinggy", "mode": "lite", "checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "check": "dns.google DoH + HTTP to the public IP", "services": svc}, indent=1))' > "$LOG/public-urls.json.tmp" \
    && mv "$LOG/public-urls.json.tmp" "$LOG/public-urls.json"
  docker cp -q "$LOG/public-urls.json" csir-devhub-portal-1:/data/public-urls.json 2>/dev/null || true
}

reconnect() {  # name port path -> new tunnel, waits (<=90 s) until it resolves publicly and answers
  local n=$1 p=$2 path=$3 old r
  old=$(url_of "$n")
  if alive "$n"; then   # hung tunnels may ignore SIGTERM (e.g. stopped) -> SIGKILL
    kill "$(cat "$LOG/pinggy-$n.pid")" 2>/dev/null; sleep 2; alive "$n" && kill -9 "$(cat "$LOG/pinggy-$n.pid")" 2>/dev/null
  fi
  start_one "$n" "$p" || return 1
  for _ in $(seq 1 18); do r=$(health "$(url_of "$n")" "$path") && { log "$n reconnected: ${old:-none} -> $(url_of "$n") ($r)"; return 0; }; sleep 5; done
  log "$n reconnected to $(url_of "$n") but not publicly healthy yet ($r)"; return 0
}

write_and_apply() {
  umask 077; { echo "PUBLIC_MODE=lite"; echo "PUBLIC_PROVIDER=pinggy"; echo "PUBLIC_SINCE=$(date -Iseconds)"
    for s in "${SERVICES[@]}"; do n=${s%%:*}; echo "PUBLIC_$(echo "$n" | tr a-z A-Z)_URL=$(url_of "$n")"; done; } > public.env.tmp
  mv public.env.tmp public.env
  if ./scripts/dc.sh up -d portal >"$LOG/compose.log" 2>&1; then
    for _ in $(seq 1 30); do health "$(url_of portal)" /login >/dev/null && { log "applied: $(grep -c '^PUBLIC_.*_URL' public.env) public URLs, portal links updated"; return 0; }; sleep 2; done
  fi
  log "portal did not come back in public mode -> reverting to local"
  rm -f public.env; ./scripts/dc.sh up -d portal >>"$LOG/compose.log" 2>&1 || true; return 1
}

applied_urls() { [ -f public.env ] && grep -E '^PUBLIC_[A-Z]+_URL=' public.env | cut -d= -f2- | tr '\n' ' ' || true; }
current_urls() { local s; for s in "${SERVICES[@]}"; do printf '%s ' "$(url_of "${s%%:*}")"; done; }

supervise() {  # every HEALTH_INTERVAL (60 s): ssh alive? public DNS? HTTP through the public IP? -> reconnect the broken one
  local interval=${HEALTH_INTERVAL:-60} r
  set +e   # a long-running loop: one failed command must never kill the supervisor
  log "supervisor started (health check every ${interval}s: dns.google + HTTP via public IP)"
  while true; do
    for s in "${SERVICES[@]}"; do
      IFS=: read -r n p path <<<"$s"
      if ! alive "$n"; then
        log "$n: ssh process not running -> reconnect"; reconnect "$n" "$p" "$path" || log "$n: reconnect failed"; continue
      fi
      if ! r=$(health "$(url_of "$n")" "$path"); then
        sleep 10   # one quick re-check before tearing down (transient DoH/HTTP blips)
        if ! r=$(health "$(url_of "$n")" "$path"); then
          log "$n: $(url_of "$n") unhealthy ($r) -> reconnect"; reconnect "$n" "$p" "$path" || log "$n: reconnect failed"
        fi
      fi
    done
    cur=$(current_urls)
    if [[ "$cur" != *"  "* && "$cur" != " "* ]] && [ "$cur" != "$(applied_urls)" ]; then write_and_apply || true; fi
    write_status
    sleep "$interval"
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
    for _ in $(seq 1 80); do grep -q 'applied:' "$LOG/pinggy-supervisor.log" 2>/dev/null && break; sleep 3; done
    "$0" status ;;
  _supervise) supervise ;;
  down)
    stop_all
    if [ -f public.env ]; then rm -f public.env; ./scripts/dc.sh up -d portal >"$LOG/compose.log" 2>&1; fi
    echo "pinggy tunnels stopped; portal back on local URLs" ;;
  status)   # LIVE check of every URL (public DNS + HTTP via the public IP), not just what was recorded
    alive supervisor && echo "  supervisor: running" || echo "  supervisor: stopped"
    for s in "${SERVICES[@]}"; do IFS=: read -r n p path <<<"$s"; u=$(url_of "$n")
      if ! alive "$n"; then echo "  $n: DOWN (ssh not running) ${u}"; continue; fi
      if r=$(health "$u" "$path"); then echo "  $n: $u  LIVE (public DNS ${r#ok }; HTTP via public IP) [$(account_of "$n")]"; else echo "  $n: $u  DEAD ($r) [$(account_of "$n")]"; fi
    done
    [ -f public.env ] && grep -E '^PUBLIC_(MODE|SINCE)' public.env | sed 's/^/  /' || echo "  (no public.env: local mode)"
    [ -f "$LOG/public-urls.json" ] && echo "  last supervisor check: $(python3 -c 'import json;print(json.load(open("'"$LOG"'/public-urls.json"))["checked_at"])')" || true ;;
  *) echo "usage: $0 up|down|status"; exit 2 ;;
esac
