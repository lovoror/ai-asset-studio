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

## Reference slots: how an edit graph says where its images go

An image-to-image job sends 1..n reference images and they land in the workflow's `LoadImage` nodes. Which node
is "reference 1" is read off the node's **title**: `REFERENCE1`, `REFERENCE2`, ... in title order. Without
titles the nodes are taken in numeric id order. Title the loaders in any graph that takes references —
`node 3` and `node 47` say nothing about which of two references is the subject and which is the style, and
re-adding a node with a lower id would silently swap them.

Slots are also disconnected when there is no reference for them. A reference graph's spare slot holds a
placeholder filename (`view_410.png`, `reference2.png`) that was never uploaded, and a wired `LoadImage` with a
filename that does not exist is a hard validation error that fails the stage before it renders anything.
Removing the inputs makes the branch unreachable from the output node, and ComfyUI validates only what it can
reach that way (`execution.py:1128`), so the placeholder is never looked at.

An uploaded reference is stored on the server under a name that carries both the slot number and a digest of
the file's content (`reference1_d89bf523bf03.png`). The upload replaces by name, so without that two references
that happen to share a basename (`reference.png` from two different jobs, say) would leave *every* slot reading
whichever was sent last, and the job would quietly edit the wrong picture. A multi-view 3D workflow names its
uploads the same way, keyed on the view slot (`front_...png`).

## `krea2_edit_refs.json` — the multi-reference edit graph

15 nodes: `LoadImage` 10/30 (titled `REFERENCE1`/`REFERENCE2`) → `ImageResizeKJv2` 11/31 →
`Krea2EditGroundedEncode` 12 (positive, grounded on both images) and 13 (negative, both images) →
`VAEEncode` 14 on the first image → `Krea2EditModelPatch` 15 (`source_image` + `source_image_b`) →
`EmptySD3LatentImage` 16 (1024²) → `KSampler` 17 → `VAEDecode` 18 → `SaveImage` 19.

With one reference the second slot is disconnected exactly as described above: `31.image`, `12.image_b`,
`13.image_b` and `15.source_image_b` are removed, nodes 30/31 become unreachable, and the graph is the
single-image case the node pack's README describes ("leave the b-inputs unconnected for single-image use").
Unlike the turnaround graph this one **keeps** `Krea2EditModelPatch`: it re-renders the object as a presentation
image rather than reposing it, so the fit-to-grid behaviour that broke the turnaround row is not a problem
here — and the node is what anchors the output to the reference. It carries no 4-view LoRA.

Rebuild it with `python var/build_edit_wf.py` and check it with `python var/check_workflow.py` if you change it.

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
