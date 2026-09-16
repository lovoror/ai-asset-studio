# Tail the control-plane log written by scripts\start.ps1.
param([int]$Tail = 100)
$root = Split-Path $PSScriptRoot -Parent
$log = Join-Path $root "var\logs\control.log"
if (-not (Test-Path $log)) { Write-Warning "no log yet at $log; start the service with scripts\start.ps1"; exit 1 }
Get-Content $log -Tail $Tail -Wait
