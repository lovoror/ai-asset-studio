"""Generate manifests/dependency-manifest.json: pinned commits, model revisions, image digests and frozen
package versions from every built image. Run from the repo root on the host: python scripts/write_manifest.py"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {
    "studio": "asset-studio/studio:local",
    "image-worker": "asset-studio/image-worker:local",
    "pixal3d-worker": "asset-studio/pixal3d-worker:local",
    "blender": "asset-studio/blender:local",
}
KEY_PACKAGES = ["torch", "torchvision", "triton", "diffusers", "transformers", "accelerate", "huggingface_hub", "safetensors",
                "natten", "flash_attn", "nvdiffrast", "o_voxel", "cumesh", "flex_gemm", "utils3d", "utils3d_moge", "moge",
                "kornia", "timm", "trimesh", "pillow", "opencv-python-headless", "bpy", "meshoptimizer", "fast-simplification", "fastapi", "uvicorn", "pydantic",
                "pygltflib", "numpy", "einops"]

PINS = {
    "sources": {
        "TencentARC/Pixal3D": {"url": "https://github.com/TencentARC/Pixal3D", "branch": "master (default; NOT `paper`)",
                               "commit": "f7cf38429b0bd264f1995f0f8743a88b1c728b94", "date": "2026-09-01"},
        "microsoft/TRELLIS.2": {"url": "https://github.com/microsoft/TRELLIS.2", "branch": "main",
                                "commit": "75fbf0183001ed9876c8dbb35de6b68552ee08bd", "note": "base image trellis2:rtx5090 built from C:/Users/Zorro/TRELLIS.2/Dockerfile"},
        "microsoft/MoGe": {"commit": "74fbce054ebed49800de42d0ad0e83495065719a", "installed": "--no-deps (keeps base FlexGEMM build)"},
        "EasternJournalist/utils3d-moge": {"commit": "62f09d58509485564e24d5d9f6aac9ee9ebc0c37"},
        "EasternJournalist/pipeline": {"commit": "1c511390d90226c00c101f34b84df26a0f8789b4"},
        "valeoai/NAF": {"commit": "37f2dfc180f2de53d98bd601109c0da0dd6b0f43",
                        "weights": "https://github.com/valeoai/NAF/releases/download/model/naf_release.pth",
                        "note": "torch.hub cache pinned to this commit under /models/torch/hub/valeoai_NAF_main"},
        "utils3d (Pixal3D-specified wheel)": {"url": "https://github.com/LDYang694/Storages/releases/download/20260430/utils3d-0.0.2-py3-none-any.whl"},
        "NATTEN": {"version": "0.21.0 (Pixal3D requirement)", "build": "from sdist, NATTEN_CUDA_ARCH=12.0 (RTX 5090 sm_120), libnatten cutlass-fna verified"},
    },
    "models": {
        "Qwen/Qwen-Image-2512": {"revision": "25468b98e3276ca6700de15c6628e51b7de54a26", "size_gb": 57.7, "role": "reference image (BF16)"},
        "TencentARC/Pixal3D": {"revision": "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f", "size_gb": 29.0, "role": "single-view ckpts (+17 GB *_mv optional)"},
        "Ruicheng/moge-2-vitl": {"revision": "39c4d5e957afe587e04eec59dc2bcc3be5ecd968", "size_gb": 1.3, "role": "camera estimation"},
        "camenduru/dinov3-vitl16-pretrain-lvd1689m": {"revision": "3c276edd87d6f6e569ff0c4400e086807d0f3881", "size_gb": 1.2, "role": "image features (ungated mirror used by upstream inference.py)"},
        "ZhengPeng7/BiRefNet": {"revision": "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4", "size_gb": 0.4,
                                "role": "background matting; replaces gated briaai/RMBG-2.0 (STUDIO_REMBG_MODEL to switch)"},
    },
    "host": {"os": "Windows 11 Pro 26200", "gpu": "NVIDIA GeForce RTX 5090 32 GB, GPU-52b50765-57a3-0ed4-78c9-4fa54276f65d",
             "driver": "616.92", "docker": "Docker Desktop (WSL2 backend), nvidia runtime", "docker_vm_ram_gib": 46},
}


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def main():
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), **PINS, "images": {}}
    for name, image in IMAGES.items():
        digest = sh(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        freeze = sh(["docker", "run", "--rm", "--entrypoint", "python", image, "-m", "pip", "freeze"])
        pkgs = {}
        for line in freeze.splitlines():
            if "==" in line:
                k, v = line.split("==", 1)
                pkgs[k.lower().replace("-", "_")] = v
            elif " @ " in line:
                k, v = line.split(" @ ", 1)
                pkgs[k.lower().replace("-", "_")] = v
        key = {k: pkgs.get(k.lower().replace("-", "_")) for k in KEY_PACKAGES if pkgs.get(k.lower().replace("-", "_"))}
        out["images"][name] = {"image": image, "id": digest, "key_packages": key, "all_packages": pkgs}
        print(f"{name}: {digest[:19]} {json.dumps(key)}")
    out["summary"] = {n: v["key_packages"] for n, v in out["images"].items()}
    p = ROOT / "manifests" / "dependency-manifest.json"
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote", p)


if __name__ == "__main__":
    main()
