# CSIR Dev Hub - Kali Linux (RANGE) workspace image: kali-rolling + core tooling + code-server
FROM kalilinux/kali-rolling
# Use an HTTPS mirror (many enterprise egress proxies, including this PoC's, block plain-HTTP mirrors).
# The base image has no CA bundle, so ca-certificates is bootstrapped with TLS peer verification off; package
# integrity is still enforced by apt's GPG-signed Release files. Override KALI_MIRROR with the on-prem mirror.
ARG KALI_MIRROR=https://kali.download/kali
RUN echo "deb ${KALI_MIRROR} kali-rolling main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/* \
    && apt-get -o Acquire::https::Verify-Peer=false update \
    && apt-get -o Acquire::https::Verify-Peer=false install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG CODE_SERVER_VERSION=4.146.0
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git sudo vim nano less jq unzip iproute2 iputils-ping python3 python3-pip \
      nmap netcat-traditional dnsutils whois tcpdump sqlmap nikto hydra john \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash -u 1000 coder \
    && echo "coder ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/coder && chmod 440 /etc/sudoers.d/coder \
    && curl -fsSL https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_amd64.deb -o /tmp/cs.deb \
    && apt-get update && apt-get install -y /tmp/cs.deb && rm -f /tmp/cs.deb && rm -rf /var/lib/apt/lists/*
# Larger metapackages (kali-linux-headless ~ several GB) can be added per range exercise:
#   RUN apt-get update && apt-get install -y kali-linux-headless
LABEL org.csir.devhub.image="kali"
USER coder
WORKDIR /home/coder
