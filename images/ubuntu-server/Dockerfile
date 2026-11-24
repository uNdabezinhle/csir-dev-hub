# CSIR Dev Hub - Ubuntu Server workspace image (24.04 or 26.04)
ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}
ARG CODE_SERVER_VERSION=4.146.0
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git sudo openssh-client bash-completion vim nano less \
      build-essential python3 python3-pip python3-venv jq unzip htop iproute2 iputils-ping locales \
    && rm -rf /var/lib/apt/lists/* \
    && (userdel -r ubuntu 2>/dev/null || true) \
    && useradd -m -s /bin/bash -u 1000 coder \
    && echo "coder ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/coder && chmod 440 /etc/sudoers.d/coder \
    && curl -fsSL https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_amd64.deb -o /tmp/cs.deb \
    && apt-get update && apt-get install -y /tmp/cs.deb && rm -f /tmp/cs.deb && rm -rf /var/lib/apt/lists/*
LABEL org.csir.devhub.image="ubuntu-server"
USER coder
WORKDIR /home/coder
