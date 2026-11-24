# ubuntu-desktop template
Ubuntu 24.04 XFCE desktop workspace (image `images/ubuntu-desktop`): everything in `ubuntu-server` plus
* **xrdp** on 3389 - only on the workspace's private network. Brokered by Guacamole via the portal "Desktop (RDP)" button
  (guacd is attached to this network on demand) or natively with `coder port-forward <ws> --tcp 3389:3389` + mstsc/Remmina.
  Login user `coder`; password = portal-managed secret `/run/csir-secrets/rdp_password` (never shown to users when brokered).
* **noVNC** desktop in the browser (`coder_app.novnc`, TigerVNC :1 -> websockify :6080).
* code-server + VS Code Desktop as in ubuntu-server.
Default 2-4 vCPU / 4 GB (desktop sessions need more RAM).
