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


def workflow_names() -> list[str]:
    """The API-format workflow files available to the remote-ComfyUI backends on this machine.

    A worker resolves a workflow name against *its own* presets dir, so a workflow used by a remote worker must
    exist there too; this list is what the Settings page offers, plus what the connectivity test verifies.
    """
    d = config.PRESETS_DIR / "workflows" if config.PRESETS_DIR else None
    if not d or not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.json"))


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


def backend_config(global_settings: dict | None) -> dict:
    """The generation-backend settings, normalised into the shape the stage request.json carries.

    Snapshotted per job (see resolve_settings) so a finished job still records which server and workflow
    produced it - the same reason style_def is copied rather than referenced. The pipeline falls back to
    calling this with the live settings table for jobs created before the fields existed.
    """
    g = global_settings or {}
    nodes = g.get("nodes") or {}
    return {
        "image": {"kind": g.get("image_kind") or "local", "url": (g.get("image_url") or "").rstrip("/"),
                  "workflow": g.get("image_workflow") or ""},
        "three_d": {"kind": g.get("three_d_kind") or "local", "url": (g.get("three_d_url") or "").rstrip("/"),
                    "workflow": g.get("three_d_workflow") or "",
                    "trellis2": bool(g.get("trellis2", True)),
                    # node ids are strings in the API-format workflow JSON; accept ints from hand-edited settings
                    "nodes": {k: str(v) for k, v in nodes.items() if v not in (None, "")}},
    }


def resolve_settings(req: dict, global_settings: dict | None = None) -> dict:
    """Map the application-level request onto concrete backend settings (requested == effective at this point).

    `global_settings` is the settings table (db.get_settings()); it supplies the generation-backend choice, which
    is an installation-level setting rather than a per-request one.
    """
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
    # The request's model wins. Otherwise honour the installation default from the settings table: the portal
    # always sends one explicitly, but the CLI/API may omit it, and hard-coding the local qwen model here would
    # make such a request demand torch even on a deployment whose images come from a ComfyUI server.
    model_id = (req.get("model") or (global_settings or {}).get("default_image_model")
                or "qwen-image-2512-lightning-8")
    entry = p["image_models"].get(model_id)
    if entry is None:
        raise ValueError(f"unknown image model {model_id!r}; available: {', '.join(p['image_models'])}")
    ref.update(copy.deepcopy(entry.get("params", {})))
    ref["model"] = model_id
    ref["family"] = entry.get("family", "qwen")
    ref["est_s"] = entry.get("est_s")
    backend = backend_config(global_settings)
    if ref["family"] == "comfyui":
        # The registry family outranks the settings switch: a comfyui model has no local implementation, so
        # honouring image_kind="local" here would only produce "unknown family comfyui" from the GPU worker.
        backend["image"]["kind"] = "comfyui"
    # Fail at submission rather than three minutes into the stage: a remote backend with no address cannot work.
    # Only checked when this job will actually run the reference stage - JobRun.run() generates references only for
    # input_mode "text" (a job from a picked candidate or a supplied image copies one instead). The 3D lane is never
    # checked here: it is legitimately configured after the images exist, so stage_pixal3d validates it lazily.
    generates_reference = not (req.get("multiview") or req.get("reference_image_b64") or req.get("image_job_id"))
    if backend["image"]["kind"] == "comfyui" and generates_reference:
        for key, what in (("url", "server address"), ("workflow", "workflow file")):
            if not backend["image"][key]:
                raise ValueError(f"the ComfyUI image backend is selected but no {what} is configured "
                                 f"(Settings -> Generation backends)")
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
        # Which server generates what. Snapshotted here (like style_def) so the job stays reproducible.
        "backend": backend,
    }
    return settings
