Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose logs -f --tail 100 $args
