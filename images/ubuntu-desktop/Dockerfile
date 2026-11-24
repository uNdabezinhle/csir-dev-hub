# CSIR Dev Hub - Ubuntu Desktop workspace: XFCE + xrdp (RDP :3389) + TigerVNC/noVNC (browser :6080) + code-server
ARG UBUNTU_VERSION=24.04
FROM csir-devhub/ubuntu-server:${UBUNTU_VERSION}
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      xfce4 xfce4-terminal xfce4-goodies dbus-x11 x11-xserver-utils xauth \
      xrdp xorgxrdp tigervnc-standalone-server tigervnc-tools novnc websockify \
      firefox fonts-dejavu-core supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && adduser xrdp ssl-cert \
    && sed -i 's/^port=3389/port=tcp:\/\/:3389/' /etc/xrdp/xrdp.ini \
    && echo "xfce4-session" > /etc/skel/.xsession \
    && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html
COPY start-desktop.sh /usr/local/bin/start-desktop.sh
RUN chmod +x /usr/local/bin/start-desktop.sh
LABEL org.csir.devhub.image="ubuntu-desktop"
USER coder
