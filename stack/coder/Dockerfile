# Coder server + provisioner with the tools terraform-provider-libvirt needs (mkisofs for cloud-init seed ISOs).
ARG CODER_VERSION=v2.38.10
FROM ghcr.io/coder/coder:${CODER_VERSION}
USER root
RUN apk add --no-cache cdrkit && { command -v mkisofs >/dev/null || ln -s "$(command -v genisoimage)" /usr/local/bin/mkisofs; }
USER 1000:1000
