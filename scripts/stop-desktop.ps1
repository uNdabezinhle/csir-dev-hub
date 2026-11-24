# Stop CSIR Dev Hub on Docker Desktop. Containers are stopped, volumes (data, workspaces home dirs) are kept.
& (Join-Path $PSScriptRoot 'devhub.ps1') stop @args
exit $LASTEXITCODE
