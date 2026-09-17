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
  "lod_fractions": [0.5, 0.25],
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

## 7. Generating the views instead of supplying them

Set `generate_views` on a job (or `auto_multiview` in Settings for new jobs) and the pipeline draws the orbit
views itself: one instruction-editing pass turns the chosen reference into a row of views, which are then cut
into the four images above. That is what makes multi-view reconstruction reachable from a single text prompt.

```
prompt -> reference image -> turnaround sheet (one editing pass, 2560x512)
       -> front/left/back/right (studio/sheet.py) -> Pixal3D multi-view -> master.glb
```

It runs inside the **reference** stage, not as a stage of its own, because the views are derived from the
reference: they have to be discarded exactly when it is, and the retry path already discards the reference
stage's result as a whole. The sheet lands in `stages/reference/turnaround/` and the views in
`stages/reference/views/` - the same shape a caller-supplied multi-view input produces, so everything
downstream is code that already existed.

| setting | default | why |
|---|---|---|
| `views_workflow` | `krea2_turnaround.json` | the editing graph; it needs the ComfyUI image lane |
| `views_prompt` | the shipped turnaround instruction | the dial that decides what the row looks like |
| canvas (`VIEWS_SIZE`) | 2560×512, i.e. **4:1 in five square panels** | see below - the aspect decides the row, the width decides the panel shape |
| `VIEWS_STEPS` | 8 at CFG 1 | the turbo path the node pack documents |
| `VIEWS_GROUNDING_PX` | 768 | the Qwen3-VL grounding resolution the node pack documents (384-768) |
| `VIEWS_FOV_DEGREES` | 20 | what the node's own default describes for generator output |

Four things are worth knowing, all measured:

* **The canvas aspect decides whether a row happens at all.** At 1:1 the model re-renders a single view of the
  reference however the instruction is worded, with or without a 4-view LoRA. At 4:1 it lays out a row.
* **The width has to leave room for the panel the model paints itself.** Because that source panel is always
  there, a 2048-wide canvas splits into *five* panels of 410px - and 410×512 is a portrait slot, which a side
  profile of a wide object (a car is about 2:1) does not fit. The model then clips it, and only the narrow
  front and rear views survive: a reconstruction whose sides are missing or malformed. 2560 wide gives
  512×512 panels, and the measured side views come out whole (aspect 1.59 and 1.69).
* **`grounding_px` is not a detail.** It caps the longest side handed to Qwen3-VL, and `0` means "native": the
  whole reference then goes through a CPU vision encoder. A 2000×2000 canvas took **two hours** that way and
  **2.9 minutes** in the pipeline at 768 with 8 steps.
* **The two sides are named by the direction the object faces in the frame, and the pair is checked afterwards.**
  Asking for "panel two is the left side profile" got the sides drawn the wrong way round on a real sheet:
  front and rear came back right and the two sides came back swapped, which is what a report of "only the front
  and back views are correct" looks like. The prompt therefore states the *screen* direction - "the full side
  profile, with the object facing the left edge of the panel" - because that is what the rig means by `left`:
  §2's azimuth puts the left camera at +x, where image-right is +y while the object faces −y, so the object's
  left side is the profile whose front points to the left of the frame. On top of that the pair is read after
  the fact - unflipped against flipped - and the right view is mirrored when the model drew the same side twice
  (measured: 0.99 correlated unflipped against 0.76 flipped, where a proper mirrored pair reads 0.95 once
  flipped). A duplicate pair tells the rig two contradictory things about one half of the object, which is half
  of what a "the car is not a car" result looks like; a swapped pair is the other half, because it poses the
  object mirrored.

Finding the panels at all is the same problem from the other side, and it is where this went wrong once. The
obvious rule - one sheet-wide background colour, and a panel is a run of columns that differ from it - does not
survive a real sheet, because the panels do not share a background: the four generated views came back on a grey
sweep while the painted-in source panel was white. The commonest border colour was therefore white, so every
grey column of the other four counted as object and the row came back as **two 1022px panels with two cars in
each** - and one of those was handed to the reconstruction as its "front" view. Panels are found from the
object's own **edges** instead: an object has a sharp boundary exactly where the sweep and the shadow on it are
smooth, and that holds whatever colour the backdrop is. Measured on two real sheets, every threshold from 10 to
30 finds the same five panels; below that, the shadow's own soft edge (6-9) joins neighbours together.

A panel is not always the object, either, and that is the trap the first fix fell into. Measured on the next two
sheets out of the same workflow: one drew the views straight onto a single sweep, so a panel *is* the object, and
the next drew each view as a **framed studio photo inset on a plain canvas**, so a panel is the frame and the
object inside it is a third of its width. That decides where the object's backdrop is read from - the columns
*outside* the panel, or the frame's own backdrop *inside* it - and no single reading works for both: against the
canvas outside the frame, the whole grey photo reads as "not backdrop", and the object comes out as a grey
rectangle in the tile. `studio/sheet.py` tells the two apart by the panel's own top edge, which is a step across
the whole panel for a frame against the object's silhouette (measured at a quarter of the panel) for an object;
a panel that looks framed but has no object on it is read the other way, which is what a crate or a wall - an
object with a genuinely flat top - needs. The object's box is then taken by *unbroken runs* rather than by any set
pixel, because a framed photo's torn border leaves dashes down the panel's sides that would otherwise stretch the
box across the whole panel and drag a streak of border into the tile.

The frame the rig wants is **the object on black**, and that is not cosmetic. The node documents its views as
"with alpha or on a black background", and it means it: the single-view path reaches the model through
`ImageCropToMask`, whose background default is `#000000`, and the multi-view node hands its images straight to
DINOv3 - there is no masking anywhere in it - while the same RGB is also the composite that *guides* the NAF
upsampling that shapes the mesh. A tile that is a grey sweep with a car drawn on it therefore says "the object
is a grey slab". So each panel is cut to its own object against **that panel's own** backdrop (read row by row
from the columns beside the object, since the sweep darkens towards the floor), the background the object
*encloses* is filled back in - the glass is a colour match for the sweep behind it, and punching it out would
put a hole through the middle of the car - and the result is pasted onto black in one shared window.

Two things that cut cannot do, both measured, both worth knowing before tuning anything: the soft contact
shadow under the object survives it, because the shadow and the object's own dark underside overlap in contrast
against the sweep (86-115 against 62-136), so no threshold separates them - a stronger shadow than this sheet's
needs a real segmentation, not a colour rule - and the object cannot be separated from that shadow by its
outline either, because where the dark underside meets the dark shadow there is no edge at all (|dy| of 2 across
a boundary whose two sides are 4 units apart).

The remaining two things `studio/sheet.py` has to get right: it drops the panel the model paints from its own
source image by *position* (the source is resampled onto the canvas at a centred offset, so that panel lands
within 2px of the middle while the nearest real view is 360px away - comparing panels by appearance only puts
the duplicate 1.26x above the runner-up, because it is a re-render and not a copy), and it cuts every view with
one shared window sized from the widest panel, so the narrow head-on view is not blown up until it matches the
wide side view.

When the sheet comes back with fewer usable panels than slots, the spare slots are **disconnected** rather than
left wired. The node's view inputs are optional and it re-bases its rig on the first one it is given, so two or
three views reconstruct fine. Leaving a slot wired is not an option: its `LoadImage` would keep the filename the
workflow ships with, and a name with no file behind it fails the stage before anything renders - measured,
`node 410 (LoadImage): image - Invalid image file: view_410.png`. Nothing has to be cleaned up behind the
disconnected slot either, because ComfyUI collects the `OUTPUT_NODE`s and then validates only what it can reach
back from them (`execution.py:1128`), so the orphaned branch is neither validated nor executed.


Needs **Pillow** on the control-plane machine (it is already there for thumbnails), and the ComfyUI image
backend: the local torch lane only generates from a prompt and cannot edit an image into other views. If the
image lane is local, the job warns and reconstructs from the single reference instead.

## 8. Not done yet

* **No portal UI.** A multi-view job has to be submitted through the API/CLI today (`generate_views: true`);
  the dialog still takes a single reference. Choosing several images, or ticking "generate the other angles",
  is the next step.
* **"Right" is still the weakest of the four panels.** The model reads a side request as a three-quarter view
  more often on the right than on the left, so that is the panel most likely to need `views_prompt` tuning.
  Nothing verifies that the panel actually shows the side the prompt asked for; the mirror check next to it can
  only tell that a pair is *not* the same side twice.
* **The contact shadow can survive the cut-out**, leaving a thin dark skirt under the object (§7). Removing it
  needs a real segmentation model, not a colour rule.
* **Framing caveat for rig renders.** `ImageCropToMask` normalises *each* view to its own silhouette, so a
  long object (a car seen from the side vs head-on) is magnified differently per view. That is the shipped
  upstream recipe and it is what makes arbitrary photos usable, but a physically consistent rig would share
  one scale across views. Worth revisiting if reconstructions look squashed.
