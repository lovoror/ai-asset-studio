"""Structured asset-description template -> reference-image prompt tuned for single-object reconstruction.

The goal is a picture Pixal3D can lift into 3D: exactly one object, fully visible, centred with margin,
plain contrasting background, three-quarter view with mild perspective, soft neutral light, no DoF/blur/cast
shadows, no text or decorations, clear silhouette and part separation.
"""
from __future__ import annotations

REFERENCE_RULES = (
    "exactly one object, the entire object fully visible and centred with generous empty margin on all sides, "
    "three-quarter front view from slightly above, mild perspective with a moderate focal length, no fisheye distortion, "
    "soft even neutral studio illumination, no dramatic cast shadows, no depth of field, no motion blur, "
    "sharp focus over the whole object, clear silhouette with visible separation between the major components, "
    "no ground clutter, no scenery, no props, no people, "
    "absolutely no text, lettering, numbers, logos, stencilled markings, decals, signage or labels anywhere on the object"
)

BASE_NEGATIVE = (
    "text, letters, lettering, words, numbers, typography, labels, stencil text, decals with text, signage, logo, "
    "watermark, signature, border, frame, vignette, "
    "multiple objects, duplicate objects, cropped, cut off, out of frame, partially visible, "
    "busy background, scenery, landscape, interior, floor grid, table, hands, people, "
    "depth of field, bokeh, motion blur, blurry, lens flare, fisheye, wide-angle distortion, "
    "harsh shadows, dramatic lighting, rim light, glow, reflections on the floor, "
    "low quality, deformed, disfigured, low resolution, jpeg artifacts, oversaturated"
)


def build_reference_prompt(req: dict, style: dict) -> dict:
    """Return {'prompt', 'negative_prompt', 'template'} for the image worker."""
    subject = req["prompt"].strip().rstrip(".")
    materials = (req.get("materials") or style.get("default_materials") or "").strip()
    palette = req.get("palette") or style.get("default_palette") or []
    dims = []
    for k, label in (("height_m", "height"), ("width_m", "width"), ("depth_m", "depth")):
        if req.get(k):
            dims.append(f"{label} about {req[k]:g} m")
    template = {
        "subject": subject,
        "style": style.get("label", req.get("style")),
        "style_clause": " ".join(style["style_clause"].split()),
        "materials": materials or None,
        "palette": palette or None,
        "dimensions": dims or None,
        "background": style.get("background", "plain flat light grey studio background"),
        "view": "three-quarter front view from slightly above",
        "lighting": "soft even neutral studio illumination",
        "constraints": REFERENCE_RULES,
    }
    parts = [f"{subject}."]
    parts.append(template["style_clause"] + ".")
    if materials:
        parts.append(f"Materials: {materials}.")
    if palette:
        parts.append("Colour palette: " + ", ".join(palette) + ".")
    if dims:
        parts.append("Real-world scale: " + ", ".join(dims) + "; proportions consistent with that scale.")
    parts.append(f"Background: {template['background']}, uniform, no horizon line, no floor texture.")
    parts.append("Composition: " + REFERENCE_RULES + ".")
    parts.append("Rendered as a clean 3D asset presentation image, high detail, physically based materials.")
    prompt = " ".join(parts)
    neg = BASE_NEGATIVE
    if style.get("negative_extra"):
        neg += ", " + style["negative_extra"]
    if req.get("negative_extra"):
        neg += ", " + req["negative_extra"]
    return {"prompt": prompt, "negative_prompt": neg, "template": template}
