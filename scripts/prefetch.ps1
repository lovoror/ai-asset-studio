# Download every inference weight into the studio-models volume at the pinned revisions (needs network once).
param([switch]$MultiView)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$scripts = (Resolve-Path "scripts").Path
docker run --rm -v studio-models:/models -v "${scripts}:/scripts:ro" -e HF_HOME=/models/hf python:3.11-slim bash -c "pip install -q 'huggingface_hub[hf_xet]==1.31.0' && python /scripts/prefetch_hf.py $(if ($MultiView) {'--mv'})"
docker compose run --rm -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 pixal3d-worker python -m pixal3d_worker.prefetch $(if ($MultiView) {'--mv'})
