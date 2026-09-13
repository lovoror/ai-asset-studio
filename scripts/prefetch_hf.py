"""Prefetch every Hugging Face weight repo the pipeline needs, at pinned revisions.

Runs inside a container with HF_HOME pointing at the shared models volume.
Usage: python prefetch_hf.py [--only qwen|pixal3d|aux] [--mv]
"""
import argparse, json, os, sys, time
from pathlib import Path
from huggingface_hub import snapshot_download

PINS = {
    "qwen":    [("Qwen/Qwen-Image-2512", "25468b98e3276ca6700de15c6628e51b7de54a26", None)],
    "pixal3d": [("TencentARC/Pixal3D", "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f",
                 ["pipeline.json", "pipeline_mv.json", "ckpts/*_bf16.*", "ckpts/*_fp16.*"])],
    "pixal3d_mv": [("TencentARC/Pixal3D", "b0cb2e1b794cab9aa0ac38a95d794a4d9337437f", ["ckpts/*_mv.*"])],
    "qwen_lightning": [("lightx2v/Qwen-Image-2512-Lightning", "a52649c9d0f6e1a248bff13f0df33bb8a2abdb52",
                        ["Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors", "Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors"])],
    # configs/tokenizers/VAEs only: the big transformer + text-encoder weights are reused from the ComfyUI models folder
    "extra_configs": [
        ("black-forest-labs/FLUX.2-klein-4B", "e7b7dc27f91deacad38e78976d1f2b499d76a294",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]),
        ("Tongyi-MAI/Z-Image-Turbo", "f332072aa78be7aecdf3ee76d5c247082da564a6",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]),
        # gated (FLUX Non-Commercial License; accept on huggingface.co first): configs only, weights come from the ComfyUI fp8 files
        ("black-forest-labs/FLUX.2-klein-9B", "92196c8e11f7b6cf2b7493e037d8c5345c559216",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*", "transformer/config.json", "vae/*"]),  # + bf16 Qwen3-8B text encoder (16 GB)
        ("black-forest-labs/FLUX.2-klein-base-9B", "32773329fbe7e81a90ef971740e8ba4b0364ecf3",
         ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]),
    ],
    "aux": [
        ("Ruicheng/moge-2-vitl", "39c4d5e957afe587e04eec59dc2bcc3be5ecd968", None),
        ("camenduru/dinov3-vitl16-pretrain-lvd1689m", "3c276edd87d6f6e569ff0c4400e086807d0f3881", None),
        ("ZhengPeng7/BiRefNet", "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4", None),
        # Pixal3D's upstream matting model (gated: accept the license on huggingface.co/briaai/RMBG-2.0); BiRefNet is the open fallback
        ("briaai/RMBG-2.0", "5df4c9c76d8170882c34f6986e848ee07fd0ba43", ["*.json", "*.py", "model.safetensors"]),
    ],
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=None)
    ap.add_argument("--mv", action="store_true", help="also fetch multi-view checkpoints (+17 GB)")
    a = ap.parse_args()
    groups = a.only or ["qwen", "qwen_lightning", "extra_configs", "pixal3d", "aux"]
    if a.mv:
        groups.append("pixal3d_mv")
    os.environ.pop("HF_HUB_OFFLINE", None)
    for g in groups:
        for repo, rev, patterns in PINS[g]:
            t = time.time()
            print(f"[prefetch] {repo}@{rev} patterns={patterns}", flush=True)
            path = snapshot_download(repo, revision=rev, allow_patterns=patterns, max_workers=8)
            refs = Path(os.environ.get("HF_HOME", "/models/hf")) / "hub" / ("models--" + repo.replace("/", "--")) / "refs"
            refs.mkdir(parents=True, exist_ok=True)
            (refs / "main").write_text(rev)  # lets revision='main' resolve offline to the pinned snapshot
            print(f"[prefetch] done {repo} -> {path} ({time.time()-t:.0f}s)", flush=True)
    print("[prefetch] all done")

if __name__ == "__main__":
    main()
