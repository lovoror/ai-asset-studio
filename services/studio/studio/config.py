"""Runtime configuration for the API and orchestrator worker (environment driven, no secrets in code)."""
import os
from pathlib import Path

JOBS_DIR = Path(os.environ.get("STUDIO_JOBS_DIR", "/jobs"))
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "studio.db"
PRESETS_DIR = Path(os.environ.get("STUDIO_PRESETS_DIR", "/app/presets"))
MANIFESTS_DIR = Path(os.environ.get("STUDIO_MANIFESTS_DIR", "/app/manifests"))

BIND_HOST = os.environ.get("STUDIO_BIND", "127.0.0.1")
PORT = int(os.environ.get("STUDIO_API_PORT", "8090"))
API_TOKEN = os.environ.get("STUDIO_API_TOKEN", "").strip()

RUNNERS = {
    "image": os.environ.get("STUDIO_IMAGE_RUNNER", "http://image-worker:8701"),
    "pixal3d": os.environ.get("STUDIO_PIXAL3D_RUNNER", "http://pixal3d-worker:8702"),
    "blender": os.environ.get("STUDIO_BLENDER_RUNNER", "http://blender:8703"),
}
GPU_UUID = os.environ.get("STUDIO_GPU_UUID", "")
# GPU sharing policy: no VRAM thresholds. Stages start immediately (the image lane, the asset lane and other apps share
# the card). On CUDA OOM the worker waits for the other lane's GPU stage to finish and retries with the same settings
# (up to MAX_OOM_RETRIES times), then retries once after a short pause, and only then applies the preset's quality
# fallback ladder. Other workloads are never killed.
STAGE_TIMEOUT_S = int(os.environ.get("STUDIO_STAGE_TIMEOUT_S", "7200"))
MAX_OOM_RETRIES = int(os.environ.get("STUDIO_MAX_OOM_RETRIES", "4"))
OOM_RETRY_PAUSE_S = int(os.environ.get("STUDIO_OOM_RETRY_PAUSE_S", "20"))
VERSION = "0.1.0"
