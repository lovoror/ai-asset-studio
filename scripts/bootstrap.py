#!/usr/bin/env python3
"""Install the Python dependencies for one role. Cross-platform, no Docker.

  python scripts/bootstrap.py --role control
  python scripts/bootstrap.py --role blender
  python scripts/bootstrap.py --role image
  python scripts/bootstrap.py --role pixal3d
  python scripts/bootstrap.py --check          # only report what is missing

Each role is a separate environment because their dependencies conflict (torch versions, numpy, bpy). Install
each into its own virtual environment. The Blender role needs its own interpreter: bpy publishes exactly one
interpreter tag per release and there is no cp312 build at all (4.5/5.0 are cp311, 5.1+ are cp313), so it usually
cannot share the control plane's python. `uv` provisions one without touching the system:

    uv python install 3.11
    uv venv --python 3.11 .venv-blender
    uv pip install --python .venv-blender/Scripts/python.exe -r requirements/blender.txt   # or .venv-blender/bin/python

then point config.toml at it:  [python] blender = ".venv-blender/Scripts/python.exe"
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "requirements"

# module name -> pip requirement, per role.
CHECKS: dict[str, list[tuple[str, str]]] = {
    "control": [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic"), ("httpx", "httpx"),
                ("yaml", "PyYAML"), ("numpy", "numpy"), ("PIL", "pillow"), ("trimesh", "trimesh"),
                ("pygltflib", "pygltflib")],
    "blender": [("bpy", "bpy"), ("numpy", "numpy"), ("PIL", "pillow"), ("trimesh", "trimesh"),
                ("meshoptimizer", "meshoptimizer"), ("fast_simplification", "fast-simplification")],
    "image": [("torch", "torch"), ("diffusers", "diffusers"), ("transformers", "transformers"),
              ("accelerate", "accelerate"), ("PIL", "pillow"), ("numpy", "numpy")],
    "pixal3d": [("torch", "torch"), ("natten", "natten"), ("diffusers", "diffusers"),
                ("transformers", "transformers"), ("cv2", "opencv-python-headless"), ("trimesh", "trimesh")],
    "comfy": [("httpx", "httpx"), ("PIL", "pillow"), ("numpy", "numpy"), ("scipy", "scipy"), ("yaml", "PyYAML")],
}

NOTES = {
    "pixal3d": ("The 3D environment needs its CUDA extensions (flash-attn, nvdiffrast, CuMesh, FlexGEMM, "
                "o-voxel, NATTEN sm_120) built for this GPU. Follow TRELLIS.2's installation instructions "
                "first; see requirements/pixal3d.txt. Anything missing above has to be built, not pip-installed."),
    "blender": ("bpy ships one interpreter tag per release and never a cp312: 4.5/5.0 need Python 3.11, 5.1+ need "
                "3.13. Use uv to get one (see requirements/blender.txt) and set [python] blender in config.toml."),
    "comfy": ("For a machine that only proxies stages to ComfyUI servers: no torch needed at all. If this "
              "machine should also generate locally, install that role instead (image / pixal3d)."),
}


def missing(role: str) -> list[str]:
    out = []
    for module, pkg in CHECKS[role]:
        try:
            if importlib.util.find_spec(module) is None:
                out.append(pkg)
        except (ImportError, ValueError):
            out.append(pkg)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", choices=sorted(CHECKS), default="control")
    ap.add_argument("--check", action="store_true", help="report missing packages and exit")
    ap.add_argument("--upgrade", action="store_true", help="pass --upgrade to pip")
    a = ap.parse_args()

    miss = missing(a.role)
    if a.check:
        print(f"{a.role}: " + ("all dependencies present" if not miss else "missing " + ", ".join(miss)))
        if miss and a.role in NOTES:
            print(f"  note: {NOTES[a.role]}")
        return 0 if not miss else 1

    # bpy publishes exactly one interpreter tag per release (4.5/5.0: cp311, 5.1+: cp313) and no cp312 ever, so
    # "not 3.13" is not the right test - only 3.11 matches the pinned bpy 4.5.13 wheel.
    if a.role == "blender" and sys.version_info[:2] != (3, 11):
        print(f"! bpy==4.5.13 has no wheel for python {sys.version.split()[0]}: it is a cp311 build. Either run "
              f"this with Python 3.11, or switch to bpy 5.1+ (cp313) - see requirements/blender.txt.", file=sys.stderr)
        return 2

    req = REQ / f"{a.role}.txt"
    if not req.is_file():
        print(f"missing {req}", file=sys.stderr)
        return 2
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    if a.upgrade:
        cmd.append("--upgrade")
    print(f"[bootstrap] {sys.executable} -m pip install -r {req.relative_to(ROOT)}", flush=True)
    rc = subprocess.call(cmd)
    if rc == 0 and a.role in NOTES and missing(a.role):
        print(f"  note: {NOTES[a.role]}")
    left = missing(a.role)
    print(f"[bootstrap] {a.role}: " + ("ready" if not left else "still missing " + ", ".join(left)))
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())
