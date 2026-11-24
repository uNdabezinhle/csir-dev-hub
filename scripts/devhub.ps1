<#
CSIR Dev Hub on Docker Desktop (Windows 11): run any repo script with the right environment.
  .\scripts\devhub.ps1 start                 # = scripts\start-desktop.ps1
  .\scripts\devhub.ps1 stop                  # stop the stack (volumes kept)
  .\scripts\devhub.ps1 scripts/dc.sh ps      # any bash script (Git Bash) ...
  .\scripts\devhub.ps1 scripts/test-mfa.py   # ... or python script
Sets DOCKER_HOST to the Docker Desktop engine, PYTHON for the bash scripts, UTF-8 Python I/O, no MSYS path mangling.
Never prints secrets.
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Bash = @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe", "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe") |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Bash) { throw 'Git for Windows (bash.exe) not found - install Git for Windows.' }

$Py = Get-Command python, py -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1
if (-not $Py) { throw 'Python 3 not found (python.exe / py.exe). Install Python 3.10+ from python.org.' }

if (-not $env:DOCKER_HOST) {
  $ep = (& docker context inspect --format '{{.Endpoints.docker.Host}}' 2>$null)
  if ($ep) { $env:DOCKER_HOST = $ep.Trim() }
}
$env:PYTHON = ($Py.Source -replace '\\', '/')
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
$env:MSYS_NO_PATHCONV = '1'; $env:MSYS2_ARG_CONV_EXCL = '*'

function Lock-Secrets {
  $f = Join-Path $Root 'secrets\devhub.env'
  if (Test-Path $f) {
    & icacls $f /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(F)" | Out-Null
  }
}

if ($args.Count -eq 0) { Get-Help $PSCommandPath; exit 0 }
$cmd = $args[0]; $rest = @($args | Select-Object -Skip 1)
switch ($cmd) {
  'start' {
    if (-not (Test-Path .env)) { Copy-Item .env.desktop.example .env; Write-Host 'created .env from .env.desktop.example' }
    New-Item -ItemType Directory -Force secrets | Out-Null
    & $Bash scripts/gen-secrets.sh; if ($LASTEXITCODE) { exit $LASTEXITCODE }
    Lock-Secrets
    & $Bash scripts/start.sh @rest; $rc = $LASTEXITCODE
    Lock-Secrets; exit $rc
  }
  'stop' { & $Bash scripts/stop.sh @rest; exit $LASTEXITCODE }
  default {
    if ($cmd -like '*.py') { & $Py.Source $cmd @rest } else { & $Bash $cmd @rest }
    exit $LASTEXITCODE
  }
}
