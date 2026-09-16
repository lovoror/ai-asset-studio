# Start the control plane (API + orchestrator + local workers) in the background and wait for /health.
# Logs go to var\logs\control.log; stop it with scripts\stop.ps1. For a foreground run use scripts\serve.ps1.
param(
  [string]$Python = "python",
  [int]$WaitSeconds = 300
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$logDir = Join-Path $root "var\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$pidFile = Join-Path $root "var\control.pid"

if (Test-Path $pidFile) {
  $old = Get-Content $pidFile -ErrorAction SilentlyContinue
  if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
    Write-Host "asset-studio is already running (pid $old); use scripts\stop.ps1 first"
    exit 1
  }
}

# Ask the service where it will listen rather than hard-coding the port in two places.
$port = (& $Python -c "import sys; sys.path.insert(0, 'services/studio'); from studio import config; print(config.PORT)").Trim()
$url = "http://127.0.0.1:$port/health"

$p = Start-Process -FilePath $Python -ArgumentList "scripts/serve.py", "control" -WorkingDirectory $root `
  -RedirectStandardOutput (Join-Path $logDir "control.log") -RedirectStandardError (Join-Path $logDir "control.err.log") `
  -PassThru -WindowStyle Hidden
$p.Id | Set-Content $pidFile
Write-Host "started control plane (pid $($p.Id)); logs: var\logs\control.log"

for ($i = 0; $i -lt ($WaitSeconds / 2); $i++) {
  if ($p.HasExited) { Write-Warning "control plane exited with code $($p.ExitCode); see var\logs\control.err.log"; exit 1 }
  try {
    $h = Invoke-RestMethod $url -TimeoutSec 5
    if ($h.ok) { Write-Host "asset-studio is up: http://127.0.0.1:$port"; exit 0 }
    Write-Host "waiting: runners are not all healthy yet"
  } catch {}
  Start-Sleep 2
}
Write-Warning "not healthy within $WaitSeconds s; check var\logs\control.log (scripts\serve.ps1 doctor also helps)"
exit 1
