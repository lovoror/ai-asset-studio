"""Optional multi-view path: coherent posed views + transforms.json -> Pixal3D's inference_mv pipeline (separate *_mv weights).

Reuses upstream `inference_mv.py` (init_pipeline, load_views with the same matting model, check_main_view, run_mv)
and only replaces the hardcoded GLB export. Camera validation happens before any GPU work.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path


def validate_views(views_dir: Path) -> tuple[dict, list[str]]:
    """Check file references, matrices, FOV and framing conventions. Returns (meta, warnings) or raises ValueError."""
    import numpy as np
    from PIL import Image

    meta = json.loads((views_dir / "transforms.json").read_text(encoding="utf-8"))
    frames = meta.get("frames") or []
    if len(frames) < 2:
        raise ValueError("multi-view input needs at least 2 frames")
    warnings = []
    top_fov = meta.get("camera_angle_x")
    sizes = set()
    for i, fr in enumerate(frames):
        p = views_dir / fr["file_path"]
        if not p.exists():
            raise ValueError(f"frame {i}: missing file {fr['file_path']}")
        with Image.open(p) as im:
            sizes.add(im.size)
            if im.mode != "RGBA":
                warnings.append(f"frame {i} ({fr['file_path']}) has no alpha; it will be matted automatically")
        fov = fr.get("camera_angle_x", top_fov)
        if fov is None or not (0.05 < float(fov) < 2.8):
            raise ValueError(f"frame {i}: camera_angle_x missing or out of range: {fov}")
        M = np.array(fr["transform_matrix"], dtype=np.float64)
        if M.shape != (4, 4) or not np.isfinite(M).all():
            raise ValueError(f"frame {i}: transform_matrix must be a finite 4x4")
        if not np.allclose(M[3], [0, 0, 0, 1], atol=1e-6):
            raise ValueError(f"frame {i}: last row of transform_matrix must be [0,0,0,1]")
        R = M[:3, :3]
        if not np.allclose(R @ R.T, np.eye(3), atol=1e-3) or np.linalg.det(R) < 0:
            raise ValueError(f"frame {i}: rotation part is not a proper rotation (orthonormal, det +1)")
        cam = M[:3, 3]
        d = float(np.linalg.norm(cam))
        if d < 1e-3:
            raise ValueError(f"frame {i}: camera is at the origin")
        # camera looks down its -Z axis; the object (origin) should be in front of it
        forward = -R[:, 2]
        if np.dot(forward, -cam / d) < 0.5:
            warnings.append(f"frame {i}: camera does not look towards the origin (cos={np.dot(forward, -cam / d):.2f})")
    if len(sizes) > 1:
        warnings.append(f"views have different sizes {sorted(sizes)}; upstream never crops/rescales, framing must match the cameras")
    # main view must be the canonical front view: camera at (0,-d,0), Z-up, looking +Y
    M0 = np.array(frames[0]["transform_matrix"], dtype=np.float64)
    d0 = float(np.linalg.norm(M0[:3, 3]))
    F = np.array([[1, 0, 0, 0], [0, 0, -1, -d0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=np.float64)
    dev = float(np.abs(M0 - F).max())
    if dev > 1e-3:
        warnings.append(f"frame 0 is not the canonical front view (max deviation {dev:.3e}); the mesh will be posed in that view's frame")
    return meta, warnings


def run_multiview(req: dict, out_dir: Path, timings: dict, vram: dict, warnings: list, vram_snapshot) -> dict:
    import numpy as np
    import torch

    import inference_mv as mv  # upstream multi-view module (functions; __main__ guarded)
    import o_voxel
    from pixal3d_worker.generate import ensure_local_pipeline_dir, phase

    views_dir = Path(req["views_dir"])
    meta, vw = validate_views(views_dir)
    warnings.extend(vw)
    t0 = time.time()
    model_path = ensure_local_pipeline_dir(req.get("rembg_model", "ZhengPeng7/BiRefNet"), multiview=True)
    pipeline = mv.init_pipeline(model_path, "pipeline_mv.json", low_vram=bool(req["low_vram"]))
    timings["load_s"] = round(time.time() - t0, 2)
    vram_snapshot("load")

    phase("preprocess")
    t0 = time.time()
    views = mv.load_views(str(views_dir), req.get("num_views"), rembg=mv.make_rembg(pipeline))
    mv.check_main_view(views)
    timings["preprocess_s"] = round(time.time() - t0, 2)

    phase("generate")
    t0 = time.time()
    seed = int(req["seed"])
    torch.manual_seed(seed)
    resolution = int(req["resolution"])
    sampler = req.get("sampler") or {}
    mesh_list, (shape_slat, tex_slat, res) = pipeline.run_mv(
        views, seed=seed,
        sparse_structure_sampler_params=sampler.get("ss", {}),
        shape_slat_sampler_params=sampler.get("shape", {}),
        tex_slat_sampler_params=sampler.get("tex", {}),
        return_latent=True, pipeline_type=f"{resolution}_cascade",
        max_num_tokens=int(req.get("max_num_tokens", 49152)),
    )
    torch.cuda.synchronize()
    timings["generate_s"] = round(time.time() - t0, 2)
    vram_snapshot("generate")
    mesh = mesh_list[0]
    mesh_stats = {"raw_vertices": int(mesh.vertices.shape[0]), "raw_faces": int(mesh.faces.shape[0]),
                  "grid_resolution": int(res), "requested_resolution": resolution, "num_views": len(views["view_names"])}
    if int(res) != resolution:
        warnings.append(f"Pixal3D reduced the reconstruction resolution from {resolution} to {res} (token limit)")
    del shape_slat, tex_slat
    torch.cuda.empty_cache()

    phase("export")
    t0 = time.time()
    exp = req["export"]
    glb = o_voxel.postprocess.to_glb(
        vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs, coords=mesh.coords,
        attr_layout=pipeline.pbr_attr_layout, grid_size=res, aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        decimation_target=int(exp["decimation_target"]), texture_size=int(exp["texture_size"]),
        remesh=bool(exp.get("remesh", True)), remesh_band=exp.get("remesh_band", 1), remesh_project=exp.get("remesh_project", 0),
        use_tqdm=False, verbose=True,
    )
    rot = np.array([[-1, 0, 0, 0], [0, 0, -1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=np.float64)
    glb.apply_transform(rot)
    glb.export(str(out_dir / "master.glb"), extension_webp=False)
    torch.cuda.synchronize()
    timings["export_s"] = round(time.time() - t0, 2)
    vram_snapshot("export")
    mesh_stats.update({"master_vertices": int(len(glb.vertices)), "master_faces": int(len(glb.faces))})
    cam = {"camera_angle_x": float(views["camera_angle_x"][0, 0]), "distance": float(views["camera_distance"][0, 0]),
           "mesh_scale": views["mesh_scale"], "fov_deg": math.degrees(float(views["camera_angle_x"][0, 0])),
           "views": views["view_names"]}
    return {"master_glb": "master.glb", "preprocessed_png": None, "camera_params": cam, "mesh_stats": mesh_stats,
            "effective": {"mode": "multiview", "resolution": resolution, "grid_resolution": int(res), "low_vram": bool(req["low_vram"]),
                          "max_num_tokens": int(req.get("max_num_tokens", 49152)), "seed": seed, "export": exp,
                          "pipeline_config": "pipeline_mv.json", "rembg_model": req.get("rembg_model")}}
