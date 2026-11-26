#!/usr/bin/env python3
"""Brokered-RDP end-to-end check: portal -> Guacamole (JSON auth) -> guacd -> xrdp in an ubuntu-desktop workspace
(container) or in a VM workspace (ubuntu-vm / windows-kvm: guacd -> DNAT on the libvirt host, reachable only from guacd).
Logs into the portal (local admin), requests the 'Desktop (RDP)' link for a running desktop workspace, exchanges the
signed token at Guacamole's REST API, opens the Guacamole WebSocket tunnel and waits for the RDP session to draw frames.
Also asserts that no host port publishes 3389. Usage: scripts/test-rdp.py [workspace-name] (default demo-desktop)
Requires: pip install websocket-client"""
import os, re, sys, json, time, subprocess, urllib.request, urllib.parse, http.cookiejar
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from devhub_testlib import cookiejar  # noqa: E402
import websocket
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
S = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/secrets/devhub.env") if "=" in l and not l.startswith("#"))
E = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/.env") if "=" in l and not l.startswith("#"))
H = E["HOST_IP"]; P = f"http://{H}:{E['ADMIN_CONSOLE_PORT']}"; G = f"http://{H}:{E.get('GUACAMOLE_PORT', '8089')}/guacamole"
name = sys.argv[1] if len(sys.argv) > 1 else "demo-desktop"; results = []
def check(n, ok, extra=""): results.append(ok); print(("PASS " if ok else "FAIL ") + n + (f"  [{extra}]" if extra and not ok else ""))
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None
jar = cookiejar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
with op.open(f"{P}/api/csrf") as response:
    csrf = json.load(response)["token"]
op.open(f"{P}/login", urllib.parse.urlencode({"username": "admin", "password": S["CODER_ADMIN_PASSWORD"], "csrf_token": csrf}).encode()).read()
ws = next(w for w in json.loads(op.open(f"{P}/api/workspaces").read()) if w["name"] == name)
nr = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
try: nr.open(f"{P}/workspaces/{ws['id']}/rdp"); loc = ""
except urllib.error.HTTPError as e: loc = e.headers.get("Location", "")
# the link may use the public (tunnel) Guacamole URL; the token exchange below always goes to the LAN URL
check("1. portal issued a Guacamole link (short-lived signed JSON token)", "/guacamole" in loc and "data=" in loc, loc[:80])
data = urllib.parse.unquote(loc.split("data=", 1)[1])
tok = json.load(urllib.request.urlopen(f"{G}/api/tokens", urllib.parse.urlencode({"data": data}).encode()))
check("2. Guacamole accepted the token (auth-json)", bool(tok.get("authToken")), tok)
conns = json.load(urllib.request.urlopen(f"{G}/api/session/data/json/connections?token={tok['authToken']}"))
cid = next(iter(conns)); print("   connection:", conns[cid]["name"], conns[cid]["protocol"])
env = {**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "tcp://127.0.0.1:2375")}
nets = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", "csir-devhub-guacd-1"], capture_output=True, text=True, env=env).stdout)
if ws.get("template") in ("ubuntu-vm", "windows-kvm") or ws.get("kind") == "vm":
    lv = os.environ.get("LIBVIRT_CONTAINER", "csir-devhub-libvirt-1")
    gip = next(n["IPAddress"] for k, n in nets.items() if k.endswith("core"))
    nat = subprocess.run(["docker", "exec", lv, "iptables", "-t", "nat", "-S", "PREROUTING"], capture_output=True, text=True, env=env).stdout
    rule = [l for l in nat.splitlines() if "DNAT" in l and ":3389" in l and f"-s {gip}/32" in l]
    check("3. VM RDP reachable only via a guacd-source-restricted DNAT on the libvirt host", bool(rule), nat[-300:])
    if rule: print("   ", rule[0])
else:
    check("3. guacd attached to the workspace's private network", f"csir-ws-{ws['owner']}-{name}" in nets, list(nets))
q = urllib.parse.urlencode({"token": tok["authToken"], "GUAC_DATA_SOURCE": "json", "GUAC_ID": cid, "GUAC_TYPE": "c",
                            "GUAC_WIDTH": 1280, "GUAC_HEIGHT": 720, "GUAC_DPI": 96, "GUAC_AUDIO": "", "GUAC_IMAGE": "image/png"})
w = websocket.create_connection(G.replace("http", "ws") + "/websocket-tunnel?" + q, subprotocols=["guacamole"], timeout=20)
seen, err, t0 = set(), None, time.time()
while time.time() - t0 < 25 and not ({"img", "sync"} <= seen):
    try: msg = w.recv()
    except Exception: break
    for ins in msg.split(";"):
        m = re.match(r"\d+\.([a-z]+)", ins.strip())
        if not m: continue
        op_ = m.group(1); seen.add(op_)
        if op_ == "sync":  # acknowledge frames so the server keeps streaming
            ts = ins.split(",")[1] if "," in ins else ""; w.send(f"4.sync,{ts};")
        if op_ == "error": err = ins
w.close()
check("4. RDP session established through guacd -> xrdp (frames drawn)", {"img", "sync"} <= seen and not err, err or sorted(seen))
ports = subprocess.run(["docker", "ps", "--format", "{{.Names}} {{.Ports}}"], capture_output=True, text=True, env=env).stdout
check("5. no container publishes 3389 on the host", ":3389->" not in ports)
print(f"{sum(results)}/{len(results)} checks passed"); sys.exit(0 if all(results) else 1)
