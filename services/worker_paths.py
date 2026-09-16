"""Where a worker process looks for model weights and caches on this machine.

The containers used to hard-code /models, /app and a shared /jobs volume. A native install has no such
directories, so every worker resolves its paths from the environment that scripts/serve.py exports (HF_HOME /
TORCH_HOME / STUDIO_* from config.toml) and otherwise falls back to the usual per-user cache locations.

Standard library only: this module is imported by every worker role, including the CUDA ones.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else None


def hf_home() -> Path:
    """Hugging Face cache root (HF_HOME)."""
    return _env_path("HF_HOME") or (Path.home() / ".cache" / "huggingface")


def hub_dir() -> Path:
    """The `hub/` directory inside HF_HOME, where snapshot_download keeps repository snapshots."""
    return hf_home() / "hub"


def torch_home() -> Path:
    """Torch hub cache root (TORCH_HOME); used for valeoai/NAF."""
    return _env_path("TORCH_HOME") or (Path.home() / ".cache" / "torch")


def extra_models_dir() -> Path | None:
    """Optional ComfyUI-style folder of single-file models (diffusion_models/, text_encoders/).

    Unset means the image models must be downloadable from the hub instead.
    """
    return _env_path("STUDIO_EXTRA_MODELS_DIR")


def extra_cache_dir() -> Path:
    """Where single-file weights are converted and cached, so later loads take seconds instead of a minute."""
    return _env_path("STUDIO_EXTRA_CACHE_DIR") or (hf_home() / "extra-cache")


def pixal3d_local() -> Path:
    """A local checkout of TencentARC/Pixal3D, used by the `local` pipeline mode."""
    return _env_path("STUDIO_PIXAL3D_LOCAL") or (hf_home() / "pixal3d-local")


def presets_dir() -> Path | None:
    return _env_path("STUDIO_PRESETS_DIR")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
