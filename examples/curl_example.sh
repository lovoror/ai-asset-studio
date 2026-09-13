#!/usr/bin/env bash
# HTTP example: submit, poll, download the optimized GLB.
set -e
API=${STUDIO_URL:-http://127.0.0.1:8090}
JOB=$(curl -s -X POST "$API/v1/jobs" -H 'Content-Type: application/json' -d @"$(dirname "$0")/cargo_crate.json" | python -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
echo "job $JOB"
while true; do
  S=$(curl -s "$API/v1/jobs/$JOB?events=1"); ST=$(echo "$S" | python -c 'import sys,json;d=json.load(sys.stdin);print(d["status"], d["stage"])')
  echo "$ST"; case "$ST" in completed*|failed*|cancelled*) break;; esac; sleep 5
done
curl -s "$API/v1/jobs/$JOB/artifacts" | python -m json.tool
curl -s -o asset.glb "$API/v1/jobs/$JOB/artifacts/$(curl -s "$API/v1/jobs/$JOB/artifacts/manifest.json" | python -c 'import sys,json;print(json.load(sys.stdin)["asset"]["optimized"])')"
