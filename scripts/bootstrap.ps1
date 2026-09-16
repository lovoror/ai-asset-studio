# One-time bootstrap, native (no Docker): install the control-plane dependencies, prefetch the weights the
# workers on this machine need, then report readiness.
#
#   .\scripts\bootstrap.ps1                      control machine
#   .\scripts\bootstrap.ps1 -Role image          the 2D server
#   .\scripts\bootstrap.ps1 -Role pixal3d -MultiView
#
# Weights are per machine now: run this on each worker machine so it has its own model cache (config.toml
# [paths] models decides where). The control role needs Python 3.11+. The blender role needs its OWN interpreter:
# bpy has no cp312 build (4.5/5.0 are cp311), so provision one with uv and set [python] blender in config.toml -
# see requirements/blender.txt.
param(
  [ValidateSet("control", "image", "pixal3d", "blender", "all")][string]$Role = "control",
  [string]$Python = "python",
  [switch]$MultiView,
  [switch]$SkipPrefetch
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not (Test-Path "config.toml")) {
  Copy-Item "config.example.toml" "config.toml"
  Write-Host "created config.toml from config.example.toml - edit it to point at your worker machines"
}

$roles = if ($Role -eq "all") { @("control", "image", "pixal3d", "blender") } else { @($Role) }
foreach ($r in $roles) { & $Python "scripts/bootstrap.py" --role $r }

if (-not $SkipPrefetch) {
  if ($roles -contains "image") { & $Python "scripts/prefetch.py" --role image }
  if ($roles -contains "pixal3d") { & $Python "scripts/prefetch.py" --role pixal3d $(if ($MultiView) { "--mv" }) }
}

& $Python "scripts/serve.py" doctor
