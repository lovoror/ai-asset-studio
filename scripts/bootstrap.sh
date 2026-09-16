#!/usr/bin/env bash
# One-time bootstrap, native (no Docker): install the control-plane dependencies, prefetch the weights the
# workers on this machine need, then report readiness.
#
#   ./scripts/bootstrap.sh                     control machine
#   ./scripts/bootstrap.sh image               the 2D server
#   ./scripts/bootstrap.sh pixal3d --mv
#
# Weights are per machine now: run this on each worker machine so it has its own model cache (config.toml
# [paths] models decides where). Requires Python 3.11+.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
ROLE="control"; MV=""
for a in "$@"; do
  case "$a" in
    --mv) MV="--mv" ;;
    *) ROLE="$a" ;;
  esac
done

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "created config.toml from config.example.toml - edit it to point at your worker machines"
fi

case "$ROLE" in
  all)
    for r in control image pixal3d blender; do "$PY" scripts/bootstrap.py --role "$r"; done
    "$PY" scripts/prefetch.py --role image
    "$PY" scripts/prefetch.py --role pixal3d $MV
    ;;
  image)   "$PY" scripts/bootstrap.py --role image;   "$PY" scripts/prefetch.py --role image ;;
  pixal3d) "$PY" scripts/bootstrap.py --role pixal3d; "$PY" scripts/prefetch.py --role pixal3d $MV ;;
  *)       "$PY" scripts/bootstrap.py --role "$ROLE" ;;
esac

"$PY" scripts/serve.py doctor
