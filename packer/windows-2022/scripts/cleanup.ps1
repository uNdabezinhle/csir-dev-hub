# Remove build-only access before sysprep: Packer WinRM listener/firewall rule, autologon; compact the disk.
netsh advfirewall firewall delete rule name="WinRM-Packer"
Remove-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -Name DefaultPassword -ErrorAction SilentlyContinue
Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -Name AutoAdminLogon -Value 0
Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue
