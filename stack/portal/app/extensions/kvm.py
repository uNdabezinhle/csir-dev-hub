"""KVM/libvirt provider: capacity reporting, VM inventory, power and snapshot actions for VM workspaces.
The engine broker executes approved virsh commands in the libvirt container."""
import os, re, logging
from .. import engine
log = logging.getLogger("kvm")
LIBVIRT_CONTAINER = os.environ.get("LIBVIRT_CONTAINER", "csir-devhub-libvirt-1")

class KvmError(Exception): pass

class KvmProvider:
    name = "libvirt (virsh via Docker API in csir-devhub-libvirt)"

    def _virsh(self, *args, timeout_ok=False):
        return engine.call("virsh", args=args)

    def _local_virsh(self, *args):
        import docker
        try: c = docker.from_env().containers.get(LIBVIRT_CONTAINER)
        except Exception as e: raise KvmError(f"libvirt container unavailable: {type(e).__name__}")
        rc, out = c.exec_run(["virsh", "-c", "qemu:///system", *args], demux=False)
        out = (out or b"").decode(errors="ignore")
        if rc != 0: raise KvmError(out.strip()[:300] or f"virsh {args[0]} rc={rc}")
        return out

    def available(self):
        try: self._virsh("version"); return True
        except Exception: return False

    def capabilities(self):
        try:
            ni = dict(re.findall(r"^([^:]+):\s+(.+)$", self._virsh("nodeinfo"), re.M))
            fm = int(self._virsh("freecell", "--all").strip().splitlines()[-1].split()[-2])  # KiB
            dom_caps = self._virsh("domcapabilities")
            accel = "kvm" if "<domain>kvm</domain>" in dom_caps else "tcg (software emulation)"
            ver = re.search(r"hypervisor: (.+)", self._virsh("version"))
            doms = self.domains()
            return {"status": "ok", "accel": accel, "hypervisor": ver.group(1).strip() if ver else "?",
                    "cpus": int(ni.get("CPU(s)", 0)), "mem_total_gb": round(int(ni.get("Memory size", "0 KiB").split()[0]) / 2**20, 1),
                    "mem_free_gb": round(fm / 2**20, 1), "domains": len(doms), "running": sum(d["state"] == "running" for d in doms),
                    "vcpu_alloc": sum(d.get("vcpus", 0) for d in doms if d["state"] == "running"),
                    "pools": self.pools(), "networks": self.networks()}
        except Exception as e:
            return {"status": f"unavailable: {e}"}

    def domains(self):
        out = []
        for line in self._virsh("list", "--all").splitlines()[2:]:
            parts = line.split(None, 2)
            if len(parts) < 3: continue
            name, state = parts[1], parts[2].strip()
            d = {"name": name, "state": state}
            try:
                info = dict(re.findall(r"^([^:]+):\s+(.+)$", self._virsh("dominfo", name), re.M))
                d.update(vcpus=int(info.get("CPU(s)", 0)), mem_mb=int(info.get("Max memory", "0").split()[0]) // 1024,
                         type=info.get("OS Type"), autostart=info.get("Autostart"))
                d["snapshots"] = self.snapshots(name)
            except Exception: pass
            out.append(d)
        return out

    def pools(self):
        try:
            rows = []
            for line in self._virsh("pool-list", "--all", "--details").splitlines()[2:]:
                p = line.split()
                if len(p) >= 4: rows.append({"name": p[0], "state": p[1], "capacity": " ".join(p[4:6]) if len(p) > 5 else "", "available": " ".join(p[8:10]) if len(p) > 9 else ""})
            return rows
        except Exception: return []

    def networks(self):
        try: return [l.split()[0] for l in self._virsh("net-list", "--all").splitlines()[2:] if l.strip()]
        except Exception: return []

    def power(self, domain, action):
        cmd = {"start": "start", "shutdown": "shutdown", "reboot": "reboot", "poweroff": "destroy"}.get(action)
        if not cmd: raise KvmError(f"unsupported action {action}")
        return self._virsh(cmd, domain)

    def snapshots(self, domain):
        try: return [l.split()[0] for l in self._virsh("snapshot-list", domain).splitlines()[2:] if l.strip()]
        except Exception: return []

    def snapshot(self, domain, name, description=""):
        if not re.match(r"^[A-Za-z0-9_.-]{1,40}$", name): raise KvmError("invalid snapshot name")
        # internal qcow2 snapshot (running or stopped). NB: delete snapshots before stopping the workspace - Terraform's
        # undefine of a domain that still has snapshot metadata fails.
        return self._virsh("snapshot-create-as", domain, name, description or "portal snapshot", "--atomic")

    def revert(self, domain, name): return self._virsh("snapshot-revert", domain, name)
    def delete_snapshot(self, domain, name): return self._virsh("snapshot-delete", domain, name)

    def rdp_forward(self, domain, guest_port=3389):
        host, port = engine.call("rdp-forward", domain=domain, guest_port=guest_port)
        return host, port

    def _local_rdp_forward(self, domain, guest_port):
        """Brokered RDP to a VM without exposing 3389 anywhere: DNAT <libvirt-container>:<port> -> <vm-ip>:3389 on the
        private Docker network, accepted ONLY from guacd's address. Returns (host, port) for the Guacamole connection."""
        import docker, zlib
        from ipaddress import IPv4Address
        ip = self.vm_ip(domain)
        if not ip: raise KvmError(f"{domain}: no DHCP lease yet (VM still booting?)")
        cli = docker.from_env(); lv = cli.containers.get(LIBVIRT_CONTAINER)
        g = cli.containers.get(os.environ.get("GUACD_CONTAINER", "csir-devhub-guacd-1"))
        gip = next((n["IPAddress"] for k, n in g.attrs["NetworkSettings"]["Networks"].items() if k.endswith("core")), None)
        if not gip: raise KvmError("guacd is not on the core network")
        gip, ip = IPv4Address(gip), IPv4Address(ip)
        port = 20000 + zlib.crc32(domain.encode()) % 10000
        script = f"""set -e
iptables -t nat -S PREROUTING | grep -- '--dport {port} ' | sed 's/^-A/-D/' | while read -r r; do iptables -t nat $r; done
iptables -t nat -A PREROUTING -i eth0 -s {gip} -p tcp --dport {port} -j DNAT --to-destination {ip}:{guest_port}
iptables -C FORWARD -s {gip} -d {ip} -p tcp --dport {guest_port} -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -s {gip} -d {ip} -p tcp --dport {guest_port} -j ACCEPT
"""
        rc, out = lv.exec_run(["bash", "-c", script])
        if rc != 0: raise KvmError(f"port-forward setup failed: {(out or b'').decode()[:200]}")
        return lv.name, port

    def vm_ip(self, domain):
        try:
            for l in self._virsh("domifaddr", domain, "--source", "lease").splitlines()[2:]:
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)/", l)
                if m: return m.group(1)
        except Exception: pass
        return None
