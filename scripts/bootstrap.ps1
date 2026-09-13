# One-time bootstrap: build images, create volumes, prefetch all weights (~105 GB), verify offline loading.
# Prerequisites: Docker Desktop (WSL2 backend) with NVIDIA GPU support, the trellis2:rtx5090 base image
# (built from C:\Users\Zorro\TRELLIS.2\Dockerfile), and a .env with STUDIO_GPU_UUID (see .env.example).
param([switch]$MultiView)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
foreach ($v in "studio-models","studio-jobs","studio-data") { docker volume create $v | Out-Null }
docker compose build
& "$PSScriptRoot\prefetch.ps1" @PSBoundParameters
docker compose up -d
docker compose exec -T pixal3d-worker python -m pixal3d_worker.prefetch --verify
docker compose exec -T image-worker python -m image_worker.generate --selftest
python cli\assetctl.py doctor
