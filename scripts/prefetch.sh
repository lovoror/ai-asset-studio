#!/usr/bin/env bash
# Download the weights this machine needs (thin wrapper around scripts/prefetch.py).
#   ./scripts/prefetch.sh image
#   ./scripts/prefetch.sh pixal3d --mv
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" scripts/prefetch.py --role "${1:-all}" "${@:2}"
