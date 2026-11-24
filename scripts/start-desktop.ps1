# Start CSIR Dev Hub on Docker Desktop (Windows 11). Idempotent. See docs/local-windows.md.
& (Join-Path $PSScriptRoot 'devhub.ps1') start @args
exit $LASTEXITCODE
