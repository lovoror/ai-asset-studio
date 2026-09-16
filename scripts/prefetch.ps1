# Download the weights this machine needs (thin wrapper around scripts/prefetch.py).
#   .\scripts\prefetch.ps1 image
#   .\scripts\prefetch.ps1 pixal3d -MultiView
param(
  [Parameter(Position = 0)][ValidateSet("image", "pixal3d", "all")][string]$Role = "all",
  [string]$Python = "python",
  [switch]$MultiView
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
& $Python "scripts/prefetch.py" --role $Role $(if ($MultiView) { "--mv" })
exit $LASTEXITCODE
