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
# GPU sharing policy (lenient by default): a stage is started unless the card is essentially full (used memory above
# the pre-start threshold). If a stage then hits CUDA OOM, the worker first waits for other workloads to release memory
# (down to the "retry" threshold) and retries with the same settings; only after that does it apply the preset's
# quality fallback ladder. Other workloads are never killed.
GPU_BUSY_THRESHOLD_MIB = {
    "image": int(os.environ.get("STUDIO_GPU_BUSY_THRESHOLD_MIB_IMAGE", "24576")),
    "pixal3d": int(os.environ.get("STUDIO_GPU_BUSY_THRESHOLD_MIB_PIXAL3D", "26624")),
}
GPU_RETRY_THRESHOLD_MIB = {
    "image": int(os.environ.get("STUDIO_GPU_RETRY_THRESHOLD_MIB_IMAGE", "6144")),
    "pixal3d": int(os.environ.get("STUDIO_GPU_RETRY_THRESHOLD_MIB_PIXAL3D", "14336")),
}
GPU_BUSY_WAIT_S = int(os.environ.get("STUDIO_GPU_BUSY_WAIT_S", "1800"))
STAGE_TIMEOUT_S = int(os.environ.get("STUDIO_STAGE_TIMEOUT_S", "7200"))
MAX_OOM_RETRIES = int(os.environ.get("STUDIO_MAX_OOM_RETRIES", "4"))
VERSION = "0.1.0"
