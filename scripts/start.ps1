# Start the asset-studio stack (Docker Desktop). Builds images if missing, creates volumes, waits for /health.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
foreach ($v in "studio-models","studio-jobs","studio-data") { docker volume create $v | Out-Null }
docker compose up -d --build
$url = "http://127.0.0.1:8090/health"
for ($i = 0; $i -lt 60; $i++) {
  try { $h = Invoke-RestMethod $url -TimeoutSec 5; if ($h.ok) { Write-Host "asset-studio is up: $url"; exit 0 } } catch {}
  Start-Sleep 2
}
Write-Warning "service did not report healthy within 120s; check: docker compose logs"
exit 1
