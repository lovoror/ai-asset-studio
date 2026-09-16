# asset-studio

> 中文说明见 [`README.md`](README.md)（中英双语）。This is the full English documentation; the bilingual entry point
> is [`README.md`](README.md).

Type a sentence, get a game-ready 3D asset. Everything runs on your own machines — no accounts, no uploads, no
cloud, and since 0.2 no Docker either: a control plane talks to ordinary Python worker services over HTTP, so the
2D and 3D generators can live on different boxes, each with the GPU it needs.

You describe an object ("stylized industrial water pump station, painted teal metal, copper pipes"). asset-studio
paints a reference image, turns it into a high-detail 3D model, then optimizes that model down to the triangle
budget you asked for, bakes the detail into textures, builds LODs and a collision hull, renders previews, and hands
you a folder you can drop straight into Godot, Unity or Blender.

<p align="center">
  <img src="samples/pump/previews/contact_sheet.png" alt="pump station: reference image, master mesh and optimized asset" width="820">
</p>

### From a sentence to a 20k-triangle asset

| Prompt | Reference image the model painted | Optimized asset, rendered |
|---|---|---|
| *"Stylized industrial water pump station, chunky readable silhouette, painted teal metal, copper pipes, small concrete foundation"* | <img src="samples/pump/reference.png" width="240"> | <img src="samples/pump/previews/asset_front_left.png" width="240"><br>19,803 tris, 2048² textures |
| *"Stylized wooden cargo crate with riveted metal corner brackets and a plain diagonal hazard stripe"* | <img src="samples/crate/reference.png" width="240"> | <img src="samples/crate/previews/asset_front_left.png" width="240"><br>7,998 tris, 1024² textures |
| *"Ceramic coffee mug with a wide open handle, glossy teal glaze with a cream interior"* | <img src="samples/mug/reference.png" width="240"> | <img src="samples/mug/previews/asset_front_left.png" width="240"><br>6,000 tris, 1024² textures |

Contact sheets for the other two (reference top-left, high-detail master bottom-left, optimized asset elsewhere):

<p align="center">
  <img src="samples/crate/previews/contact_sheet.png" alt="crate contact sheet" width="400">
  <img src="samples/mug/previews/contact_sheet.png" alt="mug contact sheet" width="400">
</p>

Every file above is in [`samples/`](samples/), together with the GLBs and the `manifest.json` each job produced.
Those samples were generated on the earlier container layout; the pipeline is unchanged, see
[Benchmarks](#benchmarks).

## What you get

One job produces a folder like this:

| File | What it is |
|---|---|
| `master.glb` | The untouched high-detail model straight out of the 3D generator: up to 1 M triangles, 4096² PBR textures. Keep it for re-optimising later. |
| `<name>.glb` | **The asset you ship.** Reduced to your triangle budget, with base colour, metallic-roughness and normal maps baked from the master. |
| `<name>_LOD1.glb` | A lower-detail version (70 % of the budget by default) that **reuses LOD0's UVs and textures** — one texture set for every level. |
| `<name>_collision.glb` | A convex hull of a couple of hundred triangles for the physics engine. |
| `previews/*.png` | Neutral-lit renders: four views of the asset, one of the master, and a contact sheet. |
| `textures/*.png` | The baked maps as loose PNGs, in case you want them outside the GLB. |
| `reference.png` | The reference image the asset was built from. |
| `manifest.json` | What you asked for vs. what actually happened: settings, triangle counts, reduction error, warnings, timings, VRAM/RAM, sha256 of every file. |

Units are metres, glTF Y-up, origin at the bottom centre — so a 2 m pump imports as a 2 m pump.

## Requirements

| | |
|---|---|
| OS | Windows 11, or macOS/Linux (the launcher is Python; the 3D GPU stack is CUDA-only in practice) |
| Python | 3.11 or newer for the control plane and the 2D/3D workers. The Blender role needs its **own** interpreter: bpy publishes one tag per release and never a cp312 (4.5/5.0 are cp311, 5.1+ are cp313). `uv python install 3.11` provisions one without touching the system; see [`requirements/blender.txt`](requirements/blender.txt) |
| 2D server | One NVIDIA GPU; ~60 GB of model weights |
| 3D server | One NVIDIA GPU (built and tuned for an RTX 5090, 32 GB); ~50 GB of weights, and a CUDA toolkit to build TRELLIS.2's extensions |
| Control machine | No GPU needed. Runs the API, the orchestrator and (by default) Blender, which is CPU-only |
| Disk | Budget ~120 GB on the 2D server and ~60 GB on the 3D server, plus room for job outputs |

There are no containers: each role is a plain Python environment installed from `requirements/<role>.txt`, and the
only Python you need on the host is the one running each service. The heavy CUDA environment for the 3D role is
built by following TRELLIS.2's own installation instructions (see [`requirements/pixal3d.txt`](requirements/pixal3d.txt)).

## Quick start

Three shapes, depending on how many machines you have. The full version is in
[`docs/DISTRIBUTED.md`](docs/DISTRIBUTED.md).

### Everything on one machine

```powershell
scripts\bootstrap.ps1 all        # venv + deps + weights + readiness report   (~105 GB download, once)
python scripts\serve.py all      # API, orchestrator and all three workers; Ctrl-C to stop
```

Open **http://127.0.0.1:8090**.

### The usual setup: a control machine, a 2D server and a 3D server

On the control machine:

```powershell
scripts\bootstrap.ps1 control                          # control-plane dependencies only
python scripts\serve.py doctor                         # what this machine can run, and what answers
```

On each worker machine — one checkout, one `config.toml`, one command:

```powershell
scripts\bootstrap.ps1 image        # 2D server: torch + diffusers + the Qwen/Z-Image/FLUX weights
python scripts\serve.py image

scripts\bootstrap.ps1 pixal3d      # 3D server: the TRELLIS.2 environment + Pixal3D weights
python scripts\serve.py pixal3d
```

Then point the control machine at them and start it:

```toml
# config.toml on the control machine
[servers]
image = "http://192.168.1.21:8701"
pixal3d = "http://192.168.1.22:8702"
blender = "local"
```

```powershell
scripts\start.ps1                  # run the control plane in the background (scripts\stop.ps1 to stop)
# or: python scripts\serve.py control      (foreground, Ctrl-C to stop)
python cli\assetctl.py doctor      # API, both servers, GPU, cached models   (--deep also loads the models)
```

On macOS/Linux use `./scripts/serve.sh <role>` / `./scripts/bootstrap.sh <role>` instead; everything else is the
same, and the launcher is a Python script either way.

### Already have ComfyUI servers? Use those instead of local workers

Both GPU stages can run on ComfyUI servers rather than a local torch environment. The stage workers then become
light HTTP proxies — no torch, no CUDA, and **nothing to install on the ComfyUI machines**:

```powershell
python scripts\bootstrap.py --role comfy     # httpx + pillow + numpy + scipy: no torch
python scripts\bootstrap.py --role blender   # CPU post-processing (needs its own Python, see Requirements)
python scripts\serve.py control              # all three stage processes run on this machine
```

Put the **API-format** workflow JSON in [`presets/workflows/`](presets/workflows/) and point each stage at its
server. This is a runtime setting (portal: **Settings → Generation backends**, or `PUT /v1/settings`), so it needs
no restart and is snapshotted into every job:

| stage | settings | example |
|---|---|---|
| images | `image_kind`, `image_url`, `image_workflow` | `comfyui`, `http://127.0.0.1:8188`, `KREA-2-TURBO.json` |
| 3D | `three_d_kind`, `three_d_url`, `three_d_workflow`, `nodes` | `comfyui`, `http://10.0.0.9:8388`, `3d_pixal3d_trellis2_image_to_model.json`, `{...}` |

The image side needs **no node ids**: the sampler, both prompts and the latent node are derived from the graph. The
3D side does, because nothing in a TRELLIS.2 graph identifies which `PrimitiveInt` feeds the texture bake. The full
recipe — which node each role maps to, plus the limits of that mapping — is in
[`docs/DISTRIBUTED.md`](docs/DISTRIBUTED.md).

`python scripts\serve.py doctor` prints, per stage, which interpreter runs it and whether the ComfyUI path is
ready; it does not demand torch when a stage is served by ComfyUI.

## Configuration

Everything lives in **`config.toml`** at the repository root — copy [`config.example.toml`](config.example.toml)
and edit it. It is git-ignored, so each machine keeps its own copy: the control machine lists where the 2D and 3D
servers are, and each worker lists its own directories and model cache.

Every setting can also be given as an environment variable (`STUDIO_*`, named in the example file), which wins over
the file. The pieces that matter most:

| Setting | Meaning |
|---|---|
| `[servers] image` / `pixal3d` / `blender` | Where each stage runs. `"local"` means this machine (the `control` role starts it); anything else is a URL such as `http://192.168.1.22:8702` |
| `[worker] bind` | `127.0.0.1` by default. A worker machine sets `0.0.0.0` to accept the control machine's connections |
| `[worker] token` | Shared secret. A worker bound to `0.0.0.0` **refuses to start without it**, because its API runs stage processes |
| `[control] api_token` | Bearer token for the portal/CLI. Required as soon as `[control] bind` is not loopback |
| `[paths] jobs` / `data` | Job outputs and the SQLite database. Only the control machine reads these; a worker's own scratch lives under `[paths] data` and is cleaned up by itself |
| `[paths] models` | Model cache root (`<models>/hf`, `<models>/torch`). Set it on every machine that runs GPU stages |
| `[python] <role>` | Optional per-role interpreter, e.g. `blender = ".venv-blender/Scripts/python.exe"`. Empty = the interpreter running `serve.py`. The Blender role usually needs its own (see Requirements) |
| `[worker] roles` | Optional restriction on which stages a worker may accept (belt-and-braces on top of `serve.py <role>`) |

`python scripts/serve.py doctor` reports what this machine can run and whether the configured servers answer.

## How the machines talk

The control plane never shares a filesystem with a worker. For each stage it:

1. creates a run on the worker (`POST /runs`) and uploads every file the stage needs — the reference image, the
   master GLB, a multi-view folder — rewriting the paths in the stage request to worker-side placeholders;
2. starts the stage (`POST /runs/<id>/start`), which runs one `python -m <stage module>` subprocess;
3. mirrors the worker's stdout back into the local stage log while it polls, so out-of-memory handling and the
   portal's live log keep working exactly as before;
4. downloads the stage's output bundle and unpacks it into the local job directory.

A worker only accepts the modules belonging to the roles it was started with, so the 2D box cannot be asked to run
Blender. Each stage is still a fresh subprocess, so CUDA memory is released between stages, and the orchestrator's
two lanes (image sessions, 3D assets) still run independently.

## Web portal

The portal is the friendly way to use asset-studio; it is served by the API process, at
http://127.0.0.1:8090 once the control plane is up.

1. **Describe an object.** Type a prompt or click one of the built-in examples (or "Surprise me"), pick a style,
   and choose how many variations you want (4 by default).
2. **Pick the images you like.** You get that many reference-image variations of your idea. Nothing has been built
   in 3D yet, so this stage is cheap to iterate on — reroll, tweak the wording, try another style.
3. **Send them to the queue.** Selected variations become 3D jobs. Image sessions and 3D jobs run in separate lanes,
   so you can keep generating images while a 3D job is building; 3D jobs run one at a time on the GPU,
   automatically, or you can park jobs on hold and release them when you want the card free.
4. **Browse the library.** Finished assets show their previews, spin in an in-browser 3D viewer, and download as
   single files or the whole folder. Re-optimise re-runs just the mesh step at a new triangle or texture budget —
   no image or 3D regeneration, so it takes about a minute.

Dark and light themes, and the whole thing is local — the API binds to `127.0.0.1` by default.

The portal is **bilingual (English / 简体中文)**: switch it on **Settings → Appearance → Language**. The choice is
remembered per browser, and a first visit follows the browser's language. See
[`docs/PORTAL.md`](docs/PORTAL.md#languages) for how the strings are organised and what is still English
(pipeline warnings and preset text come from the server).

| Create | Session (pick variations) |
|---|---|
| <img src="docs/screenshots/create-dark.png" width="440"> | <img src="docs/screenshots/session-dark.png" width="440"> |

| Queue | Library | Asset (3D viewer, stats, downloads) |
|---|---|---|
| <img src="docs/screenshots/queue-dark.png" width="290"> | <img src="docs/screenshots/library-dark.png" width="290"> | <img src="docs/screenshots/asset-dark.png" width="290"> |

Light-theme captures of the same pages are in [`docs/screenshots/`](docs/screenshots/).

### Image models

The reference-image step is the cheap, iterative part, so you can pick the model per session on the Create page
(and set a default in Settings). The models run on the **image worker** with diffusers; the FLUX and Z-Image weights
are read from an existing ComfyUI models folder (`[paths] extra_models` in `config.toml`) and never through ComfyUI
itself. Times are per image on an RTX 5090 after the model is loaded once per job
(measurements in [`docs/RESULTS.md`](docs/RESULTS.md)).

| Model | Settings | Per image | Notes |
|---|---|---|---|
| **Qwen-Image-2512 Lightning 8** (default) | 1328², 8 steps, no CFG | ~8 s | Won the blind test: best prompt adherence and clean surfaces. fp8 weights fully on the GPU, official Lightning LoRA fused in. Apache-2.0. |
| Qwen-Image-2512 Lightning 4 | 1328², 4 steps, no CFG | ~4 s | Same compositions, thinner detail. Rough iteration. |
| Z-Image Turbo | 1536², 9 steps, guidance 0 | ~9 s | Chunkiest, cleanest silhouettes; second in the blind test. Apache-2.0. |
| FLUX.2 Klein 4B | 1536², 4 steps, guidance 1.0 | ~2.5 s | Fastest. Apache-2.0. |
| FLUX.2 Klein 9B | 1536², 4 steps, guidance 1.0 | ~5 s | FLUX non-commercial licence; gated text encoder downloaded once. |
| Qwen-Image-2512 (50 steps) | 1328², 50 steps, CFG 4 | ~80 s | Undistilled sampling, kept as an option. Rated below Lightning by eye (waxier surfaces). |

The Qwen transformer is built once into an fp8 cache (53 s, 20 GB under `<models>/hf/extra-cache`) from the bf16
weights, so the 20B model runs resident on a 32 GB card instead of streaming half of it over PCIe every step (that
path took ~5 min per image). The distilled FLUX models are step- and guidance-distilled, so 4 steps / guidance 1.0
are fixed by the vendor; resolution is their quality lever, and 1536² was chosen after a sweep. Blind-test details
and per-model timings are in [`docs/RESULTS.md`](docs/RESULTS.md); the benchmark and blind-test scripts are in
[`tools/bench/`](tools/bench/).

## Command line

```powershell
python cli\assetctl.py generate "Stylized industrial water pump station with copper pipes" `
   --style mobile_factory --quality quality --seed 12345 --height-m 2.0 --triangles 12000 --texture 2048 `
   --wait --download out\pump
```

Open a finished job in your local Blender as a labelled comparison scene (front row textured: master, optimized,
LODs, collision; back row the same meshes untextured):

```powershell
python cli\assetctl.py view <job_id>            # downloads to out\<job_id> first if needed
python cli\assetctl.py generate "..." --open    # generate, wait, download and open in one go
```

Blender is found via `STUDIO_BLENDER`, `PATH`, or the newest install under `Program Files\Blender Foundation`.

Re-optimise a finished job at a different budget without regenerating anything:

```powershell
python cli\assetctl.py retry <job_id> --from-stage blender --triangles 20000 --texture 2048 --wait
```

Other commands: `status`, `wait`, `download`, `cancel`, `logs`, `list`, `capabilities`, `health`, `doctor`.
The CLI points at `STUDIO_URL` (default `http://127.0.0.1:8090`), so it can drive a control plane on another
machine.

## HTTP API

One-shot: prompt in, asset out (`examples/curl_example.sh` does the whole loop).

```bash
curl -X POST http://127.0.0.1:8090/v1/jobs -H 'Content-Type: application/json' -d @examples/pump_station.json
# -> {"job_id": "...", "status": "queued", "status_url": "/v1/jobs/<id>", "artifacts_url": "/v1/jobs/<id>/artifacts"}
curl http://127.0.0.1:8090/v1/jobs/<id>                       # status, stage, progress, warnings, settings, timings, events
curl http://127.0.0.1:8090/v1/jobs/<id>/artifacts             # list what was produced
curl -o asset.glb http://127.0.0.1:8090/v1/jobs/<id>/artifacts/<name>.glb
curl -X POST http://127.0.0.1:8090/v1/jobs/<id>/cancel
curl -X POST http://127.0.0.1:8090/v1/jobs/<id>/retry \
     -H 'Content-Type: application/json' \
     -d '{"from_stage":"blender","optimize":{"target_triangles":20000}}'   # re-optimise, no regeneration
curl http://127.0.0.1:8090/health ; curl http://127.0.0.1:8090/capabilities
curl -X POST 'http://127.0.0.1:8090/v1/selftest?deep=false'    # do the workers answer? deep=true loads the models
```

Two-step (what the portal uses): make images first, choose, then build 3D.

```bash
curl -X POST http://127.0.0.1:8090/v1/image-jobs -H 'Content-Type: application/json' \
     -d '{"prompt":"stylized copper water tank","style":"mobile_factory","variations":4}'
# poll /v1/jobs/<image_job_id> until completed, look at the candidates, then:
curl -X POST http://127.0.0.1:8090/v1/asset-jobs -H 'Content-Type: application/json' \
     -d '{"image_job_id":"<id>","candidate":"cand_02.png","hold":false}'   # hold=true parks it in the review queue
```

Ready-made request bodies: [`examples/pump_station.json`](examples/pump_station.json),
[`examples/cargo_crate.json`](examples/cargo_crate.json), [`examples/mug.json`](examples/mug.json).
`GET /capabilities` returns the full JSON schema of a request. To reach the API from another machine set
`[control] bind` **and** `[control] api_token` — the server refuses to start on a non-loopback address without a
token.

## MCP server (use it from an agent)

```powershell
pip install "mcp>=1.2"
python mcp\server.py
# Claude Code:  claude mcp add asset-studio -- python <path-to-repo>/mcp/server.py
```

Tools: `capabilities`, `generate`, `status`, `wait`, `artifacts`, `download`, `manifest`, `cancel`, `retry`,
`open_in_blender`.

## Styles and quality

Styles ([`presets/styles.yaml`](presets/styles.yaml)) set the look and sensible budget defaults:

| Style | Look | Default triangles / texture |
|---|---|---|
| `mobile_factory` | Chunky readable forms for a mobile factory-builder: strong silhouettes, painted metal, limited palette, no tiny greebles | 20,000 / 2048 |
| `stylized_generic` | General stylized game asset, hand-painted PBR, moderate detail | 30,000 / 2048 |
| `realistic` | Realistic prop, plausible materials and proportions | 40,000 / 2048 |
| `lowpoly` | Flat-shaded facets, solid colours, simple forms | 6,000 / 1024 |
| `clean_product` | Clean product-visualisation look, precise surfaces, white background | 30,000 / 2048 |
| `scifi` | Hard-surface sci-fi: clean panel lines, machined metal, a few emissive accents | 30,000 / 2048 |
| `fantasy` | Hand-crafted fantasy props: worn wood, forged iron, stone, leather, warm painted look | 30,000 / 2048 |

Every style is editable in the portal: pick a card, press **Edit this style**, and change the look sentence, background,
keep-out list and budgets. Edits are saved per style in the app's settings (SQLite) and sent with each job as
`custom_style`; **Reset to preset** brings the original back. The dashed **Custom** card is a style written from scratch.
The API takes the same thing: `"style": "custom"` with a `custom_style.style_clause`, or a preset id plus a partial
`custom_style` to override only some fields.

Quality ([`presets/quality.yaml`](presets/quality.yaml)) decides how much GPU time to spend:

| Quality | Reference images | 3D resolution | Typical wall clock |
|---|---|---|---|
| `balanced` | 1 candidate | 1024 cascade | ~11 min |
| `quality` | 2 candidates | 1536 cascade | ~16 min (about 20 min end to end with two candidates) |

Both export a 1 M-triangle / 4096² master; the image model is chosen separately (table above). You can always
override `target_triangles`, `texture_size`, `height_m`, LOD fractions and collision budget per request.

## How it works

```
prompt ─► reference image (Qwen-Image-2512) ─► 3D model (Pixal3D / TRELLIS.2) ─► master GLB
      ─► Blender: scale, origin, cleanup, meshoptimizer reduction, bake from the untouched master,
         LODs, collision hull, previews ─► validation ─► manifest.json
```

* **Reference image.** Your prompt is rewritten into a single-object, plain-background, three-quarter, soft-lit,
  no-text prompt (plus a strong negative prompt) and rendered by Qwen-Image-2512. In `quality` mode two candidates
  are made and scored by transparent technical checks — background plainness, framing margins, clipping, centring.
* **3D model.** Pixal3D (TRELLIS.2 backbone) mattes and crops the image, estimates the camera, and generates the
  mesh. The export is ours: your own decimation target and texture size, PNG textures instead of WebP.
* **Blender.** Imports the master and never modifies it. Scales to `height_m`, puts the origin at the bottom
  centre, removes loose debris, then reduces to your budget with **meshoptimizer** — the same simplifier behind
  gltfpack and the Unity/Unreal plug-ins — within an error bound that is relaxed step by step only if the budget
  cannot be met. The result gets fresh UVs, and colour/metallic/roughness/normal are baked from the untouched
  master with Cycles, so detail you lose in geometry comes back in the maps. Then LODs, convex collision hull, and
  Cycles preview renders.
* **Validate and package.** Every GLB is inspected (primitives, triangle and vertex counts, UVs, normals, texture
  slots and mime types, bounds, height, origin, budgets) and everything is written into `manifest.json` with a
  sha256 per file.

Practical behaviour worth knowing:

* **Fully offline after prefetch.** Weights are downloaded once per machine by `scripts/prefetch.py`; the stages run
  with `HF_HUB_OFFLINE=1`, so generation never touches the network.
* **One job at a time per lane.** Each stage is a fresh subprocess, so CUDA memory is always released between
  stages (that is why a job pays ~52 s of model loading once).
* **It shares the GPU politely.** If another application (ComfyUI, a Blender viewport, a game) is using the card,
  the worker waits instead of killing anything, and on CUDA out-of-memory it waits for memory to free before it
  touches your quality settings.
* **Jobs are durable.** They live in SQLite on the control machine; if the orchestrator restarts, the job requeues
  and completed stages are reused instead of re-run. A stage still running on a worker is re-attached to.
* **Worker scratch is disposable.** A worker stages each run under `[paths] data`/worker/<role> and collects its own
  leftovers on start; the authoritative copy of every artifact is on the control machine.

## Benchmarks

Measured on this machine (RTX 5090 32 GB, driver 616.92) on the earlier container layout, from the sample
manifests — full detail in **[BENCHMARKS.md](BENCHMARKS.md)** and the narrative in
[`docs/RESULTS.md`](docs/RESULTS.md). The stage code is unchanged, so the numbers still describe the pipeline.

| Asset | Preset | Reference | 3D | Blender | Total | Master tris → optimized | Error | LOD1 / LOD2 | Collision |
|---|---|---|---|---|---|---|---|---|---|
| crate | balanced (1024) | 339 s | 248 s | 78 s | 11.1 min | 954,901 → 7,998 (voxel proxy) | 1.4 % | 3,997 / 1,996 | 200 |
| pump | quality (1536) | 631 s | 226 s | 80 s | 15.6 min | 997,520 → 19,803 | 2.3 % | 9,813 / 5,227 | 200 |
| mug | balanced (1024) | 347 s | 250 s | 74 s | 11.2 min | 969,785 → 6,000 | 1.4 % | 3,088 / 1,500 | 254 |

Most of the time is the reference image (5.7–6.0 s per step at full quality). Re-optimising an existing job at a new
budget is a Blender-only run: about 80 seconds. All 15 produced GLBs pass the Khronos glTF validator with zero
errors and import into headless Godot 4.7.2.

## Tips

* **No text or lettering.** Ask for a sign and you get garbled glyphs — the models cannot spell. The prompt builder
  already forbids text; describe shapes and materials instead ("plain diagonal hazard stripe", not "a sign saying DANGER").
* **One object per prompt.** The pipeline builds a single object on a plain background. A scene, a pair of things, or
  an object on a base plate will confuse the matting step.
* **Budgets:** 3,000–8,000 triangles is a comfortable mobile prop; 20,000 keeps visible mechanical detail; going much
  higher mostly buys silhouette precision you will not see.
* **`height_m` is real-world size** in metres, applied as a uniform scale with the origin at the bottom centre. Set it
  and your asset imports at the right size in the engine.
* **Re-optimise instead of regenerating.** Once you have a master you like, retry from the `blender` stage with a new
  triangle or texture budget. It takes a minute and costs no image or 3D generation.
* **Seeds are reproducible.** Same prompt, style, quality and seed gives you the same reference image again.
* **Try `quality` when the silhouette matters.** The 1536 cascade keeps thin pipes and openings that 1024 rounds off.
* **Put the model cache on a fast, large disk.** `[paths] models` on every GPU machine keeps the weights out of the
  user profile and makes prefetching predictable.

## Limitations

* The output is an **optimized static prop**: automatic decimation topology, no rig, no animation-friendly edge flow.
* Matting defaults to Pixal3D's upstream `briaai/RMBG-2.0`, which is gated: accept its license on Hugging Face and run the
  prefetch with your token. Without access, set `[stage] rembg_model = "ZhengPeng7/BiRefNet"` (open drop-in replacement).
* Multi-view input (2–12 images → one asset) runs on both lanes: `Pixal3DMultiViewConditioning` on ComfyUI, which
  needs **ComfyUI 0.35+** on the 3D server, or Pixal3D's own `inference_mv` locally. The multi-view checkpoints are
  in the same prefetch set as the single-image ones (`scripts/prefetch.py --role pixal3d --mv`); they are not a
  separate download. What is still missing is the portal UI for picking the images, and automatic novel-view
  generation — nothing renders the views you did not supply. See [docs/MULTIVIEW.md](docs/MULTIVIEW.md).
* **Godot 4.7.2 import is tested**; Unity is not installed on this machine, so it is untested here (the GLBs are
  standard, validator-clean glTF).
* **LOD budgets are capped by the UV layout, not by the fraction you ask for.** LODs share LOD0's texture set, so they
  have to keep its UV layout, and meshoptimizer never collapses an edge lying on a UV border — after an atlas bake every
  seam is one. Measured on a 19,415-triangle atlas-baked asset (`var/lod_fractions_sweep.py`): nothing below ~67 % was
  reachable at all, and a chained second level stalled at ~65 %, so `[0.5, 0.25]` was not merely missed, it was
  unreachable, and both levels were reported 12–113 % over budget. The default is therefore one level at `[0.7]`, which
  is reached exactly; `lod_fractions` is still settable per request or per style. Asking for more levels is allowed — it
  just comes back as a warning in the manifest, not a failure (it used to fail the job:
  [issue #1](https://github.com/zorrobyte/asset-studio/issues/1)).
* **The 3D environment is not pip-installable.** TRELLIS.2's CUDA extensions must be compiled for the GPU in the 3D
  server; `scripts/bootstrap.py --role pixal3d` checks what is present and tells you what is missing.

## License

The code in this repository is released under the [BSD Zero Clause License](LICENSE) (0BSD): use it for anything,
commercial or not, with no attribution required. The models it downloads keep their own licences, which apply to what
you generate with them: Qwen-Image-2512, Z-Image Turbo and FLUX.2 Klein 4B are Apache-2.0; FLUX.2 Klein 9B is under the
FLUX non-commercial licence; Pixal3D / TRELLIS.2 and RMBG-2.0 have their own terms on Hugging Face. Check those before
shipping assets from a given model.

## Project layout

| Path | What |
|---|---|
| `config.example.toml` | Every setting, documented. Copy to `config.toml` (git-ignored, per machine) |
| `scripts/serve.py` | Cross-platform launcher and `doctor`: runs a role on this machine |
| `scripts/bootstrap.py` | Installs one role's dependencies from `requirements/<role>.txt` |
| `scripts/prefetch.py` | Downloads the pinned weights a role needs, on that machine |
| `scripts/start.ps1` / `stop.ps1` / `logs.ps1` | Run the control plane in the background on Windows |
| `services/studio/studio/` | FastAPI app (`api.py`), orchestrator (`worker.py`), pipeline, job store, presets, validator |
| `services/runner/runner.py` | The worker: a stdlib HTTP server that runs one stage subprocess at a time |
| `services/studio/studio/runner_client.py` | Control-plane side of the transfer protocol (upload in, bundle out) |
| `services/image_worker/` | Qwen-Image-2512 reference candidates + technical selection |
| `services/pixal3d_worker/` | Pixal3D adapter (single-image + multi-view), prefetch/verify |
| `services/blender/process_asset.py` | bpy 4.5 LTS post-processing |
| `services/worker_paths.py` | Where a worker finds model caches on this machine |
| `presets/` | `styles.yaml`, `quality.yaml` (incl. OOM fallback ladders), `workflows/` |
| `requirements/` | One file per role |
| `web/` | React web portal (create → queue → library), served by the API |
| `cli/`, `mcp/` | Dependency-free CLI, MCP wrapper with the same actions |
| `docs/`, `BENCHMARKS.md` | Measurements, notes, limitations, [stage workers](docs/STAGES.md), [multi-view](docs/MULTIVIEW.md), [distributed setup](docs/DISTRIBUTED.md) |
| `manifests/dependency-manifest.json` | Pinned commits, model revisions, frozen packages per role |

Tests (no GPU, no models needed for the first two):

```powershell
python scripts/bootstrap.py --role control
python -m pytest -q tests                       # API, portal, transfer protocol, control-plane smoke test
python tests\acceptance_real.py --cancel-test   # real GPU runs against a running control plane
python tests\blender_synthetic_check.py         # Blender stage on a synthetic GLB (needs --role blender)
```

## Credits

asset-studio is orchestration around other people's models and tools:

* [Pixal3D](https://huggingface.co/TencentARC/Pixal3D) and [TRELLIS.2](https://github.com/microsoft/TRELLIS) — image-to-3D
* [Qwen-Image](https://huggingface.co/Qwen/Qwen-Image-2512) — reference image generation
* [meshoptimizer](https://github.com/zeux/meshoptimizer) — the mesh simplifier
* [MoGe](https://github.com/microsoft/MoGe) — monocular geometry / camera estimation
* [NAF](https://github.com/valeoai/NAF) and [BiRefNet](https://huggingface.co/ZhengPeng7/BiRefNet) — conditioning and matting
* [Blender](https://www.blender.org/) (bpy) — post-processing, baking and previews

## Pinned versions

Everything is pinned in [`manifests/dependency-manifest.json`](manifests/dependency-manifest.json): the package sets
of each role, the model revisions, and the commits of the upstream projects. Refresh the package sets of the roles
installed on a machine with:

```powershell
python scripts\write_manifest.py --env image=<venv>\Scripts\python.exe --env pixal3d=<venv-3d>\bin\python
```

| Role | Installed from | Key pins |
|---|---|---|
| `control` | `requirements/control.txt` | fastapi, uvicorn, pydantic, httpx, pyyaml, trimesh, pygltflib (no CUDA) |
| `image` | `requirements/image.txt` | torch 2.11.0+cu128, diffusers 0.40.0, transformers 5.17.0, accelerate 1.15.0 |
| `pixal3d` | `requirements/pixal3d.txt` | torch 2.11.0+cu128, NATTEN 0.21.0 (NATTEN_CUDA_ARCH=12.0), flash-attn 2.8.3, nvdiffrast, CuMesh, FlexGEMM, o-voxel, transformers 4.57.3, diffusers 0.37.1 |
| `blender` | `requirements/blender.txt` | bpy 4.5.13 (Blender 4.5 LTS), meshoptimizer 0.2.30a0 (bound with explicit argtypes), fast-simplification (fallback), trimesh, pygltflib |

Models (Hugging Face, pinned revisions, cached under `[paths] models`): `Qwen/Qwen-Image-2512`,
`TencentARC/Pixal3D` (single-view checkpoints; `*_mv` optional), `Ruicheng/moge-2-vitl`,
`camenduru/dinov3-vitl16-pretrain-lvd1689m`, `briaai/RMBG-2.0` (gated) or `ZhengPeng7/BiRefNet`, plus `valeoai/NAF` (torch.hub, pinned commit).
