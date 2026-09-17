# Prompts and references

Every image this pipeline makes is generated from a prompt that the pipeline itself composed. That was invisible
for a long time: the portal collected a one-line idea, the API composed the instruction, and the only way to find
out what the model actually read was to read the code. This document is the reference for what is composed, where
each part comes from, and how to change it — including without a code change.

For where images go once they exist, see [`RESULTS.md`](RESULTS.md); for the multi-view (2D multi-angle) path and
its measurement, [`MULTIVIEW.md`](MULTIVIEW.md).

## 1. The composed reference prompt

`services/studio/studio/promptbuilder.py::build_reference_prompt` assembles one instruction out of the request and
the style definition. In order:

| part | source | example |
|---|---|---|
| subject | the request's `prompt`, trimmed of a trailing full stop | `a small rusty green hatchback.` |
| style clause | the style's `style_clause`, whitespace collapsed | `stylized mobile-game asset, hand-painted ...` |
| materials | request `materials`, else style `default_materials` | `Materials: weathered sheet metal and rubber.` |
| palette | request `palette`, else style `default_palette` | `Colour palette: rust, olive, grey.` |
| real-world scale | `height_m` / `width_m` / `depth_m` when given | `Real-world scale: height about 1.5 m; ...` |
| background | the style's `background` | `Background: plain flat light grey studio background, uniform, ...` |
| composition | `REFERENCE_RULES` | `Composition: exactly one object, the entire object fully visible ...` |
| rendering | fixed sentence | `Rendered as a clean 3D asset presentation image, ...` |

`REFERENCE_RULES` is the part that is not about taste: one object, whole and centred with margin, three-quarter
front view from slightly above, mild perspective, soft even light, no depth of field, sharp focus, clear
separation between components, no scenery or props, and no text anywhere on the object. Pixal3D reconstructs what
it is shown, so a picture that breaks those rules produces a model with a second object next to it, a cropped
base, or lettering baked into the texture.

The negative prompt is `BASE_NEGATIVE` — the mirror image of those rules (text/letters/logos, multiple or
duplicated objects, cropped, busy background, hands and people, depth of field and blur, lens flare, harsh or
dramatic lighting, low quality, deformation, jpeg artifacts, oversaturation). Then, in this order:

1. the style's `negative_extra`, when it has one,
2. the request's `negative_extra`.

## 2. Changing it without touching code

| what you want | how |
|---|---|
| a different style wording, background or extra negatives | `style_edits` in the settings table (portal: Settings → styles). Keys are style ids, or `custom`. The whole map is replaced on write; `{}` clears it. |
| a one-off additional negative | `negative_extra` per request — appended to the style's list, not replacing it |
| send exactly this prompt | `final_prompt` / `final_negative_prompt` per request: they replace the composed text, and `template` in the stage request still records what the composition *would* have been |
| a picture from an existing image | `materials` / `palette` / `height_m` per request |
| the turnaround instruction or graph | settings `views_prompt` / `views_workflow` (§6), or the shipped defaults below |

`GET /capabilities` publishes the pieces a UI needs to offer those knobs without hard-coding them:

- `styles.<id>.style_clause`, `.background`, `.negative_extra`, `.default_materials`, `.default_palette` — the
  current effective values, after `style_edits`;
- `prompt_defaults` — `reference_rules`, `base_negative`, `views_prompt`, `views_workflow`, `views_size`,
  `views_steps`, `views_grounding_px`, `views_fov_degrees`, and `style_fields` (which style keys may be edited);
- `request_schema` / `image_job_schema` — the full field list, generated from the pydantic models.

## 3. Seeing the prompt before generating

```http
POST /v1/prompt/preview
{"prompt": "a rusty green hatchback", "style": "mobile_factory", "materials": "weathered steel"}
```

```json
{"prompt": "a rusty green hatchback. <style clause>. Materials: weathered steel. Background: ...",
 "negative_prompt": "text, letters, ...",
 "template": {"subject": "a rusty green hatchback", "style": "...", "style_clause": "...",
              "materials": "weathered steel", "palette": null, "dimensions": null,
              "background": "...", "view": "three-quarter front view from slightly above",
              "lighting": "soft even neutral studio illumination", "constraints": "exactly one object, ..."},
 "style_label": "..."}
```

No job is created and no GPU is touched: it is the template, rendered. `template` breaks the text into the parts
it was assembled from, which is what lets a UI show the prompt beside a card and offer the piece to edit — the
canvas sends the edited text back as `final_prompt`. `422` with `unknown style ...` for a style id that is not in
the presets.

## 4. Thinking in images: references

A generation can be conditioned on 0, 1 or more images instead of a prompt alone. `ImageJobRequest.references` is
a list, in order (max 6), and each entry is one of:

- `{"image_b64": "<base64 PNG/JPEG>", "label": "front"}` — a picture from the user's desktop, sent inline;
- `{"job_id": "20260917-124330-ade52616", "file": "artifacts/reference_candidates/cand_00.png", "label": "style"}`
  — a file of a job that is already on disk, which is how a canvas card feeds another card without pushing
  megabytes through the browser.

`file` is a plain relative path inside that job directory: no `..`, no leading slash, and the control plane
re-checks it before queueing (`404` if it is not there). The whole list is echoed back on
`GET /v1/jobs/<id>` with `image_b64` **replaced by** `"inline": true|false` — the request is returned on every
poll, and an inline reference is megabytes.

Order carries meaning. In a two-reference edit the first image is the subject and the second is the style or the
detail to borrow, so swapping them changes the result rather than erroring.

### Getting references into the graph

The stage worker uploads each reference and writes it into a `LoadImage` node of the chosen workflow:

- **Which node is which** is read from the node's **title**: `REFERENCE1`, `REFERENCE2`, ... in title order;
  without titles, numeric id order. Title your loaders (see `presets/workflows/README.md`).
- **A slot with no reference is disconnected** — its inputs are removed so the branch stops being reachable from
  the output node and ComfyUI never validates the placeholder filename it shipped with. That is what makes "1
  reference into a 2-reference graph" and "3 views into a 4-view graph" work.
- **References are renamed on upload** to `<slot>_<content digest><ext>`, because the upload replaces by name:
  two references that happen to share a basename would otherwise leave every slot reading the same picture.
- Each reference is copied into *this* job before the stage starts (`<job>/references/ref0.<ext>`,
  `ref1.<ext>`), because only files under the job directory are shipped to a worker on another machine — and
  specifically *outside the stage directory*, which the runner treats as what the stage produces: a path inside it
  becomes an `@out/...` placeholder that is never uploaded, while a path inside the job directory becomes
  `@in/...` and is. (`StagePlan` in `services/studio/studio/runner_client.py`.)

`references` needs an edit workflow and the ComfyUI image lane. The local torch lane generates from a prompt and
nothing else, so a job that asks for references on it is refused at submission with
`generating from reference images needs the ComfyUI image backend` — rather than producing an unrelated picture.
`workflow` names the graph (any `*.json` in `presets/workflows`); the shipped multi-reference graph is
`krea2_edit_refs.json`, and a text-to-image graph has no slot to put a reference in.

## 5. Per-request generation settings

The model presets pin a size and a step count, because a family's values are chosen to fit a card (krea2-turbo is
1024² for exactly that reason). A request may override them; the override is applied *after* the preset.

| field | meaning |
|---|---|
| `width` / `height` | output size, multiples of 16, 64..4096 |
| `steps` | 1..100 |
| `cfg` | the family's guidance value, 0..20 — `true_cfg_scale` for qwen, `guidance_scale` for klein and zimage, `cfg` for ComfyUI models |

`cfg` is spelled per family because the local worker reads only its own family's key; the API maps it so a caller
does not have to know which model it will land on. The ComfyUI lane also takes `self.settings` per request:
`workflow`, and the sampler/scheduler from the model preset.

## 6. The turnaround instruction

For jobs that generate their own multi-view input the pipeline draws a four-panel sheet with a *second* editing
pass, using `VIEWS_PROMPT` and the graph in `presets/workflows/krea2_turnaround.json`:

> Convert the object in the image to a Character Sheet of exactly four views arranged in one horizontal row of
> four equal square panels. Panel one: the front view. Panel two: the full side profile, with the object facing
> the left edge of the panel. Panel three: the rear view. Panel four: the full side profile, with the object
> facing the right edge of the panel. Every panel shows the whole object uncropped and centred, at the same scale
> and the same camera distance, on a plain flat light grey background. No extra views, no close-ups, no text.

The side panels are named by **the direction the object faces in the frame**, which matches the convention
`Pixal3DMultiViewConditioning`'s rig encodes. Two measured facts about this instruction, both detailed in
[`MULTIVIEW.md`](MULTIVIEW.md) §7:

- The model drawing the *same* side in both side panels is common and depends on the reference image. One
  reference drew a proper pair on two of four seeds; another drew the same side twice on every seed and every
  wording tried (seven draws) — including wording that named the two sides explicitly and wording that forbade
  repeating one. No wording has been shown to fix it, so this instruction stands and the pipeline instead
  re-draws a sheet that comes back that way (once) and mirrors a side as a last resort.
- The instruction is re-stated rather than relied on: the workflow file carries a copy of it, and the control
  plane writes `views_prompt` over that copy on every run.

Settings: `views_prompt`, `views_workflow`, `auto_multiview` (whether new jobs generate views by default), and
`views_grounding_px` — the longest side handed to the Qwen3-VL vision encoder, which the node pack documents as
384–768. Leaving it at 0 ("native") pushes the whole reference through a CPU vision encoder: a 2000×2000 canvas
took two hours that way and nine minutes at 768.
