# kali template (RANGE)
Kali Rolling Docker workspace (`images/kali`: nmap, sqlmap, nikto, hydra, john, tcpdump... + code-server).
Defaults to the **isolated** network profile: own `internal` Docker network, no internet egress; only the Coder control
plane is reachable through a per-workspace TCP gateway. Range targets are attached by joining the workspace network to a
range network (planned: portal "attach to range" action). Raw-socket tools (nmap -sS, tcpdump) need `NET_RAW/NET_ADMIN`
capabilities - add `capabilities { add = ["NET_RAW","NET_ADMIN"] }` to the container for range exercises (off by default).
A full Kali **VM** (KVM) variant follows the windows-kvm libvirt pattern.
