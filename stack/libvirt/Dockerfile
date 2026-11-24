# CSIR Dev Hub - libvirt/QEMU host-in-a-container (PoC KVM path).
# On a real KVM host install libvirt natively (docs/extending-kvm.md); this image lets the PoC run libvirt next to Coder.
FROM debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libvirt-daemon-system libvirt-clients qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools \
      dnsmasq-base iptables iproute2 genisoimage curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
COPY networks/ /etc/csir-libvirt/networks/
RUN chmod +x /entrypoint.sh \
 && sed -i 's/^#\?listen_tls.*/listen_tls = 0/; s/^#\?listen_tcp.*/listen_tcp = 1/; s/^#\?auth_tcp.*/auth_tcp = "none"/' /etc/libvirt/libvirtd.conf \
 && printf 'user = "root"\ngroup = "root"\nsecurity_driver = "none"\ncgroup_controllers = [ ]\nswtpm_user = "root"\nswtpm_group = "root"\n' >> /etc/libvirt/qemu.conf
LABEL org.csir.devhub.image="libvirt-host"
VOLUME ["/var/lib/libvirt"]
EXPOSE 16509
HEALTHCHECK --interval=30s --timeout=10s CMD virsh -c qemu:///system version >/dev/null || exit 1
ENTRYPOINT ["/entrypoint.sh"]
