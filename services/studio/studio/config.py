"""Runtime configuration for the control plane.

Settings come from `config.toml` at the repository root (see `config.example.toml`), with `STUDIO_*` environment
variables taking precedence over it, and built-in defaults below everything. That ordering keeps the service
runnable with no config at all (single machine, everything local) while still letting a deployment pin every
directory and every worker address. Only TOML and the standard library are used, so a machine can be configured
before any dependency is installed.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(os.environ.get("STUDIO_ROOT") or Path(__file__).resolve().parents[3]).resolve()
CONFIG_PATH = Path(os.environ.get("STUDIO_CONFIG") or (REPO_ROOT / "config.toml"))

VERSION = "0.1.0"

# Worker HTTP ports used when a stage runs on this machine. They match the historical Docker layout so existing
# scripts and firewalls keep working.
DEFAULT_PORTS = {"image": 8701, "pixal3d": 8702, "blender": 8703}


def _load_file() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:  # a broken config must fail loudly, not silently default
        raise SystemExit(f"cannot read {CONFIG_PATH}: {e}") from e


_FILE = _load_file()


def _section(name: str) -> dict:
    v = _FILE.get(name) or {}
    return v if isinstance(v, dict) else {}


def _raw(section: str, key: str, env: str, default):
    if env in os.environ:
        return os.environ[env]
    v = _section(section).get(key)
    return default if v is None else v


def _bool(section: str, key: str, env: str, default: bool) -> bool:
    v = _raw(section, key, env, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int(section: str, key: str, env: str, default: int) -> int:
    return int(_raw(section, key, env, default))


def _text(section: str, key: str, env: str, default: str) -> str:
    return str(_raw(section, key, env, default)).strip()


def _path(section: str, key: str, env: str, default: str) -> Path | None:
    """Relative paths in config.toml are resolved against the repository root, so the service can be started
    from any working directory."""
    raw = _text(section, key, env, default)
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


# ---- directories ---------------------------------------------------------------------------------------------
JOBS_DIR = _path("paths", "jobs", "STUDIO_JOBS_DIR", "var/jobs")
DATA_DIR = _path("paths", "data", "STUDIO_DATA_DIR", "var/data")
PRESETS_DIR = _path("paths", "presets", "STUDIO_PRESETS_DIR", "presets")
MANIFESTS_DIR = _path("paths", "manifests", "STUDIO_MANIFESTS_DIR", "manifests")
WEB_DIST = _path("paths", "web_dist", "STUDIO_WEB_DIST", "web/dist")
DB_PATH = DATA_DIR / "studio.db"

# Model cache. Empty means "let torch/diffusers use their platform default"; set it to keep the weights on a
# drive with room for them. HF_HOME/TORCH_HOME are exported for the stage subprocesses by scripts/serve.py.
MODELS_DIR = _path("paths", "models", "STUDIO_MODELS_DIR", "")
HF_HOME = _text("paths", "hf_home", "HF_HOME", str(MODELS_DIR / "hf") if MODELS_DIR else "")
TORCH_HOME = _text("paths", "torch_home", "TORCH_HOME", str(MODELS_DIR / "torch") if MODELS_DIR else "")
EXTRA_MODELS_DIR = _path("paths", "extra_models", "STUDIO_EXTRA_MODELS_DIR", "")

# ---- HTTP API ------------------------------------------------------------------------------------------------
BIND_HOST = _text("control", "bind", "STUDIO_BIND", "127.0.0.1")
PORT = _int("control", "port", "STUDIO_API_PORT", 8090)
API_TOKEN = _text("control", "api_token", "STUDIO_API_TOKEN", "")
CORS_ORIGINS = [o.strip() for o in _text("control", "cors_origins", "STUDIO_CORS_ORIGINS", "").split(",") if o.strip()]

# ---- worker servers ------------------------------------------------------------------------------------------
# Where each stage runs. "local" (or empty) means this machine, addressed on the loopback port below - the
# control plane may start that worker itself (scripts/serve.py). Anything else is a URL such as
# "http://192.168.1.22:8702"; a bare "host:port" is accepted and http:// is assumed.
LOCAL_VALUES = ("", "local", "-")


def _endpoint(key: str, env: str) -> str:
    """The raw config.toml/env value for a stage: "local" or a URL (normalised, "" means local)."""
    raw = _text("servers", key, env, "local")
    if raw in LOCAL_VALUES:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


WORKER_BIND = _text("worker", "bind", "STUDIO_WORKER_BIND", "127.0.0.1")
WORKER_TOKEN = _text("worker", "token", "STUDIO_WORKER_TOKEN", "")
WORKER_PORTS = {k: _int("worker", f"{k}_port", f"STUDIO_{k.upper()}_PORT", v) for k, v in DEFAULT_PORTS.items()}

# The config.toml values: the bootstrap defaults for this machine.
_ENDPOINTS = {k: _endpoint(k, f"STUDIO_{k.upper()}_RUNNER") for k in DEFAULT_PORTS}
RUNNERS = {k: (v or f"http://127.0.0.1:{WORKER_PORTS[k]}") for k, v in _ENDPOINTS.items()}
LOCAL_RUNNERS = [k for k, v in _ENDPOINTS.items() if not v]

WORKER_ROLES = [r.strip() for r in _text("worker", "roles", "STUDIO_WORKER_ROLES", "").split(",") if r.strip()]

# The portal's Settings page can override the stage addresses at runtime. Those overrides live in the settings
# table next to the jobs (not in config.toml, so a hand-commented config file is never rewritten), and are read
# here on demand: a Runner is built per request/job, so an address change takes effect on the next job without a
# restart. Only *starting or stopping* the local worker processes still needs one - that decision is made once,
# by scripts/serve.py at boot.
_ENDPOINT_CACHE: dict = {"t": 0.0, "v": None}
ENDPOINT_CACHE_S = 2.0


def _settings_row(key: str):
    """Read one settings value straight from SQLite. Deliberately not via db.py: db.py imports this module."""
    import sqlite3
    if not DB_PATH or not DB_PATH.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001  (a locked or absent table just means "no override")
        return None
    if not row or row[0] is None:
        return None
    import json
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def normalize_endpoint(role: str, raw: str | None) -> str:
    """A `[servers]` / settings value -> a URL. Local values resolve to the loopback port."""
    v = (raw or "").strip()
    if v in LOCAL_VALUES:
        return f"http://127.0.0.1:{WORKER_PORTS[role]}"
    if "://" not in v:
        v = "http://" + v
    return v.rstrip("/")


def endpoint_values() -> dict[str, str]:
    """Stage -> the configured value ("local" or a URL), config.toml overridden by the Settings page."""
    out = {k: (v or "local") for k, v in _ENDPOINTS.items()}
    overrides = _settings_row("worker_endpoints")
    if isinstance(overrides, dict):
        for role, raw in overrides.items():
            if role in out:
                out[role] = (str(raw).strip() or "local")
    return out


def effective_runners(force: bool = False) -> dict[str, str]:
    """Stage -> the URL to reach its worker. Cached for a couple of seconds: this is called per request."""
    import time
    now = time.time()
    if not force and _ENDPOINT_CACHE["v"] is not None and now - _ENDPOINT_CACHE["t"] < ENDPOINT_CACHE_S:
        return dict(_ENDPOINT_CACHE["v"])
    out = {role: normalize_endpoint(role, raw) for role, raw in endpoint_values().items()}
    _ENDPOINT_CACHE.update(t=now, v=out)
    return dict(out)


def local_stage_roles() -> list[str]:
    """Stages whose worker process belongs on this machine (scripts/serve.py starts these at boot)."""
    return [role for role, raw in endpoint_values().items() if raw.strip().lower() in LOCAL_VALUES]

# ---- interpreters --------------------------------------------------------------------------------------------
# Optional per-role Python. A role often cannot share the control plane's interpreter: bpy ships exactly one
# interpreter tag per release (4.5/5.0 build for cp311, 5.1+ for cp313, and there is no cp312 build at all),
# while the control plane runs on any 3.11+. Empty means "the interpreter running scripts/serve.py".
# Relative paths are resolved against the repository root.
PYTHON_KEYS = ("control", "image", "pixal3d", "blender")


def _interpreter(key: str) -> str:
    raw = _text("python", key, f"STUDIO_PYTHON_{key.upper()}", "")
    if not raw:
        return ""
    p = Path(raw).expanduser()
    return str(p if p.is_absolute() else (REPO_ROOT / p))


PYTHON_INTERPRETERS = {k: _interpreter(k) for k in PYTHON_KEYS}

# ---- stage behaviour -----------------------------------------------------------------------------------------
GPU_UUID = _text("stage", "gpu_uuid", "STUDIO_GPU_UUID", "")
STAGE_TIMEOUT_S = _int("stage", "stage_timeout_s", "STUDIO_STAGE_TIMEOUT_S", 7200)
MAX_OOM_RETRIES = _int("stage", "max_oom_retries", "STUDIO_MAX_OOM_RETRIES", 4)
OOM_RETRY_PAUSE_S = _int("stage", "oom_retry_pause_s", "STUDIO_OOM_RETRY_PAUSE_S", 20)
REMBG_MODEL = _text("stage", "rembg_model", "STUDIO_REMBG_MODEL", "briaai/RMBG-2.0")
VISION_EVALUATOR_URL = _text("stage", "vision_evaluator_url", "STUDIO_VISION_EVALUATOR_URL", "")
# Refuse a new job (or a release/retry) when a stage server it needs does not answer, instead of queueing work
# that will fail minutes later. On by default; turn it off to pre-queue jobs while a server is briefly down, or
# in tests, which point the runners at a closed port on purpose.
REQUIRE_READY = _bool("stage", "require_ready", "STUDIO_REQUIRE_READY", True)
# Transfer knobs for remote workers: how long to wait on a slow link and how big a single uploaded file may be.
CONNECT_TIMEOUT_S = _int("stage", "connect_timeout_s", "STUDIO_CONNECT_TIMEOUT_S", 10)
TRANSFER_TIMEOUT_S = _int("stage", "transfer_timeout_s", "STUDIO_TRANSFER_TIMEOUT_S", 1800)
