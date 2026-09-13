"""CPU-only check of the Blender stage on a synthetic textured GLB (no GPU, no models).

Builds a constant-colour PBR master (sphere + cube, roughness 0.3, metallic 0.9), runs process_asset with a heavy
reduction (forces the rebake path), then verifies: triangle budget met, height applied, origin at bottom-centre,
baked base colour / roughness / metallic within a few levels of the source, collision and LODs present.

Run:  docker compose run --rm --no-deps -v ${PWD}:/src blender python /src/tests/blender_synthetic_check.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image


def build_master(path: Path):
    m = trimesh.creation.icosphere(subdivisions=5, radius=0.5)
    box = trimesh.creation.box(extents=[0.3, 0.3, 0.3])
    box.apply_translation([0.6, 0, 0])
    m = trimesh.util.concatenate([m, box])
    uv = (m.vertices[:, :2] - m.vertices[:, :2].min(0)) / np.ptp(m.vertices[:, :2], axis=0)
    base = np.zeros((512, 512, 4), np.uint8)
    base[..., 0], base[..., 1], base[..., 2], base[..., 3] = 200, 80, 40, 255
    mr = np.zeros((512, 512, 3), np.uint8)
    mr[..., 1], mr[..., 2] = 77, 230
    mat = trimesh.visual.material.PBRMaterial(baseColorTexture=Image.fromarray(base), metallicRoughnessTexture=Image.fromarray(mr),
                                              metallicFactor=1.0, roughnessFactor=1.0)
    m.visual = trimesh.visual.TextureVisuals(uv=uv, material=mat)
    m.export(path)
    return len(m.faces)


def main():
    root = Path(tempfile.mkdtemp(prefix="blender_check_"))
    master = root / "master.glb"
    faces = build_master(master)
    req = {"master_glb": str(master), "out_dir": str(root / "out"), "name": "synthetic",
           "optimize": {"target_triangles": 3000, "texture_size": 512, "generate_lods": True, "lod_fractions": [0.5],
                        "generate_collision": True, "collision_triangles": 100, "height_m": 1.0, "render_previews": True, "preview_size": 128}}
    (root / "req.json").write_text(json.dumps(req))
    r = subprocess.run([sys.executable, "-m", "blender.process_asset", "--request", str(root / "req.json")], capture_output=True, text=True, cwd="/app")
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit("process_asset failed")
    out = json.loads((root / "out" / "output.json").read_text())
    a = out["stats"]["asset"]
    checks = {
        "method_rebake": a["method"] == "decimate_rebake",
        "triangles_within_budget": a["triangles"] <= 3000 * 1.02,
        "height_1m": abs(a["dimensions_m"][2] - 1.0) < 0.01,
        "bottom_origin": abs(a["bounds_min"][2]) < 1e-3,
        "lod_present": len(out["outputs"]["lods"]) == 1 and out["outputs"]["lods"][0]["triangles"] <= 1500 * 1.1,
        "collision_present": out["outputs"]["collision_glb"] is not None and out["stats"]["collision"]["triangles"] <= 120,
        "previews": len(out["outputs"]["previews"]) >= 5,
    }
    tex = root / "out" / "textures"
    for f, idx, want in (("asset_baseColor.png", (0, 1, 2), (200, 80, 40)), ("asset_metallicRoughness.png", (1, 2), (77, 230))):
        arr = np.asarray(Image.open(tex / f)).astype(float)
        cov = arr[..., :3].sum(-1) > 0
        mean = arr[cov].mean(0)
        checks[f"bake_{f}"] = all(abs(mean[i] - w) < 4 for i, w in zip(idx, want))
        print(f, "covered mean", mean.round(1))
    print(json.dumps(checks, indent=1))
    if not all(checks.values()):
        raise SystemExit("blender synthetic check FAILED")
    print("blender synthetic check OK", root)


if __name__ == "__main__":
    main()
