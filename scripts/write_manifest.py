#!/usr/bin/env python3
"""Refresh manifests/dependency-manifest.json: pinned commits, model revisions and the frozen package versions of
each role's environment on this machine.

There are no container images to inspect any more, so the package sets come from the interpreter you point at.
A role you do not have installed is left as it is (its entry keeps the reference pins captured from the original
container build).

  python scripts/write_manifest.py                       # only this interpreter (whatever role it can serve)
  python scripts/write_manifest.py --env image=.venv-image/Scripts/python.exe \
                                   --env pixal3d=.venv-3d/bin/python \
                                   --env blender=.venv-blender/Scripts/python.exe
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "dependency-manifest.json"

ROLES = ("control", "image", "pixal3d", "blender")
# The first of these that a role's interpreter can import identifies the role, so `--env` can stay optional.
ROLE_MARKERS = {
    "blender": "bpy",
    "pixal3d": "natten",
    "image": "diffusers",
    "control": "fastapi",
}

KEY_PACKAGES = ["torch", "torchvision", "triton", "diffusers", "transformers", "accelerate", "huggingface_hub",
                "safetensors", "natten", "flash_attn", "nvdiffrast", "o_voxel", "cumesh", "flex_gemm", "utils3d",
                "utils3d_moge", "moge", "kornia", "timm", "trimesh", "pillow", "opencv-python-headless", "bpy",
                "meshoptimizer", "fast-simplification", "fastapi", "uvicorn", "pydantic", "pygltflib", "numpy",
                "einops"]

# Reference data: what the pipeline pins, independent of any one machine.
PINS = {
    "sources": {
        "TencentARC/Pixal3D": {"url": "https://github.com/TencentARC/Pixal3D", "branch": "master (default; NOT `paper`)",
                               "commit": "f7cf38429b0bd264f1995f0f8743a88b1c728b94", "date": "2026-09-01"},
        "microsoft/TRELLIS.2": {"url": "https://github.com/microsoft/TRELLIS.2", "branch": "main",
                                "commit": "75fbf0183001ed9876c8dbb35de6b68552ee08bd",
                                "note": ("3D base environment (CUDA 12.8.1 toolkit, torch 2.11.0+cu128, flash-attn 2.8.3, "
                                         "nvdiffrast, CuMesh, FlexGEMM, o-voxel) built for the RTX 5090 per TRELLIS.2's "
                                         "installation instructions; see requirements/pixal3d.txt")},
        "microsoft/MoGe": {"commit": "74fbce054ebed49800de42d0ad0e83495065719a", "installed": "--no-deps (keeps the base FlexGEMM build)"},
        "EasternJournalist/utils3d-moge": {"commit": "62f09d58509485564e24d5d9f6aac9ee9ebc0c37"},
        "EasternJournalist/pipeline": {"commit": "1c511390d90226c00c101f34b84df26a0f8789b4"},
        "valeoai/NAF": {"commit": "37f2dfc180f2de53d98bd601109c0da0dd6b0f43",
                        "weights": "https://github.com/valeoai/NAF/releases/download/model/naf_release.pth",
                        "note": "torch.hub cache pinned to this commit under <TORCH_HOME>/hub/valeoai_NAF_main"},
        "utils3d (Pixal3D-specified wheel)": {"url": "https://github.com/LDYang694/Storages/releases/download/20260430/utils3d-0.0.2-py3-none-any.whl"},
        "NATTEN": {"version": "0.21.0 (Pixal3D requirement)",
                   "build": "from sdist, NATTEN_CUDA_ARCH=12.0 (RTX 5090 sm_120), libnatten cutlass-fna verified"},
    },
    "models": {
        "Qwen/Qwen-Image-2512": {"revision": "25468b98e3276ca6700de15c6628e51b7de54a26", "size_gb": 57.7, "role": "reference image (BF16)"},
        "TencentARC/Pixal3D": {"revision": "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f", "size_gb": 29.0, "role": "single-view ckpts (+17 GB *_mv optional)"},
        "Ruicheng/moge-2-vitl": {"revision": "39c4d5e957afe587e04eec59dc2bcc3be5ecd968", "size_gb": 1.3, "role": "camera estimation"},
        "camenduru/dinov3-vitl16-pretrain-lvd1689m": {"revision": "3c276edd87d6f6e569ff0c4400e086807d0f3881", "size_gb": 1.2,
                                                      "role": "image features (ungated mirror used by upstream inference.py)"},
        "ZhengPeng7/BiRefNet": {"revision": "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4", "size_gb": 0.4,
                                "role": "background matting; replaces gated briaai/RMBG-2.0 (STUDIO_REMBG_MODEL to switch)"},
    },
    "host": {"os": "Windows 11 Pro 26200",
             "gpu": "NVIDIA GeForce RTX 5090 32 GB, GPU-52b50765-57a3-0ed4-78c9-4fa54276f65d",
             "driver": "616.92",
             "layout": ("native (no containers): a control plane (api + orchestrator + local Blender worker) plus "
                        "separately addressable image and pixal3d worker servers, see config.example.toml"),
             "measured_under": "Docker Desktop (WSL2 backend, 46 GiB VM RAM) - the container layout these pins were taken from"},
}


def freeze(python: str) -> dict[str, str] | None:
    try:
        r = subprocess.run([python, "-m", "pip", "freeze"], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ! cannot run {python}: {e}", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"  ! pip freeze failed for {python}: {r.stderr.strip()[:200]}", file=sys.stderr)
        return None
    pkgs = {}
    for line in r.stdout.splitlines():
        if "==" in line:
            k, v = line.split("==", 1)
        elif " @ " in line:
            k, v = line.split(" @ ", 1)
        else:
            continue
        pkgs[k.strip().lower().replace("-", "_")] = v.strip()
    return pkgs


def role_of(python: str) -> str | None:
    for role, marker in ROLE_MARKERS.items():
        try:
            r = subprocess.run([python, "-c", f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec({marker!r}) else 1)"],
                               capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0:
            return role
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", action="append", default=[], metavar="ROLE=PYTHON",
                    help="interpreter of a role's environment; repeatable (default: this interpreter)")
    ap.add_argument("--out", default=str(MANIFEST))
    a = ap.parse_args()

    targets: dict[str, str] = {}
    if a.env:
        for item in a.env:
            role, _, python = item.partition("=")
            if role not in ROLES or not python:
                print(f"--env expects ROLE=PYTHON with ROLE in {', '.join(ROLES)}; got {item!r}", file=sys.stderr)
                return 2
            targets[role] = python
    else:
        role = role_of(sys.executable)
        if role is None:
            print(f"{sys.executable} is not one of the roles ({', '.join(ROLES)}); pass --env ROLE=PYTHON",
                  file=sys.stderr)
            return 2
        targets[role] = sys.executable

    out = json.loads(Path(a.out).read_text(encoding="utf-8")) if Path(a.out).is_file() else {}
    out.update(PINS)
    out["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out.setdefault("environments", {})
    out.setdefault("summary", {})
    out["note"] = ("Package sets are the reference pins. asset-studio no longer runs in containers: each role is a "
                   "native environment installed from requirements/<role>.txt.")

    for role, python in targets.items():
        pkgs = freeze(python)
        if pkgs is None:
            continue
        key = {k: pkgs.get(k.lower().replace("-", "_")) for k in KEY_PACKAGES if pkgs.get(k.lower().replace("-", "_"))}
        out["environments"][role] = {"python": python, "installed_from": f"requirements/{role}.txt",
                                     "key_packages": key, "all_packages": pkgs}
        out["summary"][role] = key
        print(f"{role}: {python}\n  {json.dumps(key)}")

    p = Path(a.out)
    p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
