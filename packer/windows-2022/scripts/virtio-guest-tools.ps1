# virtio drivers (balloon, serial, display, qemu-guest-agent) from the attached virtio-win ISO
$iso = Get-Volume | Where-Object { Test-Path ($_.DriveLetter + ':\virtio-win-guest-tools.exe') } | Select-Object -First 1
if (-not $iso) { throw "virtio-win ISO not found" }
Start-Process -Wait -FilePath ($iso.DriveLetter + ':\virtio-win-guest-tools.exe') -ArgumentList '/install', '/quiet', '/norestart'
