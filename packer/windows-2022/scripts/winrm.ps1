# Build-time only: WinRM over HTTP on the private Packer NAT (removed again in cleanup.ps1 before sysprep).
Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
winrm quickconfig -q
winrm set winrm/config/service/auth '@{Basic="true"}'
winrm set winrm/config/service '@{AllowUnencrypted="true"}'
netsh advfirewall firewall add rule name="WinRM-Packer" dir=in action=allow protocol=TCP localport=5985
