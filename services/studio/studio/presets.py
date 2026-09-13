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
    return {"styles": styles, "quality": quality}


def style_ids() -> list[str]:
    return list(load_presets()["styles"].keys())


def resolve_settings(req: dict) -> dict:
    """Map the application-level request onto concrete backend settings (requested == effective at this point)."""
    p = load_presets()
    style = p["styles"].get(req["style"])
    if style is None:
        raise ValueError(f"unknown style {req['style']!r}; available: {', '.join(p['styles'])}")
    q = copy.deepcopy(p["quality"][req["quality"]])
    od = style.get("optimize_defaults", {})
    seed = req.get("seed")
    if seed is None:
        seed = random.SystemRandom().randint(0, 2**31 - 1)
    ref = q["reference"]
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
