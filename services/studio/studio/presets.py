"""Load style/quality presets and resolve a request into effective per-stage settings."""
import copy
import functools
import random
from pathlib import Path

import yaml

from . import config


@functools.lru_cache(maxsize=1)
def load_presets() -> dict:
    styles = yaml.safe_load((config.PRESETS_DIR / "styles.yaml").read_text(encoding="utf-8"))
    quality = yaml.safe_load((config.PRESETS_DIR / "quality.yaml").read_text(encoding="utf-8"))
    p = config.PRESETS_DIR / "image_models.yaml"
    models = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else []
    return {"styles": styles, "quality": quality, "image_models": {m["id"]: m for m in models}}


def image_model_ids() -> list[str]:
    return list(load_presets()["image_models"].keys())


def style_ids() -> list[str]:
    return list(load_presets()["styles"].keys())


def custom_style_def(req: dict, presets: dict, base: dict | None) -> dict | None:
    """Apply `custom_style` on top of a preset (`base`), or build a style from scratch when the style id is 'custom'.
    Only the fields that are given override the preset; budgets fall back to the preset's (or the generic stylized)
    optimize_defaults. The result has the same shape as a presets/styles.yaml entry."""
    cs = req.get("custom_style")
    if not cs:
        return base
    if base is None and not cs.get("style_clause"):
        return None
    style = copy.deepcopy(base) if base else {"label": "Custom", "description": "User-defined style",
                                              "optimize_defaults": copy.deepcopy(presets["styles"].get("stylized_generic", {}).get("optimize_defaults", {}))}
    for k in ("label", "style_clause", "background", "negative_extra"):
        if cs.get(k) is not None and (cs.get(k) != "" or k == "negative_extra"):
            style[k] = cs[k]
    style.setdefault("background", "plain flat light grey studio background")
    od = style.setdefault("optimize_defaults", {})
    if cs.get("target_triangles"):
        od["target_triangles"] = int(cs["target_triangles"])
    if cs.get("texture_size"):
        od["texture_size"] = int(cs["texture_size"])
    style["custom"] = base is None
    style["edited"] = base is not None
    return style


def resolve_settings(req: dict) -> dict:
    """Map the application-level request onto concrete backend settings (requested == effective at this point)."""
    p = load_presets()
    base = None if req["style"] == "custom" else p["styles"].get(req["style"])
    if base is None and req["style"] != "custom":
        raise ValueError(f"unknown style {req['style']!r}; available: {', '.join(p['styles'])}, custom")
    style = custom_style_def(req, p, base)
    if style is None:
        raise ValueError("style 'custom' requires custom_style.style_clause")
    q = copy.deepcopy(p["quality"][req["quality"]])
    od = style.get("optimize_defaults", {})
    seed = req.get("seed")
    if seed is None:
        seed = random.SystemRandom().randint(0, 2**31 - 1)
    ref = q["reference"]
    model_id = req.get("model") or "qwen-image-2512-lightning-8"
    entry = p["image_models"].get(model_id)
    if entry is None:
        raise ValueError(f"unknown image model {model_id!r}; available: {', '.join(p['image_models'])}")
    ref.update(copy.deepcopy(entry.get("params", {})))
    ref["model"] = model_id
    ref["family"] = entry.get("family", "qwen")
    ref["est_s"] = entry.get("est_s")
    if req.get("reference_candidates"):
        ref["candidates"] = int(req["reference_candidates"])
    if req.get("variations"):
        ref["candidates"] = int(req["variations"])
    master = q["master"]
    if req.get("master_texture_size"):
        master["texture_size"] = int(req["master_texture_size"])
    if req.get("master_triangles"):
        master["decimation_target"] = int(req["master_triangles"])
    settings = {
        "seed": seed,
        "style": req["style"],
        "style_def": style,  # the effective style (preset copy or custom) so the pipeline never depends on later preset edits
        "quality": req["quality"],
        "reference": ref,
        "pixal3d": q["pixal3d"],
        "master": master,
        "optimize": {
            "target_triangles": int(req.get("target_triangles") or od.get("target_triangles", 12000)),
            "texture_size": int(req.get("texture_size") or od.get("texture_size", 2048)),
            "generate_lods": bool(req.get("generate_lods", True)),
            "lod_fractions": list(req.get("lod_fractions") or od.get("lod_fractions", [0.5, 0.25])),
            "generate_collision": bool(req.get("generate_collision", True)),
            "collision_triangles": int(req.get("collision_triangles") or od.get("collision_triangles", 200)),
            "height_m": req.get("height_m"),
            "width_m": req.get("width_m"),
            "depth_m": req.get("depth_m"),
            "render_previews": bool(req.get("render_previews", True)),
            "preview_size": int(req.get("preview_size", 512)),
        },
        "fallback": q.get("fallback", []),
        "allow_quality_fallback": bool(req.get("allow_quality_fallback", True)),
        "input_mode": "multiview" if req.get("multiview") else ("reference_image" if req.get("reference_image_b64") else
                      ("image_job" if req.get("image_job_id") else "text")),
    }
    return settings
