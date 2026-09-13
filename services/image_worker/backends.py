"""Image model backends for the reference stage. Every backend returns a callable `gen(prompt, negative, seed, w, h) -> PIL.Image`
plus a `describe()` dict for the manifest. All models run in this container with diffusers; nothing calls another service.

  qwen   : Qwen-Image-2512 (BF16, text encoder loaded/freed first, transformer block-offloaded); optional Lightning LoRA.
  klein  : FLUX.2 Klein 4B — transformer from the on-disk BFL-format file (`Flux2Transformer2DModel.from_single_file`),
           Qwen3-4B text encoder from the on-disk transformers-format file, VAE/tokenizer/scheduler from the HF config repo.
  zimage : Z-Image Turbo — same pattern with `ZImageTransformer2DModel.from_single_file` and `Qwen3Model`.
"""
from __future__ import annotations

import gc
import json
import os
import time
from pathlib import Path

EXTRA_MODELS_DIR = Path(os.environ.get("STUDIO_EXTRA_MODELS_DIR", "/extra-models"))


def log(msg):
    print(msg, flush=True)


CONFIG_PATTERNS = ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*.json", "transformer/config.json", "vae/*"]


def _snapshot(repo: str, revision: str, patterns=None) -> str:
    """Resolve the prefetched config-only snapshot (works offline; the big weights come from the extra-models folder)."""
    from huggingface_hub import snapshot_download

    return snapshot_download(repo, revision=revision, allow_patterns=patterns or CONFIG_PATTERNS)


def _local_copy(src: Path) -> Path:
    """The extra-models folder is a Windows bind mount (slow, ~300 MB/s). Cache a copy on the Linux volume once so later
    loads take seconds instead of a minute. Copies are keyed by name+size and live under /models/extra-cache."""
    cache = Path(os.environ.get("STUDIO_EXTRA_CACHE_DIR", "/models/extra-cache"))
    try:
        cache.mkdir(parents=True, exist_ok=True)
        dst = cache / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            return dst
        import shutil

        t0 = time.time()
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        log(f"[cache] copied {src.name} ({src.stat().st_size / 1e9:.1f} GB) to the models volume in {time.time() - t0:.0f}s")
        return dst
    except Exception as e:  # noqa: BLE001
        log(f"[cache] using {src} directly ({e})")
        return src


def load_dequantized(path: Path):
    """Load a safetensors file, dequantizing ComfyUI 'scaled fp8' tensors (float8_e4m3fn weight + scalar `weight_scale`)
    to bf16. `input_scale` (activation quant) and `comfy_quant` metadata blobs are dropped."""
    import torch
    from safetensors import safe_open

    from safetensors.torch import load_file, save_file

    # dequantized bf16 copy cached next to the source copy (only for files that actually contain fp8 tensors)
    cached = path.with_name(path.stem + ".bf16.safetensors")
    if cached.exists():
        return load_file(str(cached))
    out = {}
    with safe_open(str(path), "pt") as f:
        keys = set(f.keys())
        n_deq = 0
        for k in keys:
            if k.endswith((".weight_scale", ".input_scale", ".comfy_quant")):
                continue
            t = f.get_tensor(k)
            if t.dtype == torch.float8_e4m3fn:
                sk = k[: -len(".weight")] + ".weight_scale" if k.endswith(".weight") else k + "_scale"
                scale = f.get_tensor(sk).float() if sk in keys else torch.tensor(1.0)
                t = (t.to(torch.float32) * scale).to(torch.bfloat16)
                n_deq += 1
            elif t.dtype == torch.float32:
                t = t.to(torch.bfloat16)
            out[k] = t
    if n_deq:
        log(f"[dequant] {path.name}: {n_deq} fp8 tensors -> bf16")
        try:
            t0 = time.time()
            tmp = cached.with_suffix(".part")
            save_file({k: v.contiguous() for k, v in out.items()}, str(tmp))
            os.replace(tmp, cached)
            log(f"[cache] wrote dequantized {cached.name} in {time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            log(f"[cache] could not write {cached.name}: {e}")
    return out


def _load_text_encoder_from_file(cls, config_dir: str, weights: Path, strip_model_prefix: bool):
    """Instantiate a transformers Qwen3 model from config and fill it from a single safetensors file (ComfyUI layout,
    bf16 or scaled-fp8)."""
    import torch
    from transformers import AutoConfig
    from transformers.initialization import no_init_weights

    cfg = AutoConfig.from_pretrained(config_dir)
    with no_init_weights():  # allocate on CPU without random init (buffers such as rotary tables are computed normally)
        model = cls(cfg)
    sd = load_dequantized(_local_copy(weights))
    if strip_model_prefix:
        sd = {k[6:] if k.startswith("model.") else k: v for k, v in sd.items()}
    sd = {k: v.to(torch.bfloat16) for k, v in sd.items()}
    missing, unexpected = model.load_state_dict(sd, strict=False, assign=True)
    if hasattr(model, "tie_weights"):
        model.tie_weights()
    missing = [m for m in missing if "lm_head" not in m]
    if missing or unexpected:
        log(f"[text_encoder] missing={missing[:5]} unexpected={unexpected[:5]}")
    if missing:
        raise RuntimeError(f"text encoder file {weights.name} does not match {config_dir}: missing {len(missing)} tensors")
    model.eval().requires_grad_(False)
    return model


class KleinBackend:
    family = "klein"

    def __init__(self, params: dict):
        import torch
        from diffusers import AutoencoderKLFlux2, FlowMatchEulerDiscreteScheduler, Flux2KleinPipeline, Flux2Transformer2DModel
        from transformers import AutoTokenizer, Qwen3ForCausalLM

        t0 = time.time()
        cfg = _snapshot(params["config_repo"], params["config_revision"])
        tf_path = EXTRA_MODELS_DIR / params["transformer_file"]
        te_path = EXTRA_MODELS_DIR / params["text_encoder_file"]
        self.params = params
        self.distilled = bool(params.get("distilled", True))
        self.sequential = bool(params.get("sequential", False))  # 9B: text encoder (16 GB) and transformer (18 GB) can't share 32 GB
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(cfg, subfolder="scheduler")
        tokenizer = AutoTokenizer.from_pretrained(cfg, subfolder="tokenizer")
        vae = AutoencoderKLFlux2.from_pretrained(cfg, subfolder="vae", torch_dtype=torch.bfloat16)
        if params.get("text_encoder_from_hub"):
            # exact bf16 Qwen3-8B from the (gated) HF repo; the on-disk "fp8mixed" file mixes fp8 and NVFP4 layers
            te_cfg = _snapshot(params.get("text_encoder_repo", params["config_repo"]), params.get("text_encoder_revision", params["config_revision"]),
                               CONFIG_PATTERNS + ["text_encoder/*"])
            text_encoder = Qwen3ForCausalLM.from_pretrained(os.path.join(te_cfg, "text_encoder"), torch_dtype=torch.bfloat16)
            text_encoder.eval().requires_grad_(False)
            te_path = Path(te_cfg) / "text_encoder"
        else:
            text_encoder = _load_text_encoder_from_file(Qwen3ForCausalLM, os.path.join(cfg, "text_encoder"), te_path, strip_model_prefix=False)
        self.cache = {}
        if self.sequential:
            # encode later, per prompt, with only the text encoder resident; the transformer is loaded on first gen()
            self.pipe = Flux2KleinPipeline(scheduler=scheduler, vae=vae, text_encoder=text_encoder.to("cuda"), tokenizer=tokenizer,
                                           transformer=None, is_distilled=self.distilled)
            self._tf_path, self._cfg = tf_path, cfg
            self.transformer = None
        else:
            sd = load_dequantized(_local_copy(tf_path))
            transformer = Flux2Transformer2DModel.from_single_file(sd, config=cfg, subfolder="transformer", torch_dtype=torch.bfloat16)
            self.pipe = Flux2KleinPipeline(scheduler=scheduler, vae=vae, text_encoder=text_encoder, tokenizer=tokenizer,
                                           transformer=transformer, is_distilled=self.distilled).to("cuda")
        self.pipe.set_progress_bar_config(disable=False)
        self.load_s = round(time.time() - t0, 2)
        log(f"[klein] loaded {'text encoder (transformer deferred)' if self.sequential else 'transformer ' + tf_path.name + ' + text encoder'} "
            f"{te_path.name} in {self.load_s}s")

    def _embeds(self, prompt, negative):
        """Encode once per prompt; in sequential mode swap the text encoder out and the transformer in afterwards."""
        import torch

        key = (prompt, negative)
        if key in self.cache:
            return self.cache[key]
        with torch.inference_mode():
            pe, _ = self.pipe.encode_prompt(prompt=prompt, device="cuda")
            ne = None
            if not self.distilled:
                ne, _ = self.pipe.encode_prompt(prompt=negative or "", device="cuda")
        self.cache[key] = (pe, ne)
        if self.sequential and self.transformer is None:
            t0 = time.time()
            self.pipe.text_encoder.to("cpu")
            free_cuda()
            sd = load_dequantized(_local_copy(self._tf_path))
            from diffusers import Flux2Transformer2DModel

            self.transformer = Flux2Transformer2DModel.from_single_file(sd, config=self._cfg, subfolder="transformer", torch_dtype=torch.bfloat16)
            del sd
            self.pipe.register_modules(transformer=self.transformer)
            self.pipe.transformer.to("cuda")
            self.pipe.vae.to("cuda")
            log(f"[klein] swapped text encoder out, transformer {self._tf_path.name} in ({time.time() - t0:.1f}s)")
        return self.cache[key]

    def gen(self, prompt, negative, seed, w, h):
        import torch

        pe, ne = self._embeds(prompt, negative)
        with torch.inference_mode():
            return self.pipe(prompt_embeds=pe, negative_prompt_embeds=ne, width=w, height=h, num_inference_steps=int(self.params["steps"]),
                             guidance_scale=float(self.params.get("guidance_scale", 1.0)),
                             generator=torch.Generator(device="cuda").manual_seed(int(seed))).images[0]

    def describe(self):
        return {"family": "klein", "transformer": self.params["transformer_file"], "text_encoder": self.params["text_encoder_file"],
                "config_repo": self.params["config_repo"], "steps": self.params["steps"], "guidance_scale": self.params.get("guidance_scale", 1.0),
                "load_s": self.load_s}


class ZImageBackend:
    family = "zimage"

    def __init__(self, params: dict):
        import torch
        from diffusers import AutoencoderKL, FlowMatchEulerDiscreteScheduler, ZImagePipeline, ZImageTransformer2DModel
        from transformers import AutoTokenizer, Qwen3Model

        t0 = time.time()
        cfg = _snapshot(params["config_repo"], params["config_revision"])
        tf_path = EXTRA_MODELS_DIR / params["transformer_file"]
        te_path = EXTRA_MODELS_DIR / params["text_encoder_file"]
        transformer = ZImageTransformer2DModel.from_single_file(load_dequantized(_local_copy(tf_path)), config=cfg, subfolder="transformer", torch_dtype=torch.bfloat16)
        text_encoder = _load_text_encoder_from_file(Qwen3Model, os.path.join(cfg, "text_encoder"), te_path, strip_model_prefix=True)
        vae = AutoencoderKL.from_pretrained(cfg, subfolder="vae", torch_dtype=torch.bfloat16)
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(cfg, subfolder="scheduler")
        tokenizer = AutoTokenizer.from_pretrained(cfg, subfolder="tokenizer")
        self.pipe = ZImagePipeline(scheduler=scheduler, vae=vae, text_encoder=text_encoder, tokenizer=tokenizer, transformer=transformer).to("cuda")
        self.pipe.set_progress_bar_config(disable=False)
        self.params = params
        self.load_s = round(time.time() - t0, 2)
        log(f"[zimage] loaded transformer {tf_path.name} + text encoder {te_path.name} in {self.load_s}s")

    def gen(self, prompt, negative, seed, w, h):
        import torch

        with torch.inference_mode():
            return self.pipe(prompt=prompt, negative_prompt=negative if float(self.params.get("guidance_scale", 1.0)) > 1 else None,
                             width=w, height=h, num_inference_steps=int(self.params["steps"]),
                             guidance_scale=float(self.params.get("guidance_scale", 1.0)),
                             generator=torch.Generator(device="cuda").manual_seed(int(seed))).images[0]

    def describe(self):
        return {"family": "zimage", "transformer": self.params["transformer_file"], "text_encoder": self.params["text_encoder_file"],
                "config_repo": self.params["config_repo"], "steps": self.params["steps"], "guidance_scale": self.params.get("guidance_scale", 1.0),
                "load_s": self.load_s}


def free_cuda():
    import torch

    gc.collect()
    torch.cuda.empty_cache()


def available(entry: dict) -> tuple[bool, str]:
    """Static availability check (files present) used by the API's /capabilities via `--list`."""
    p = entry.get("params", {})
    fam = entry.get("family")
    hub = Path(os.environ.get("HF_HOME", "/models/hf")) / "hub"
    if fam == "qwen":
        lora = p.get("lightning_lora")
        if lora:
            d = hub / ("models--" + lora["repo"].replace("/", "--")) / "snapshots" / lora["revision"] / lora["file"]
            return (d.exists(), "lightning LoRA not prefetched" if not d.exists() else "")
        return (True, "")
    if fam in ("klein", "zimage"):
        tf = EXTRA_MODELS_DIR / p["transformer_file"]
        snap = hub / ("models--" + p["config_repo"].replace("/", "--")) / "snapshots" / p["config_revision"]
        cfg = snap / "model_index.json"
        if p.get("text_encoder_from_hub"):
            tsnap = hub / ("models--" + p.get("text_encoder_repo", p["config_repo"]).replace("/", "--")) / "snapshots" / p.get("text_encoder_revision", p["config_revision"])
            te = tsnap / "text_encoder" / "model.safetensors.index.json"
            if not te.exists() or len(list((tsnap / "text_encoder").glob("model-*.safetensors"))) < 4:
                return (False, "text encoder not downloaded yet (scripts/prefetch.ps1)")
            te = tf  # only the transformer file needs to exist on disk
        else:
            te = EXTRA_MODELS_DIR / p["text_encoder_file"]
        missing = [str(x) for x in (tf, te) if not x.exists()]
        if missing:
            return (False, "missing on disk: " + ", ".join(Path(m).name for m in missing))
        if not cfg.exists():
            return (False, "config repo not prefetched")
        return (True, "")
    return (False, f"unknown family {fam}")
