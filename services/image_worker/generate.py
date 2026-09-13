"""Reference-image stage: Qwen-Image-2512 (diffusers) -> N candidates -> transparent technical selection.

Memory strategy ("bf16_split"): everything stays BF16 (no quantisation).
  1. The Qwen2.5-VL text encoder (16.6 GB) is loaded on the GPU alone, both prompts are encoded, then it is freed.
  2. The 41 GB MMDiT transformer is loaded with an explicit device map: the first `gpu_resident_blocks` of its 60
     blocks live on the GPU, the rest on CPU and are streamed per forward by accelerate's block-level offload hooks.
     This never needs the whole transformer in host RAM (the Docker VM has ~46 GB) and never pins a duplicate copy.
  3. VAE on GPU. Documented full-quality sampling: 50 steps, true_cfg_scale 4.0, 1328x1328, negative prompt.
Runs as a one-shot subprocess so the driver releases all CUDA memory on exit.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import traceback
from pathlib import Path

MODEL_REVISION = "25468b98e3276ca6700de15c6628e51b7de54a26"


def local_snapshot(model_id: str) -> str:
    """Resolve the pinned snapshot directory from the HF cache (works offline; avoids diffusers' model_info() network
    call for sharded checkpoints)."""
    from huggingface_hub import snapshot_download

    return snapshot_download(model_id, revision=MODEL_REVISION)


def phase(name):
    print(f"[phase] {name}", flush=True)


def _rss_peak_mb():
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None


# ----------------------------------------------------------------------------------------------- model loading

def build_device_map(model_id: str, gpu_resident_blocks: int) -> tuple[dict, int]:
    """Explicit per-module device map: non-block modules + first K blocks on cuda:0, remaining blocks on cpu."""
    import torch
    from accelerate import init_empty_weights
    from diffusers import QwenImageTransformer2DModel

    cfg = QwenImageTransformer2DModel.load_config(model_id, subfolder="transformer")
    with init_empty_weights():
        skel = QwenImageTransformer2DModel.from_config(cfg)
    dm = {}
    n_blocks = 0
    for name, child in skel.named_children():
        if isinstance(child, torch.nn.ModuleList):
            n_blocks = len(child)
            for i in range(n_blocks):
                dm[f"{name}.{i}"] = 0 if i < gpu_resident_blocks else "cpu"
        else:
            dm[name] = 0
    for name, _ in skel.named_parameters(recurse=False):
        dm[name] = 0
    del skel
    return dm, n_blocks


def load_text_encoder(model_id: str):
    import torch
    from diffusers import QwenImagePipeline

    pipe = QwenImagePipeline.from_pretrained(model_id, transformer=None, vae=None, torch_dtype=torch.bfloat16)
    pipe.text_encoder.to("cuda")
    return pipe


def encode_prompts(pipe, prompt: str, negative: str, max_len: int = 512):
    import torch

    with torch.no_grad():
        pe, pm = pipe.encode_prompt(prompt=prompt, device="cuda", num_images_per_prompt=1, max_sequence_length=max_len)
        ne, nm = pipe.encode_prompt(prompt=negative, device="cuda", num_images_per_prompt=1, max_sequence_length=max_len)
    return pe, pm, ne, nm


def lightning_scheduler():
    """Scheduler config used by the official Qwen-Image-Lightning diffusers script (shift=3, exponential time shift)."""
    import math

    from diffusers import FlowMatchEulerDiscreteScheduler

    return FlowMatchEulerDiscreteScheduler.from_config({
        "base_image_seq_len": 256, "base_shift": math.log(3), "invert_sigmas": False, "max_image_seq_len": 8192,
        "max_shift": math.log(3), "num_train_timesteps": 1000, "shift": 1.0, "shift_terminal": None, "stochastic_sampling": False,
        "time_shift_type": "exponential", "use_beta_sigmas": False, "use_dynamic_shifting": True, "use_exponential_sigmas": False,
        "use_karras_sigmas": False})


def load_generator(model_id: str, gpu_resident_blocks: int, lightning: dict | None = None):
    import torch
    from diffusers import QwenImagePipeline, QwenImageTransformer2DModel

    dm, n_blocks = build_device_map(model_id, gpu_resident_blocks)
    print(f"[load] transformer device map: {min(gpu_resident_blocks, n_blocks)}/{n_blocks} blocks GPU-resident, rest CPU-offloaded", flush=True)
    transformer = QwenImageTransformer2DModel.from_pretrained(model_id, subfolder="transformer", torch_dtype=torch.bfloat16, device_map=dm)
    kw = {}
    if lightning:
        kw["scheduler"] = lightning_scheduler()
    pipe = QwenImagePipeline.from_pretrained(model_id, transformer=transformer, text_encoder=None, tokenizer=None, torch_dtype=torch.bfloat16, **kw)
    if lightning:
        from huggingface_hub import snapshot_download

        d = snapshot_download(lightning["repo"], revision=lightning["revision"], allow_patterns=[lightning["file"]])
        t0 = time.time()
        pipe.load_lora_weights(os.path.join(d, lightning["file"]), adapter_name="lightning")
        pipe.fuse_lora(adapter_names=["lightning"], lora_scale=1.0)
        pipe.unload_lora_weights()
        print(f"[load] fused Lightning LoRA {lightning['file']} in {time.time() - t0:.1f}s", flush=True)
    pipe.vae.to("cuda")
    pipe.vae.enable_tiling()  # keeps the 1328x1328 decode peak small; the transformer weights are the real VRAM cost
    pipe.set_progress_bar_config(disable=False)
    return pipe, n_blocks


# ----------------------------------------------------------------------------------------------- candidate checks

def analyze_candidate(path: Path) -> dict:
    """Transparent technical checks: background plainness, foreground framing/margins/clipping, centring, sharpness.
    These measure reconstructability, not artistic quality."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.float32) / 255.0
    H, W = a.shape[:2]
    gray = a.mean(-1)
    # Foreground from edges (robust to soft studio vignettes/gradients that fool a global colour threshold):
    # Sobel magnitude -> threshold -> close -> fill holes -> open -> keep the significant components.
    gx = ndimage.sobel(gray, axis=1)
    gy = ndimage.sobel(gray, axis=0)
    grad = np.hypot(gx, gy)
    fg = grad > 0.08
    fg = ndimage.binary_closing(fg, iterations=6)
    fg = ndimage.binary_fill_holes(fg)
    fg = ndimage.binary_opening(fg, iterations=3)
    lbl, n = ndimage.label(fg)
    if n > 1:  # keep components that are at least 2% of the largest (drops speckles, keeps real parts)
        sizes = ndimage.sum(fg, lbl, range(1, n + 1))
        keep = [i + 1 for i, s in enumerate(sizes) if s >= 0.02 * sizes.max()]
        fg = np.isin(lbl, keep)
        n_parts = len(keep)
    else:
        n_parts = n
    # Background plainness: local texture in the border ring (16px blocks), insensitive to smooth gradients.
    b = max(8, H // 40)
    ring = np.zeros((H, W), bool)
    ring[:b], ring[-b:], ring[:, :b], ring[:, -b:] = True, True, True, True
    ring &= ~ndimage.binary_dilation(fg, iterations=8)
    blur = ndimage.uniform_filter(gray, 15)
    hp = np.abs(gray - blur)
    bg_std = float(hp[ring].mean() * 4) if ring.any() else 1.0  # ~0 for plain, ~0.04+ for textured/busy
    bg = np.median(a[ring].reshape(-1, 3), axis=0) if ring.any() else a[0, 0]
    m = {"bg_color": [float(x) for x in bg], "bg_std": bg_std, "fg_fraction": float(fg.mean()), "fg_components": int(n_parts)}
    if fg.sum() < 0.02 * H * W:
        m.update({"score": 0.0, "reject": "no clear foreground object", "clipped": None})
        return m
    ys, xs = np.where(fg)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    margins = {"left": x0 / W, "right": (W - 1 - x1) / W, "top": y0 / H, "bottom": (H - 1 - y1) / H}
    clipped = min(margins.values()) < 0.005
    cx, cy = (x0 + x1) / 2 / W, (y0 + y1) / 2 / H
    off = float(np.hypot(cx - 0.5, cy - 0.5))
    gray = a.mean(-1)
    lap = ndimage.laplace(gray)[y0:y1 + 1, x0:x1 + 1]
    sharp = float(lap.var())
    m.update({"bbox": [int(x0), int(y0), int(x1), int(y1)], "margins": {k: float(v) for k, v in margins.items()}, "clipped": bool(clipped),
              "center_offset": off, "bbox_fill": float(fg[y0:y1 + 1, x0:x1 + 1].mean()), "sharpness": sharp})
    score = 1.0
    reasons = []
    if clipped:
        score -= 0.6
        reasons.append("object touches the image border (clipped)")
    mm = min(margins.values())
    if mm < 0.04:
        score -= 0.15
        reasons.append(f"small margin {mm:.2f}")
    ff = m["fg_fraction"]
    if ff < 0.15:
        score -= min(0.4, (0.15 - ff) * 3)
        reasons.append(f"object small in frame ({ff:.2f})")
    elif ff > 0.70:
        score -= min(0.4, (ff - 0.70) * 3)
        reasons.append(f"object fills frame ({ff:.2f})")
    if off > 0.06:
        score -= min(0.2, (off - 0.06) * 2)
        reasons.append(f"off-centre ({off:.2f})")
    if bg_std > 0.04:
        score -= min(0.4, (bg_std - 0.04) * 5)
        reasons.append(f"background not plain (std {bg_std:.3f})")
    if n_parts > 3:
        score -= 0.1
        reasons.append(f"{n_parts} separate foreground regions (possible multiple objects)")
    m["score"] = float(max(0.0, score))
    m["reasons"] = reasons
    return m


def external_eval(url: str, path: Path) -> dict | None:
    """Optional local vision evaluator (configured by the operator): POST the PNG, expect {"score": 0..1, "reason": str}."""
    if not url:
        return None
    try:
        import urllib.request

        data = path.read_bytes()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "image/png"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


# ----------------------------------------------------------------------------------------------- main

def run(req: dict):
    import torch
    from PIL import Image  # noqa: F401

    out_dir = Path(req["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    timings, vram, warnings = {}, {}, []
    t_all = time.time()
    family = req.get("family", "qwen")
    if family in ("klein", "zimage"):
        return run_single_gpu_family(req, family, out_dir, timings, vram, warnings, t_all)
    model_id = local_snapshot(req.get("model", "Qwen/Qwen-Image-2512"))

    phase("text_encoder")
    t0 = time.time()
    te = load_text_encoder(model_id)
    timings["load_text_encoder_s"] = round(time.time() - t0, 2)
    t0 = time.time()
    pe, pm, ne, nm = encode_prompts(te, req["prompt"], req["negative_prompt"])
    timings["encode_s"] = round(time.time() - t0, 2)
    vram["text_encoder_max_reserved_mib"] = int(torch.cuda.max_memory_reserved() // 2**20)
    print(f"[encode] prompt tokens {pe.shape[1]}, negative tokens {ne.shape[1]}", flush=True)
    te.text_encoder.to("cpu")
    del te
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    phase("transformer")
    t0 = time.time()
    pipe, n_blocks = load_generator(model_id, int(req.get("gpu_resident_blocks", 30)), req.get("lightning_lora"))
    timings["load_transformer_s"] = round(time.time() - t0, 2)
    vram["after_load_reserved_mib"] = int(torch.cuda.memory_reserved() // 2**20)

    phase("generate")

    def qwen_gen(prompt, negative, seed, w, h):
        with torch.inference_mode():
            return pipe(prompt_embeds=pe, prompt_embeds_mask=pm, negative_prompt_embeds=ne, negative_prompt_embeds_mask=nm,
                        width=w, height=h, num_inference_steps=int(req["steps"]), true_cfg_scale=float(req["true_cfg_scale"]),
                        generator=torch.Generator(device="cuda").manual_seed(int(seed))).images[0]

    cands = generate_candidates(req, out_dir, qwen_gen)
    vram["generate_max_reserved_mib"] = int(torch.cuda.max_memory_reserved() // 2**20)
    vram["generate_max_allocated_mib"] = int(torch.cuda.max_memory_allocated() // 2**20)
    timings["generate_s"] = round(sum(c["time_s"] for c in cands), 2)
    timings["per_candidate_s"] = [c["time_s"] for c in cands]
    extra = {"transformer_blocks": n_blocks, "gpu_resident_blocks": int(req.get("gpu_resident_blocks", 30)),
             "lightning_lora": (req.get("lightning_lora") or {}).get("file")}
    finish(req, out_dir, cands, timings, vram, warnings, t_all, extra)


def generate_candidates(req: dict, out_dir: Path, gen) -> list:
    import torch

    cands = []
    for i, seed in enumerate(req["seeds"]):
        t0 = time.time()
        img = gen(req["prompt"], req["negative_prompt"], int(seed), int(req["width"]), int(req["height"]))
        torch.cuda.synchronize()
        dt = round(time.time() - t0, 2)
        cdir = out_dir / "candidates"
        cdir.mkdir(exist_ok=True)
        f = cdir / f"cand_{i:02d}.png"
        img.save(f)
        metrics = analyze_candidate(f)
        ext = external_eval(req.get("evaluator_url", ""), f)
        if ext and "score" in ext:
            metrics["external"] = ext
            metrics["score"] = 0.5 * metrics["score"] + 0.5 * float(ext["score"])
        elif ext:
            metrics["external"] = ext
        cands.append({"file": f"candidates/{f.name}", "seed": int(seed), "time_s": dt, "metrics": metrics})
        print(f"[candidate {i}] seed={seed} {dt}s score={metrics.get('score'):.2f} {metrics.get('reasons') or metrics.get('reject') or ''}", flush=True)
    return cands


def run_single_gpu_family(req: dict, family: str, out_dir: Path, timings: dict, vram: dict, warnings: list, t_all: float):
    """Klein / Z-Image: everything fits on the GPU at once; load, generate all candidates, finish."""
    import torch

    from image_worker.backends import KleinBackend, ZImageBackend

    phase("load")
    t0 = time.time()
    backend = (KleinBackend if family == "klein" else ZImageBackend)(req)
    timings["load_s"] = round(time.time() - t0, 2)
    vram["after_load_reserved_mib"] = int(torch.cuda.memory_reserved() // 2**20)
    torch.cuda.reset_peak_memory_stats()
    phase("generate")
    cands = generate_candidates(req, out_dir, backend.gen)
    vram["generate_max_reserved_mib"] = int(torch.cuda.max_memory_reserved() // 2**20)
    vram["generate_max_allocated_mib"] = int(torch.cuda.max_memory_allocated() // 2**20)
    timings["generate_s"] = round(sum(c["time_s"] for c in cands), 2)
    timings["per_candidate_s"] = [c["time_s"] for c in cands]
    finish(req, out_dir, cands, timings, vram, warnings, t_all, backend.describe())


def finish(req: dict, out_dir: Path, cands: list, timings: dict, vram: dict, warnings: list, t_all: float, extra: dict):
    ranked = sorted(cands, key=lambda c: c["metrics"].get("score", 0), reverse=True)
    best = ranked[0]
    if best["metrics"].get("score", 0) < 0.4:
        warnings.append(f"best reference candidate scored only {best['metrics'].get('score', 0):.2f}: "
                        f"{best['metrics'].get('reasons') or best['metrics'].get('reject')}")
    selection = {"selected": best["file"], "method": "technical_checks" + ("+external_evaluator" if req.get("evaluator_url") else ""),
                 "ranking": [{"file": c["file"], "score": c["metrics"].get("score"), "reasons": c["metrics"].get("reasons") or c["metrics"].get("reject")} for c in ranked]}
    (out_dir / "selection.json").write_text(json.dumps({"selection": selection, "candidates": cands}, indent=2), encoding="utf-8")
    timings["total_s"] = round(time.time() - t_all, 2)
    result = {"selected": best["file"], "candidates": cands, "selection": selection, "warnings": warnings,
              "stats": {"timings_s": timings, "vram": vram, "peak_rss_mb": _rss_peak_mb(), "model": req.get("model_id"),
                        "family": req.get("family", "qwen"), **extra}}
    tmp = out_dir / "output.json.tmp"
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(tmp, out_dir / "output.json")
    print(f"[done] {json.dumps(result['stats'])}", flush=True)


def selftest():
    """Offline load of text encoder + transformer + VAE and a 4-step 512px generation."""
    import torch

    os.environ["HF_HUB_OFFLINE"] = "1"
    model_id = local_snapshot("Qwen/Qwen-Image-2512")
    te = load_text_encoder(model_id)
    pe, pm, ne, nm = encode_prompts(te, "a red cube on a grey background", "text")
    te.text_encoder.to("cpu")
    del te
    gc.collect()
    torch.cuda.empty_cache()
    pipe, n = load_generator(model_id, 30)
    with torch.inference_mode():
        img = pipe(prompt_embeds=pe, prompt_embeds_mask=pm, negative_prompt_embeds=ne, negative_prompt_embeds_mask=nm, width=512, height=512,
                   num_inference_steps=4, true_cfg_scale=4.0, generator=torch.Generator(device="cuda").manual_seed(1)).images[0]
    print(f"selftest ok: {img.size}, blocks={n}, max reserved {torch.cuda.max_memory_reserved() // 2**20} MiB")


def list_models(path: str):
    import yaml

    from image_worker.backends import available

    out = []
    for e in yaml.safe_load(Path(path).read_text(encoding="utf-8")):
        ok, why = available(e)
        out.append({"id": e["id"], "available": ok, "reason": why})
    print(json.dumps(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--request")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", metavar="IMAGE_MODELS_YAML")
    a = ap.parse_args()
    try:
        if a.list:
            list_models(a.list)
        elif a.selftest:
            selftest()
        else:
            run(json.loads(Path(a.request).read_text(encoding="utf-8")))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
