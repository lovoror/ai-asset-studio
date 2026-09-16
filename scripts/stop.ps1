# Stop the control plane started by scripts\start.ps1 (job data in var\ is kept).
$root = Split-Path $PSScriptRoot -Parent
$pidFile = Join-Path $root "var\control.pid"
if (-not (Test-Path $pidFile)) { Write-Host "no pid file; nothing started by scripts\start.ps1"; exit 0 }
$pid_ = (Get-Content $pidFile).Trim()
if (-not (Get-Process -Id $pid_ -ErrorAction SilentlyContinue)) {
  Write-Host "process $pid_ is not running"; Remove-Item $pidFile -Force; exit 0
}
# /T so the API, the orchestrator and any local worker started by serve.py go down together.
& taskkill /T /F /PID $pid_ | Out-Null
Remove-Item $pidFile -Force
Write-Host "stopped control plane (pid $pid_)"
