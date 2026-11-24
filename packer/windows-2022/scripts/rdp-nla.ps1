# RDP for Guacamole brokering: enabled, NLA required, TLS; firewall restricted to the RDP gateway subnet at deploy time.
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name UserAuthentication -Value 1
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name SecurityLayer -Value 2
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
