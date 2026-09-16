# Multi-view input: several reference images → one asset

Asset Studio normally turns **one** reference image into a master GLB. A **multi-view** job turns **two to
twelve** images of the same object into one, which is the only way to get the back and sides of an object
that a single three-quarter image never shows.

The ComfyUI lane does this with `Pixal3DMultiViewConditioning`, which was added to ComfyUI on 2026-09-08 and
ships in the official `3d_pixal3d_multi_views` template. The local (torch) lane does it with Pixal3D's own
`inference_mv` path.

*中文版：[MULTIVIEW.zh-CN.md](MULTIVIEW.zh-CN.md)*

---

## 1. Sending a multi-view job

`POST /v1/jobs` with a `multiview` object instead of a prompt:

```jsonc
{
  "name": "rusty-car",
  "style": "mobile_factory",
  "quality": "balanced",
  "lod_fractions": [0.7],
  "multiview": {
    "camera_angle_x": 0.6912,           // horizontal FOV in RADIANS (39.60° here)
    "camera_source": "rig",             // "measured" | "rig" | "approximate"
    "mesh_scale": 1.0,
    "images_b64": {                     // file name -> base64 PNG/JPEG (alpha is used as the mask)
      "front.png": "iVBORw0KGgo...",
      "left.png":  "iVBORw0KGgo...",
      "back.png":  "iVBORw0KGgo...",
      "right.png": "iVBORw0KGgo..."
    },
    "frames": [
      {"file_path": "front.png", "name": "front", "transform_matrix": [[...],[...],[...],[0,0,0,1]]},
      {"file_path": "left.png",  "name": "left",  "transform_matrix": [[...],[...],[...],[0,0,0,1]]},
      {"file_path": "back.png",  "name": "back",  "transform_matrix": [[...],[...],[...],[0,0,0,1]]},
      {"file_path": "right.png", "name": "right", "transform_matrix": [[...],[...],[...],[0,0,0,1]]}
    ]
  }
}
```

`reference_candidates`, `variations` and the image lane are skipped: the images *are* the reference
(`presets.generates_reference`). Supply either `multiview` or `reference_image_b64`, not both.

Each `transform_matrix` is a 4×4 **camera-to-world** matrix, Z-up, camera looking down its own −Z. The
translation column is the camera position.

## 2. Which frame becomes which view

The local lane hands the whole folder to Pixal3D, which poses every camera from its matrix. ComfyUI's node
cannot: it takes four **named** views and rebuilds a fixed, level, 90°-apart orbit rig. So the control plane
decides the mapping (`studio/multiview.py`) from the best evidence available, and says so when it has to
guess:

| evidence | used when |
|---|---|
| 1. `name` | the frame is named `front` / `left` / `back` / `right`. The name wins even if the matrix disagrees — but the disagreement is reported as a warning, because one of the two is wrong |
| 2. camera azimuth | unnamed, and the camera's position in the matrix lands within 15° of a free slot |
| 3. input order | unnamed with an unusable or duplicated matrix; the frame takes the next free slot in `front, left, back, right` order, and a warning says which frame was placed by position |

The azimuth is read by inverting the node's own rig, `position = (sin az, −cos az, sin el) · distance`, so
`az = atan2(x, −y)` on the camera position. This is the same convention `process_asset.render_views` uses to
render previews, so renders made by Asset Studio need no conversion.

Each slot takes one frame. A fifth view is dropped with a warning, a duplicated name keeps the first and
warns, and a frame shot more than 10° above the horizon is reported: the rig is level, so its pose is
treated as level.

`front` is also the pose of the finished asset — the node re-bases the rig on the first connected view.

## 3. The horizontal FOV

All four rig cameras share one distance (`_VIEW_PAD · 0.5 / tan(fov/2)`, `_VIEW_PAD = 1.1`), so `fov` sets
both the perspective and the scale the model reconstructs at. It is the one number worth getting right.

| `camera_source` | what happens |
|---|---|
| `rig`, `measured` | the caller's `camera_angle_x` is converted to degrees and **pinned** as a literal on the node, replacing the workflow's measurement link |
| `approximate` | the FOV is **not** trusted, and the workflow measures it from the front view with `MoGeGeometryToFOV` — the node's own documented setting "for photos" |

A per-frame `camera_angle_x` (also radians) overrides the input-level one; the front frame's value wins if
they disagree, and that is reported. A declared FOV that works out outside the node's 1–170° range is
refused with a warning rather than passed on.

## 4. What the two lanes need

| | local (`kind = "local"`) | ComfyUI (`kind = "comfyui"`) |
|---|---|---|
| module | `pixal3d_worker.generate` (`mode = "multiview"`) | `comfy_worker.three_d_generate` |
| input | `views_dir` + `transforms.json`, written by the `reference` stage | `views` = `{slot: path}` + `view_fov_degrees`, computed by the control plane |
| workflow | — | `presets/workflows/3d_pixal3d_multi_views.json` |
| weights | Pixal3D multi-view checkpoints | `pixal3d_multiview_int8_convrot.safetensors` |

### ComfyUI requirements

* **ComfyUI 0.35.0 or newer** on the 3D server — `Pixal3DMultiViewConditioning` does not exist before that,
  and the job fails with `… has no Pixal3DMultiViewConditioning node`. Update the server the same way as any
  other ComfyUI checkout: `git pull`, install requirements, restart.
* The multi-view checkpoint in `models/diffusion_models/`, plus the shared `dino_v3_L_naf_fp32.safetensors`,
  `trellis_2_shape_vae_bf16.safetensors` and `trellis_2_texture_vae_bf16.safetensors`.
* **VRAM: the peak is the texture VAE decode, not the sampler.** A 4-view job on a 19,415-triangle asset took 49
  nodes and then exhausted an 8 GB card at `VaeDecodeTextureTrellis` (3.40 GiB allocated, 2.04 GiB requested).
  The same job at `resolution = 1024` finished in **3.0 minutes** and produced a 56 MB textured GLB (698,803
  triangles, base colour + metallic-roughness + normal, TEXCOORD_0 present), and its front view is the front view
  of the input — the slot mapping poses the model the right way round. A 24 GB card has ample headroom at 1536;
  on a small card lower `resolution` (the `Trellis2UpsampleStage.target_resolution` role) — the same step as the
  `balanced` quality preset's OOM ladder.

The workflow is selected by `three_d_multiview_workflow` (Settings; default `3d_pixal3d_multi_views.json`),
so a multi-view job uses its own graph no matter which single-image workflow is configured.

## 5. The multi-view workflow

`presets/workflows/3d_pixal3d_multi_views.json` is **generated**, not hand-edited, from the single-image
workflow by `var/build_multiview_workflow.py`. Four differences:

1. `UNETLoader` → `pixal3d_multiview_int8_convrot.safetensors`
2. `Pixal3DConditioning` → `Pixal3DMultiViewConditioning` (both expose positive/negative on slots 0/1, so the
   swap changes nothing downstream)
3. the per-view preprocessing chain — `LoadImage` → `RemoveBackground` → mask switch → `MaskPreview` →
   `ImageCropToMask` → `PreviewImage` — is cloned once per extra view. This is not optional: the node wants
   every view framed "with the object spanning about 1/1.1 of the frame, the same scale in every view", and
   `ImageCropToMask` (`pad_factor = 1.1`, centred square, black background) is what produces exactly that
4. the TRELLIS.2/Pixal3D branch switch is dropped: this graph is Pixal3D-only, so `trellis2` cannot silently
   send a multi-view job down the single-image path

The worker does **not** need to be told which node holds which view. It follows each slot's input back
through the graph to the `LoadImage` that feeds it (`three_d_generate.view_loaders`), so node ids stay a
property of the workflow.

Two scripts keep it honest:

```powershell
python var/build_multiview_workflow.py                       # regenerate the workflow
python var/check_workflow.py presets/workflows/3d_pixal3d_multi_views.json
```

`check_workflow.py` runs the cheap half of ComfyUI's `validate_prompt` against a live `/object_info`: every
required input present, every link resolving to a real node and output slot, every type compatible. Run it
against the single-image workflow too — it should report `0 problem(s)` for both.

## 6. Rendering a rig from an existing asset

`var/render_rig_views.py` renders the four views from any GLB with `process_asset.render_views`, whose camera
placement is already the node's rig (`position = (sin az, −cos az, sin el) · distance`), so azimuth
0/90/180/270 maps to front/left/back/right with no conversion:

```powershell
.venv-blender\Scripts\python.exe var\render_rig_views.py <asset.glb> <out_dir> [size]
```

It prints the camera's horizontal FOV, which is the value to put in `camera_angle_x` (a 50 mm camera is
39.60°). This is the fixture used to exercise the lane, and the basis for generating multi-view input from an
asset that already exists.

## 7. Not done yet

* **No portal UI.** A multi-view job has to be submitted through the API/CLI today; the dialog still takes a
  single reference. Choosing several images and picking a mode ("one asset per image" vs "one multi-view
  asset") is the next step.
* **No automatic novel-view generation.** Nothing renders the missing views for you yet; the caller supplies
  real ones (or uses §6).
* **Framing caveat for rig renders.** `ImageCropToMask` normalises *each* view to its own silhouette, so a
  long object (a car seen from the side vs head-on) is magnified differently per view. That is the shipped
  upstream recipe and it is what makes arbitrary photos usable, but a physically consistent rig would share
  one scale across views. Worth revisiting if reconstructions look squashed.
