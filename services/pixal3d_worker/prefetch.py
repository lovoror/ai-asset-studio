"""Prefetch every auxiliary weight the Pixal3D stage needs (run once, online), then verify offline loading.

  python -m pixal3d_worker.prefetch            # download (Pixal3D ckpts, MoGe-2, DINOv3, BiRefNet, NAF repo+weights)
  python -m pixal3d_worker.prefetch --mv       # also the multi-view checkpoints (+17 GB)
  python -m pixal3d_worker.prefetch --verify   # offline load of every component (no network needed)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PINS = {
    "TencentARC/Pixal3D": "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f",
    "Ruicheng/moge-2-vitl": "39c4d5e957afe587e04eec59dc2bcc3be5ecd968",
    "camenduru/dinov3-vitl16-pretrain-lvd1689m": "3c276edd87d6f6e569ff0c4400e086807d0f3881",
    "ZhengPeng7/BiRefNet": "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4",
}
NAF_COMMIT = "37f2dfc180f2de53d98bd601109c0da0dd6b0f43"
NAF_WEIGHTS = "https://github.com/valeoai/NAF/releases/download/model/naf_release.pth"
NAF_WEIGHTS_SHA256 = None  # filled into the dependency manifest after first download


def pin_ref_main(repo: str, sha: str):
    """Make `from_pretrained(repo)` (revision 'main') resolve offline to the pinned snapshot."""
    root = Path(os.environ.get("HF_HOME", "/models/hf")) / "hub" / ("models--" + repo.replace("/", "--")) / "refs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "main").write_text(sha)


def torch_hub_dir() -> Path:
    return Path(os.environ.get("TORCH_HOME", "/models/torch")) / "hub"


def prefetch(mv: bool):
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    from huggingface_hub import snapshot_download

    pats = ["pipeline.json", "pipeline_mv.json", "ckpts/*_bf16.*", "ckpts/*_fp16.*"]
    if mv:
        pats.append("ckpts/*_mv.*")
    for repo, rev in PINS.items():
        print(f"[prefetch] {repo}@{rev}", flush=True)
        snapshot_download(repo, revision=rev, allow_patterns=pats if repo == "TencentARC/Pixal3D" else None, max_workers=8)
        pin_ref_main(repo, rev)
    # NAF: torch.hub expects <hub>/valeoai_NAF_main (ref 'main'); we pin the archive to a commit instead of trusting HEAD.
    hub = torch_hub_dir()
    repo_dir = hub / "valeoai_NAF_main"
    if not (repo_dir / "hubconf.py").exists():
        url = f"https://github.com/valeoai/NAF/archive/{NAF_COMMIT}.zip"
        print(f"[prefetch] NAF repo {url}", flush=True)
        data = urllib.request.urlopen(url, timeout=120).read()
        hub.mkdir(parents=True, exist_ok=True)
        tmp = hub / "_naf_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
        inner = next(tmp.iterdir())
        shutil.rmtree(repo_dir, ignore_errors=True)
        shutil.move(str(inner), str(repo_dir))
        shutil.rmtree(tmp, ignore_errors=True)
        (repo_dir / "PINNED_COMMIT").write_text(NAF_COMMIT)
    ck = hub / "checkpoints" / "naf_release.pth"
    if not ck.exists():
        print(f"[prefetch] NAF weights {NAF_WEIGHTS}", flush=True)
        ck.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(NAF_WEIGHTS, timeout=600) as r, open(ck, "wb") as f:
            shutil.copyfileobj(r, f)
    print("[prefetch] done", flush=True)


def verify():
    """Load every component with the network disabled by environment (HF_HUB_OFFLINE=1)."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("ATTN_BACKEND", "flash_attn")
    sys.path.insert(0, "/opt/pixal3d")
    import torch

    from pixal3d_worker.generate import ensure_local_pipeline_dir

    report = {"torch": torch.__version__, "cuda": torch.version.cuda, "device": torch.cuda.get_device_name(0)}
    import inference as up

    model_path = ensure_local_pipeline_dir(os.environ.get("STUDIO_REMBG_MODEL", "ZhengPeng7/BiRefNet"))
    pipe = up.init_pipeline(model_path, low_vram=True)
    report["pipeline_models"] = sorted(pipe.models.keys())
    report["rembg"] = type(pipe.rembg_model).__name__
    report["naf_loaded"] = all(getattr(getattr(pipe, a), "naf_model", None) is not None
                               for a in ("image_cond_model_shape_512", "image_cond_model_shape_1024", "image_cond_model_tex_1024"))
    m = up.load_moge_model(device="cuda")
    report["moge"] = type(m).__name__
    del m
    torch.cuda.empty_cache()
    import natten
    report["natten"] = natten.__version__
    report["natten_has_libnatten"] = bool(getattr(natten, "has_cuda", lambda: False)() if hasattr(natten, "has_cuda") else False)
    # NAF neighbourhood attention smoke test on the GPU (this is what needs libnatten on sm_120)
    naf = pipe.image_cond_model_shape_512.naf_model.cuda()
    with torch.no_grad():
        lr = torch.randn(1, 1024, 32, 32, device="cuda")
        img = torch.rand(1, 3, 512, 512, device="cuda")
        try:
            out = naf(img, lr, (512, 512))  # same call as pixal3d image_conditioned_proj: (guide, lr_features, target_size)
            report["naf_forward"] = list(out.shape)
        except Exception as e:  # noqa: BLE001
            report["naf_forward_error"] = f"{type(e).__name__}: {e}"[:300]
    print(json.dumps(report, indent=2))
    if "naf_forward_error" in report:
        sys.exit(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mv", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    if a.verify:
        verify()
    else:
        prefetch(a.mv)
