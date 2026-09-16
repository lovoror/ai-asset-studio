#!/usr/bin/env bash
# Start an asset-studio role on this machine (thin wrapper around scripts/serve.py).
#   ./scripts/serve.sh control    API + orchestrator + whatever worker runs here
#   ./scripts/serve.sh image      this machine is the 2D server
#   ./scripts/serve.sh pixal3d    this machine is the 3D server
#   ./scripts/serve.sh all        everything on one machine
#   ./scripts/serve.sh doctor     report what this machine can run
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" scripts/serve.py "${1:-control}"
