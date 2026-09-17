# Workflow files for the remote ComfyUI backends

Put **API-format** workflow JSON here. `Settings -> Generation backends` takes just the filename
(e.g. `krea2_turbo.json`); the proxy worker resolves it against this folder. Every machine that runs a
ComfyUI stage needs its own copy of this folder (scripts/serve.py exports `STUDIO_PRESETS_DIR`, so a worker
reads `<repo>/presets/workflows` on the machine it runs on).

## Exporting from ComfyUI

API format is not the default export:

1. ComfyUI → Settings → enable **Dev mode Options**
2. Load your workflow, then **Export (API)** — not "Export"

A UI-format export has `nodes` and `links` keys at the top level and is rejected on load with
`"... is a UI-format export"`. API format is a flat `{node_id: {class_type, inputs}}` map.

## What the image backend needs

Nothing configured. `comfy_worker.comfy_client.derive_sampler_graph` finds the sampler and follows its
`positive` / `negative` / `latent_image` links to the nodes it should write, so any ordinary
KSampler-based txt2img workflow works as-is. Requirements:

- exactly one chain of `KSampler` or `KSamplerAdvanced` (a hires-fix chain is fine — the last sampler wins,
  because its latent size is the one that determines the output)
- a prompt path that ends at a node with a `text` input
- a `latent_image` that is a node link, so `width` / `height` / `batch_size` can be set

Written per candidate: prompt, negative prompt, seed, steps, cfg, sampler_name, scheduler, width, height.
`batch_size` is forced to 1 — variations need different seeds, so the worker submits one prompt each.

## The turnaround workflow is not a plain export

`krea2_turnaround.json` is the one file here that is deliberately *not* what ComfyUI exports: it is a Krea 2
identity-edit graph with `Krea2EditModelPatch` **left out**, and the sampler taking the LoRA-patched model
directly. Do not re-add that node, and do not overwrite this file with a fresh export of it.

The node fits its source image to the *output's* grid and, as its own source says, places it at an integer
centred offset - so the reference is painted into the middle of the row by construction, the row comes back
with five panels instead of four, and (measured on a real job) with a view missing altogether: a front view
plus the same side three times and no rear view, which then posed a side profile as the object's back. Without
it the row is exactly four square panels with the rear view drawn. `docs/MULTIVIEW.md` §7 has the numbers.

## What the 3D backend needs

The TRELLIS.2 / Pixal3D graph has no sampler to follow, so its knobs are configured explicitly in
`Settings -> Generation backends -> advanced`. Each field takes a node id, optionally `"<id>:<input key>"`
when the default key guess is wrong:

| role | default input keys tried | trellis2 workflow |
|---|---|---|
| `branch` | `value`, `boolean` | `316` PrimitiveBoolean — `true` = TRELLIS.2, `false` = Pixal3D |
| `seed` | `seed`, `noise_seed`, `value` | without it, a retry is not reproducible |
| `resolution` | `target_resolution`, `resolution`, `value` | `94` Trellis2UpsampleStage (combo; snapped to the nearest allowed option via `/object_info`) |
| `decimate` | `target_face_count`, `face_count`, `target_count`, `value` | `186` DecimateMesh |
| `texture` | `value`, `texture_size`, `resolution`, `size` | `288` PrimitiveInt |
| `normal` | `resolution`, `size`, `value` | `224` BakeNormalMapFromMesh |
| `image` | `image` | `122` LoadImage — only needed if the workflow has several |
| `output` | — | `322` Save3DAdvanced — only needed if several save nodes exist |

Unset roles are left alone, so the workflow's own values stand. `image` and `output` are auto-detected by
`class_type` when unset; `output` is tried class by class most-specific-first, so a workflow with an
intermediate `Save3D` next to the real `Save3DAdvanced` still resolves to one file.

The deliverable **must be a `.glb`** — the Blender stage and the validator both assume it. A non-GLB is
rejected before it reaches them.

## Orientation

Do not add a rotation node. The local exporter applies a Z-up → Y-up matrix because `o_voxel` writes a
Z-up voxel grid; `Save3DAdvanced` already writes glTF-convention Y-up, so the proxy applies nothing.
Adding one here would lay the model on its side.
