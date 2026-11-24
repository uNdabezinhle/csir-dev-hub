# Coder server + provisioner with the tools terraform-provider-libvirt needs (mkisofs for cloud-init seed ISOs).
ARG CODER_VERSION=v2.38.10
FROM ghcr.io/coder/coder:${CODER_VERSION}
USER root
# socat: Docker Desktop overlay (docker-compose.desktop.yml) relays the browser-facing localhost:KEYCLOAK_PORT to keycloak
RUN apk add --no-cache cdrkit socat && { command -v mkisofs >/dev/null || ln -s "$(command -v genisoimage)" /usr/local/bin/mkisofs; }
USER 1000:1000
