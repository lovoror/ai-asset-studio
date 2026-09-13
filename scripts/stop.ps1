# Stop the asset-studio containers (volumes with jobs/models/db are kept).
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose down
