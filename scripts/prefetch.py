#!/usr/bin/env python3
"""Download the model weights a role needs, at the pinned revisions (needs network once).

  python scripts/prefetch.py --role image            # 2D server: Qwen-Image, FLUX/Z-Image configs
  python scripts/prefetch.py --role pixal3d --mv     # 3D server: Pixal3D (+ multi-view), MoGe, matting
  python scripts/prefetch.py --role all

There is no shared model volume any more, so run this once on *every* machine that runs GPU stages: each keeps
its own cache under [paths] models in config.toml (or HF_HOME/TORCH_HOME if you set those).

The 3D role additionally prepares Pixal3D's own pipeline files with `python -m pixal3d_worker.prefetch`, which
must run in the 3D environment.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "studio"))
from studio import config  # noqa: E402

PINS = {
    "qwen": [("Qwen/Qwen-Image-2512", "25468b98e3276ca6700de15c6628e51b7de54a26", None)],
    "pixal3d": [("TencentARC/Pixal3D", "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f",
                 ["pipeline.json", "pipeline_mv.json", "ckpts/*_bf16.*", "ckpts/*_fp16.*"])],
    "pixal3d_mv": [("TencentARC/Pixal3D", "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f", ["ckpts/*_mv.*"])],
    "qwen_lightning": [("lightx2v/Qwen-Image-2512-Lightning", "a52649c9d0f6e1a248bff13f0df33bb8a2abdb52",
                        ["Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors",
                         "Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors"])],
    # configs/tokenizers/VAEs only: the big transformer + text-encoder weights are reused from the ComfyUI models folder
    "extra_configs": [
        ("black-forest-labs/FLUX.2-klein-4B", "e7b7dc27f91deacad38e78976d1f2b499d76a294",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]),
        ("Tongyi-MAI/Z-Image-Turbo", "f332072aa78be7aecdf3ee76d5c247082da564a6",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]),
        # gated (FLUX Non-Commercial License; accept on huggingface.co first): configs only, weights come from the ComfyUI fp8 files
        ("black-forest-labs/FLUX.2-klein-9B", "92196c8e11f7b6cf2b7493e037d8c5345c559216",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*", "transformer/config.json", "vae/*"]),
        ("black-forest-labs/FLUX.2-klein-base-9B", "32773329fbe7e81a90ef971740e8ba4b0364ecf3",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]),
    ],
    "aux": [
        ("Ruicheng/moge-2-vitl", "39c4d5e957afe587e04eec59dc2bcc3be5ecd968", None),
        ("camenduru/dinov3-vitl16-pretrain-lvd1689m", "3c276edd87d6f6e569ff0c4400e086807d0f3881", None),
        ("ZhengPeng7/BiRefNet", "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4", None),
        # Pixal3D's upstream matting model (gated: accept the license on huggingface.co/briaai/RMBG-2.0);
        # BiRefNet above is the open drop-in.
        ("briaai/RMBG-2.0", "5df4c9c76d8170882c34f6986e848ee07fd0ba43", ["*.json", "*.py", "model.safetensors"]),
    ],
}

ROLE_GROUPS = {
    "image": ["qwen", "qwen_lightning", "extra_configs"],
    "pixal3d": ["pixal3d", "aux"],
    "all": ["qwen", "qwen_lightning", "extra_configs", "pixal3d", "aux"],
}


def hub_dir() -> Path:
    try:
        from huggingface_hub import constants
        return Path(constants.HF_HUB_CACHE)
    except Exception:  # noqa: BLE001
        home = config.HF_HOME or str(Path.home() / ".cache" / "huggingface")
        return Path(home) / "hub"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", choices=sorted(ROLE_GROUPS), default="all")
    ap.add_argument("--mv", action="store_true", help="also fetch the multi-view checkpoints (+17 GB)")
    ap.add_argument("--skip-pixal3d-pipeline", action="store_true",
                    help="do not run pixal3d_worker.prefetch (HF repos only)")
    a = ap.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is not installed in this interpreter.\n"
              "  python scripts/bootstrap.py --role image    (or --role pixal3d)", file=sys.stderr)
        return 2

    groups = list(ROLE_GROUPS[a.role])
    if a.mv:
        groups.append("pixal3d_mv")
    if config.HF_HOME:
        os.environ["HF_HOME"] = config.HF_HOME
        print(f"[prefetch] HF_HOME={config.HF_HOME}")
    else:
        print("[prefetch] HF_HOME is not set in config.toml: using the huggingface_hub default cache")
    os.environ.pop("HF_HUB_OFFLINE", None)   # fetching is the one thing that must be able to reach the network

    refs_root = hub_dir()
    for g in groups:
        for repo, rev, patterns in PINS[g]:
            t = time.time()
            print(f"[prefetch] {repo}@{rev[:8]} patterns={patterns}", flush=True)
            path = snapshot_download(repo, revision=rev, allow_patterns=patterns, max_workers=8)
            refs = refs_root / ("models--" + repo.replace("/", "--")) / "refs"
            refs.mkdir(parents=True, exist_ok=True)
            (refs / "main").write_text(rev, encoding="utf-8")  # lets revision='main' resolve offline to the pinned snapshot
            print(f"[prefetch] done {repo} -> {path} ({time.time() - t:.0f}s)", flush=True)

    if a.role in ("pixal3d", "all") and not a.skip_pixal3d_pipeline:
        print("[prefetch] preparing the Pixal3D pipeline files (pixal3d_worker.prefetch)", flush=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "services")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        cmd = [sys.executable, "-m", "pixal3d_worker.prefetch"]
        if a.mv:
            cmd.append("--mv")
        rc = subprocess.call(cmd, env=env, cwd=str(ROOT))
        if rc != 0:
            print("[prefetch] pixal3d_worker.prefetch failed; fix that before running 3D jobs", file=sys.stderr)
            return rc

    print("[prefetch] all done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
