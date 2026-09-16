# Start an asset-studio role on this machine (thin wrapper around scripts/serve.py).
#   .\scripts\serve.ps1 control     API + orchestrator + whatever worker runs here
#   .\scripts\serve.ps1 image       this machine is the 2D server
#   .\scripts\serve.ps1 pixal3d     this machine is the 3D server
#   .\scripts\serve.ps1 all         everything on one machine
#   .\scripts\serve.ps1 doctor      report what this machine can run
param(
  [Parameter(Position = 0)][string]$Role = "control",
  [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
& $Python "scripts/serve.py" $Role
exit $LASTEXITCODE
